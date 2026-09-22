"""QuickBooks connection routes (F05). Connect, reconnect and disconnect are
``can_manage_connections`` (firm_admin); the status is ``can_view_connections``.

``GET /qbo/callback`` is where Intuit sends the browser back. It takes nothing from
the session (``app.integrations.qbo.connect``), sets no cookie, and always answers
with a 303 to the Connections page carrying a short result code. Its query string
holds the authorization ``code`` and the ``state``: ``CallbackQueryFilter`` keeps
both out of the access log.

F05.1 adds two more routes that no session reaches. ``POST /qbo/webhook`` is
Intuit's delivery: the HMAC signature is its only proof, the raw body is stored in
one transaction and the request answers 200; the poll it triggers is enqueued after
the response (``app.integrations.qbo.webhooks``). ``GET /qbo/disconnected`` is where
Intuit sends the browser when a user disconnects the app inside QuickBooks, with the
realm on the query string; the route enqueues one poll for that company (whose first
call ends in ``needs_reconnect`` through the ordinary refresh path) and redirects to
the static page. Neither marks anything on the query string alone. Refused requests
on both are counted per IP in the process and store nothing.
"""

import logging
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.schemas import QboConnectOut, QboStatusOut, QboSyncRequestOut
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession, find_session_by_token, is_expired, utcnow
from app.core.authz import can_manage_connections, can_view_connections
from app.core.config import MissingSettings, get_settings
from app.core.db import AppEngine, untenanted_session
from app.core.throttle import MemoryThrottle
from app.ingest.models import CONNECTION_STATUS_LABELS, Connection
from app.integrations.qbo import SYSTEM
from app.integrations.qbo.connect import (
    RESULT_MESSAGES,
    complete_connect,
    disconnect,
    start_connect,
)
from app.integrations.qbo.schedule import request_sync
from app.integrations.qbo.status import copy_status
from app.integrations.qbo.webhooks import (
    SCHEMA_HEADER,
    SIGNATURE_HEADER,
    TID_HEADER,
    deliveries_in_window,
    dispatch,
    dispatch_in_background,
    signature_ok,
    store_delivery,
)
from app.tenancy.models import Role

router = APIRouter(prefix="/qbo", tags=["qbo"])

CALLBACK_PATH = "/api/qbo/callback"
DISCONNECTED_PATH = "/api/qbo/disconnected"
CONNECTIONS_PAGE = "/connections"
DISCONNECTED_PAGE = "/qbo/disconnected"  # the static page nginx serves (F05.0)
# Paths whose query string is dropped from the access log (the callback's code and
# state; the disconnect page's realm id, for symmetry).
QUIET_PATHS = (CALLBACK_PATH, DISCONNECTED_PATH)

log = logging.getLogger("app.qbo")
WEBHOOK_THROTTLE = MemoryThrottle()

Manager = Annotated[Principal, Depends(can_manage_connections)]
Viewer = Annotated[Principal, Depends(can_view_connections)]

STATUS_MESSAGES: dict[str, str | None] = {
    "disconnected": "QuickBooks is not connected for this company.",
    "connected": None,
    "needs_reconnect": "QuickBooks stopped accepting this connection. "
    "A firm admin needs to reconnect it; nothing syncs until then.",
    "error": "The last sync failed. It will be tried again; "
    "if this stays, see the operations guide.",
}
# F05.1: the reason behind ``needs_reconnect`` when it is not Intuit's doing.
REASON_MESSAGES: dict[str, str] = {
    "environment_mismatch": "This company was connected with the other Intuit environment "
    "(sandbox or production) than the keys this server holds. Disconnect it; if this is "
    "the sandbox tenant, delete the tenant (operations guide, Deleting a tenant).",
}


