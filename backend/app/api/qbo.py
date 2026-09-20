"""QuickBooks connection routes (F05). Connect, reconnect and disconnect are
``can_manage_connections`` (firm_admin); the status is ``can_view_connections``.

``GET /qbo/callback`` is where Intuit sends the browser back. It takes nothing from
the session (``app.integrations.qbo.connect``), sets no cookie, and always answers
with a 303 to the Connections page carrying a short result code. Its query string
holds the authorization ``code`` and the ``state``: ``CallbackQueryFilter`` keeps
both out of the access log.
"""

import logging
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.schemas import QboConnectOut, QboStatusOut
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession, find_session_by_token, is_expired, utcnow
from app.core.authz import can_manage_connections, can_view_connections
from app.core.config import MissingSettings, get_settings
from app.core.db import AppEngine, untenanted_session
from app.ingest.models import CONNECTION_STATUS_LABELS, Connection
from app.integrations.qbo import SYSTEM
from app.integrations.qbo.connect import (
    RESULT_MESSAGES,
    complete_connect,
    disconnect,
    start_connect,
)
from app.tenancy.models import Role

router = APIRouter(prefix="/qbo", tags=["qbo"])

CALLBACK_PATH = "/api/qbo/callback"
CONNECTIONS_PAGE = "/connections"

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


class CallbackQueryFilter(logging.Filter):
    """uvicorn's access record is ``(client, method, full_path, http_version, status)``.
    For the callback the query string is dropped before the record is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            if args[2].startswith(CALLBACK_PATH):
                record.args = (*args[:2], CALLBACK_PATH, *args[3:])
        return True


def install_access_log_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, CallbackQueryFilter) for f in access.filters):
        access.addFilter(CallbackQueryFilter())


def _status_out(
    connection: Connection | None, principal: Principal, result: str | None = None
) -> QboStatusOut:
    status = connection.status if connection is not None else "disconnected"
    known_result = result if result in RESULT_MESSAGES else None
    return QboStatusOut(
        status=status,
        status_label=CONNECTION_STATUS_LABELS.get(status, status.replace("_", " ").capitalize()),
        message=STATUS_MESSAGES.get(status),
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
    )


def _connection(db: TenantSession) -> Connection | None:
    return db.execute(select(Connection).where(Connection.system == SYSTEM)).scalar_one_or_none()


@router.get("/status", response_model=QboStatusOut)
def status(principal: Viewer, db: TenantSession, result: str | None = None) -> QboStatusOut:
    """``result`` is the code the callback put on the Connections page address; the
    sentence for it comes back as ``result_message``."""
    return _status_out(_connection(db), principal, result)


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
    return _status_out(connection, principal)


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