class CallbackQueryFilter(logging.Filter):
    """uvicorn's access record is ``(client, method, full_path, http_version, status)``.
    For the callback the query string is dropped before the record is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            for path in QUIET_PATHS:
                if args[2].startswith(path):
                    record.args = (*args[:2], path, *args[3:])
        return True


def install_access_log_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, CallbackQueryFilter) for f in access.filters):
        access.addFilter(CallbackQueryFilter())


def _status_out(
    connection: Connection | None,
    principal: Principal,
    result: str | None = None,
    db: TenantSession | None = None,
) -> QboStatusOut:
    status = connection.status if connection is not None else "disconnected"
    known_result = result if result in RESULT_MESSAGES else None
    held = None
    webhooks_24h = 0
    if db is not None and connection is not None and connection.realm_id is not None:
        held = copy_status(db, principal.active_tenant_id, connection)
        webhooks_24h = deliveries_in_window(db, connection.realm_id, utcnow())
    message = STATUS_MESSAGES.get(status)
    if connection is not None and connection.last_error in REASON_MESSAGES:
        message = REASON_MESSAGES[connection.last_error]
    return QboStatusOut(
        held=held,
        status=status,
        status_label=CONNECTION_STATUS_LABELS.get(status, status.replace("_", " ").capitalize()),
        message=message,
        error_detail=connection.last_error if connection is not None else None,
        company_name=connection.company_name if connection is not None else None,
        environment=connection.environment if connection is not None else None,
        connected_company=connection is not None and connection.realm_id is not None,
        last_success_at=(
            connection.last_success_at.isoformat()
            if connection is not None and connection.last_success_at
            else None
        ),
        can_manage=principal.role is Role.firm_admin,
        result=known_result,
        result_message=RESULT_MESSAGES.get(known_result) if known_result else None,
        last_webhook_at=(
            connection.last_webhook_at.isoformat()
            if connection is not None and connection.last_webhook_at
            else None
        ),
        webhooks_24h=webhooks_24h,
    )


def _connection(db: TenantSession) -> Connection | None:
    return db.execute(select(Connection).where(Connection.system == SYSTEM)).scalar_one_or_none()


@router.get("/status", response_model=QboStatusOut)
def status(principal: Viewer, db: TenantSession, result: str | None = None) -> QboStatusOut:
    """``result`` is the code the callback put on the Connections page address; the
    sentence for it comes back as ``result_message``."""
    return _status_out(_connection(db), principal, result, db)


@router.post("/connect", response_model=QboConnectOut)
def connect(request: Request, principal: Manager, db: TenantSession) -> QboConnectOut:
    try:
        url = start_connect(
            db,
            principal.active_tenant_id,
            actor_user_id=principal.user.id,
            actor_role=principal.role,
            meta=request_meta(request),
        )
    except MissingSettings:
        raise HTTPException(status_code=503, detail=RESULT_MESSAGES["not_configured"]) from None
    return QboConnectOut(authorization_url=url)


@router.post("/disconnect", response_model=QboStatusOut)
def disconnect_route(request: Request, principal: Manager, db: TenantSession) -> QboStatusOut:
    connection = disconnect(
        db,
        principal.active_tenant_id,
        actor_user_id=principal.user.id,
        actor_role=principal.role,
        meta=request_meta(request),
    )
    return _status_out(connection, principal, None, db)


SYNC_MESSAGES = {
    ("cdc", True): "A change poll was queued; refresh in a minute to see it.",
    ("cdc", False): "A change poll is already queued or running.",
    ("backfill", True): "A fresh backfill was started; refresh to follow its progress.",
    ("backfill", False): "A backfill is already running.",
}


def _sync(request: Request, principal: Manager, db: TenantSession, kind: str) -> QboSyncRequestOut:
    connection = _connection(db)
    if connection is None or connection.status != "connected":
        raise HTTPException(status_code=409, detail="QuickBooks is not connected for this company.")
    created = request_sync(
        db,
        principal.active_tenant_id,
        connection,
        kind=kind,
        actor_user_id=principal.user.id,
        actor_role=principal.role,
        meta=request_meta(request),
    )
    return QboSyncRequestOut(created=created, message=SYNC_MESSAGES[(kind, created)])


@router.post("/sync", response_model=QboSyncRequestOut)
def sync_now(request: Request, principal: Manager, db: TenantSession) -> QboSyncRequestOut:
    """ "Sync now": one change poll."""
    return _sync(request, principal, db, "cdc")


@router.post("/backfill", response_model=QboSyncRequestOut)
def backfill(request: Request, principal: Manager, db: TenantSession) -> QboSyncRequestOut:
    """A fresh backfill, only ever on request (never on a schedule)."""
    return _sync(request, principal, db, "backfill")


def _session_user_id(request: Request, engine) -> UUID | None:
    """The user of a live session cookie, if one came along. Never required."""
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        return None
    with untenanted_session(engine) as db:
        row = find_session_by_token(db, token)
        if row is None or is_expired(row, utcnow()):
            return None
        return row.user_id


@router.get("/callback", include_in_schema=False)
def callback(
    request: Request,
    engine: AppEngine,
    state: str | None = None,
    code: str | None = None,
    realmId: str | None = None,  # noqa: N803 - Intuit's parameter name
    error: str | None = None,
) -> RedirectResponse:
    result = complete_connect(
        engine,
        state=state,
        code=code,
        realm_id=realmId,
        error=error,
        session_user_id=_session_user_id(request, engine),
        meta=request_meta(request),
    )
    base = get_settings().app_base_url.rstrip("/")
    target = f"{base}{CONNECTIONS_PAGE}?{urlencode({'result': result})}"
    return RedirectResponse(target, status_code=303)


# --- F05.1: Intuit's webhook and the disconnect page ---------------------------------------


@router.post("/webhook", include_in_schema=False)
async def webhook(request: Request, engine: AppEngine, background: BackgroundTasks) -> Response:
    """Store first, answer 200, enqueue after. The raw bytes are read before anything
    parses them; the signature is the only proof (no session, no CSRF header)."""
    body = await request.body()
    verifier = get_settings().qbo_webhook_verifier
    meta = request_meta(request)
    now = utcnow()
    if not verifier:
        log.error("qbo webhook refused: QBO_WEBHOOK_VERIFIER is not set")
        return Response(status_code=503)
    if not signature_ok(body, request.headers.get(SIGNATURE_HEADER), verifier):
        if WEBHOOK_THROTTLE.refused(meta.ip, now):
            return Response(status_code=429)
        WEBHOOK_THROTTLE.record_failure(meta.ip, now)
        if WEBHOOK_THROTTLE.should_log(meta.ip, now):
            log.warning("qbo webhook bad signature request_id=%s", meta.request_id)
        return Response(status_code=401)
    stored = await run_in_threadpool(
        store_delivery,
        engine,
        body=body,
        tid=request.headers.get(TID_HEADER),
        schema_version=request.headers.get(SCHEMA_HEADER),
        now=now,
    )
    if stored.realms:
        background.add_task(dispatch_in_background, engine, stored.realms, now)
    return Response(status_code=200)


@router.get("/disconnected", include_in_schema=False)
def disconnected(
    request: Request,
    engine: AppEngine,
    realmId: str | None = None,  # noqa: N803 - Intuit's parameter name
) -> RedirectResponse:
    """Intuit sends the browser here (registered as ``…/api/qbo/disconnected?realmId=``).
    A connected company named on the query string gets one poll, whose first call
    finds the revoked token and sets ``needs_reconnect``; nothing is marked here."""
    meta = request_meta(request)
    now = utcnow()
    realm = (realmId or "").strip()[:40]
    if realm and not WEBHOOK_THROTTLE.refused(meta.ip, now):
        outcome = dispatch(engine, (realm,), now, source="disconnect").get(realm, "unknown")
        if outcome == "unknown":
            WEBHOOK_THROTTLE.record_failure(meta.ip, now)
    base = get_settings().app_base_url.rstrip("/")
    return RedirectResponse(f"{base}{DISCONNECTED_PAGE}", status_code=303)
