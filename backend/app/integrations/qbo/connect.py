"""Connect, callback and disconnect for a QuickBooks company (F05 brief; plan
question 1 as answered by the owner on 2026-09-20).

The callback **never takes the tenant or the user from the session**: both come
from the pending ``state`` row. If a session cookie is present it must belong to
the user who started the flow; an absent cookie is not refused. Steps:

1. Claim the ``state`` in a transaction of its own (single use even if a later step
   fails). Unknown, used and expired values all find nothing and change nothing.
2. Re-check that the starting user is still ``firm_admin`` in that tenant.
3. Exchange the code, then read ``CompanyInfo`` for the ``realmId`` on the redirect
   with the new token: Intuit refuses a token that belongs to another company, so
   the query parameter is never trusted on its own.
   A different company is refused on a reconnect, and after a disconnect too once the
   tenant holds any QuickBooks raw record; the same company again is a reconnect.
4. Store tokens (ciphertext), company and audit rows in one transaction. The partial
   unique index refuses a company another tenant holds; tokens obtained for a
   refused company are revoked.

Every outcome is a short code; ``RESULT_MESSAGES`` holds the one sentence a person
sees. Nothing here logs or returns a token, a code or a ``state`` value.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import RequestMeta, TenantEvent, write_tenant_audit
from app.core.auth import effective_role, load_firm_memberships
from app.core.config import MissingSettings, get_settings, require_qbo_settings
from app.core.crypto import CryptoError
from app.core.db import tenant_session
from app.ingest.connections import (
    get_connection_tokens,
    get_or_create_connection,
    mark_disconnected,
    set_connection_tokens,
)
from app.ingest.models import CONNECTION_REALM_INDEX, Connection, RawRecord
from app.integrations.qbo import SYSTEM, client
from app.integrations.qbo.constants import STATE_TTL_MINUTES
from app.integrations.qbo.oauth import (
    authorization_url,
    new_state,
    state_sha256,
    tenant_id_from_state,
)
from app.tenancy.models import Membership, Role, Tenant

log = logging.getLogger("app.qbo")

CONNECTED = "connected"
RESULT_MESSAGES: dict[str, str] = {
    CONNECTED: "QuickBooks is connected.",
    "cancelled": "The connection was cancelled at QuickBooks. Nothing was changed; "
    "start again when you are ready.",
    "state_invalid": "That connection link was already used or has expired. "
    "Start again from Connect to QuickBooks.",
    "wrong_user": "The connection was started by a different user. "
    "Sign in as that user, or start again from Connect to QuickBooks.",
    "not_allowed": "Only a firm admin of this company can connect QuickBooks. "
    "Ask a firm admin to connect it.",
    "different_company": "This client is linked to a different QuickBooks company. "
    "To change company, disconnect first and then connect.",
    "books_held": "This company's books are already held for a different QuickBooks company; "
    "a new QuickBooks company needs a new tenant.",
    "company_in_use": "This QuickBooks company is already connected to another client. "
    "Choose the right company at QuickBooks and connect again.",
    "code_refused": "QuickBooks did not accept the sign-in. "
    "Start again from Connect to QuickBooks.",
    "company_unverified": "QuickBooks did not confirm the company that was chosen. "
    "Start again from Connect to QuickBooks.",
    "intuit_unavailable": "QuickBooks could not be reached. Try again in a few minutes.",
    "not_configured": "QuickBooks is not set up on this server. "
    "See the QuickBooks connection section of the operations guide.",
}


@dataclass(frozen=True)
class _Claim:
    connection_id: UUID
    user_id: UUID
    realm_id: str | None
    status: str


def start_connect(
    db: Session,
    tenant_id: UUID,
    *,
    actor_user_id: UUID,
    actor_role: Role | None,
    meta: RequestMeta | None,
) -> str:
    """Store a fresh pending ``state`` (replacing any earlier one) and return the
    Intuit authorization URL. Raises ``MissingSettings`` when QBO is not configured."""
    settings = get_settings()
    require_qbo_settings(settings)
    connection = get_or_create_connection(db, tenant_id, SYSTEM)
    state = new_state(tenant_id)
    connection.oauth_state_sha256 = state_sha256(state)
    connection.oauth_state_user_id = actor_user_id
    connection.oauth_state_expires_at = datetime.now(UTC) + timedelta(minutes=STATE_TTL_MINUTES)
    db.flush()
    write_tenant_audit(
        db,
        tenant_id=tenant_id,
        action=TenantEvent.connection_started,
        entity_type="connection",
        entity_id=connection.id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        detail={"system": SYSTEM, "reconnect": connection.realm_id is not None},
        meta=meta,
    )
    return authorization_url(
        client_id=settings.qbo_client_id or "",
        redirect_uri=settings.qbo_redirect_uri or "",
        state=state,
    )


def _claim_state(engine: Engine, tenant_id: UUID, state: str) -> _Claim | None:
    with tenant_session(engine, tenant_id) as db:
        row = db.execute(
            select(Connection)
            .where(
                Connection.system == SYSTEM,
                Connection.oauth_state_sha256 == state_sha256(state),
                Connection.oauth_state_expires_at > datetime.now(UTC),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None or row.oauth_state_user_id is None:
            return None
        claim = _Claim(row.id, row.oauth_state_user_id, row.realm_id, row.status)
        row.oauth_state_sha256 = None
        row.oauth_state_user_id = None
        row.oauth_state_expires_at = None
        return claim


def _role_in_tenant(engine: Engine, tenant_id: UUID, user_id: UUID) -> Role | None:
    """The starting user's effective role now. ``membership`` is read through the
    tenant context (``tenant_isolation``), not through another user's ``app.user_id``."""
    with tenant_session(engine, tenant_id) as db:
        membership = db.execute(
            select(Membership).where(
                Membership.tenant_id == tenant_id, Membership.user_id == user_id
            )
        ).scalar_one_or_none()
        firm_id = db.execute(select(Tenant.firm_id).where(Tenant.id == tenant_id)).scalar_one()
        return effective_role(membership, firm_id, load_firm_memberships(db, user_id))


def _holds_quickbooks_records(engine: Engine, tenant_id: UUID) -> bool:
    with tenant_session(engine, tenant_id) as db:
        return (
            db.execute(select(RawRecord.id).where(RawRecord.source == SYSTEM).limit(1)).first()
            is not None
        )


def complete_connect(
    engine: Engine,
    *,
    state: str | None,
    code: str | None,
    realm_id: str | None,
    error: str | None,
    session_user_id: UUID | None,
    meta: RequestMeta | None,
) -> str:
    """Returns a key of ``RESULT_MESSAGES``."""
    settings = get_settings()
    try:
        require_qbo_settings(settings)
    except MissingSettings:
        return "not_configured"
    tenant_id = tenant_id_from_state(state) if state else None
    if tenant_id is None:
        return "state_invalid"
    claim = _claim_state(engine, tenant_id, state or "")
    if claim is None:
        return "state_invalid"
    if session_user_id is not None and session_user_id != claim.user_id:
        return "wrong_user"
    if error:
        return "cancelled"
    if not code or not realm_id:
        return "state_invalid"
    role = _role_in_tenant(engine, tenant_id, claim.user_id)
    if role is not Role.firm_admin:
        return "not_allowed"
    if claim.realm_id is not None and claim.realm_id != realm_id:
        if claim.status != "disconnected":
            return "different_company"
        # QuickBooks ids are small integers per company: a second company's records
        # would become new versions of the first company's (owner, 2026-09-20).
        if _holds_quickbooks_records(engine, tenant_id):
            return "books_held"

    try:
        tokens = client.exchange_code(settings, code)
    except client.TokenRefused:
        return "code_refused"
    except client.QboError:
        return "intuit_unavailable"
    environment = settings.qbo_environment or ""
    try:
        body = client.api_get(
            environment=environment,
            realm_id=realm_id,
            access_token=tokens.access_token,
            resource=f"companyinfo/{realm_id}",
            operation="CompanyInfo",
        )
    except client.QboError:
        client.revoke(settings, tokens.refresh_token)
        return "company_unverified"
    info = body.get("CompanyInfo") if isinstance(body, dict) else None
    company_name = str(info.get("CompanyName") or "")[:200] if isinstance(info, dict) else ""

    try:
        with tenant_session(engine, tenant_id) as db:
            connection = db.execute(
                select(Connection).where(Connection.id == claim.connection_id).with_for_update()
            ).scalar_one()
            reconnect = connection.realm_id == realm_id
            connection.realm_id = realm_id
            connection.environment = environment
            connection.company_name = company_name or None
            connection.tokens_refreshed_at = None
            set_connection_tokens(
                db,
                connection,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                token_expires_at=tokens.access_expires_at,
                refresh_token_expires_at=tokens.refresh_expires_at,
                actor_user_id=claim.user_id,
                actor_role=role,
                meta=meta,
            )
            write_tenant_audit(
                db,
                tenant_id=tenant_id,
                action=TenantEvent.connection_completed,
                entity_type="connection",
                entity_id=connection.id,
                actor_user_id=claim.user_id,
                actor_role=role,
                detail={
                    "system": SYSTEM,
                    "environment": environment,
                    "reconnect": reconnect,
                },
                meta=meta,
            )
    except IntegrityError as exc:
        if CONNECTION_REALM_INDEX not in str(exc.orig):
            raise
        client.revoke(settings, tokens.refresh_token)
        return "company_in_use"
    log.info("qbo connected connection=%s tenant=%s", claim.connection_id, tenant_id)
    return CONNECTED


def disconnect(
    db: Session,
    tenant_id: UUID,
    *,
    actor_user_id: UUID,
    actor_role: Role | None,
    meta: RequestMeta | None,
) -> Connection | None:
    """Revoke at Intuit (best effort), clear tokens, status ``disconnected``, audited.
    Synced data stays. ``None`` when the tenant never had a connection."""
    connection = db.execute(
        select(Connection)
        .where(Connection.tenant_id == tenant_id, Connection.system == SYSTEM)
        .with_for_update()
    ).scalar_one_or_none()
    if connection is None:
        return None
    if connection.status == "disconnected" and connection.access_token_enc is None:
        return connection
    revoked = False
    try:
        _access, refresh_token = get_connection_tokens(connection)
    except (LookupError, CryptoError):
        refresh_token = None
    if refresh_token:
        settings = get_settings()
        try:
            require_qbo_settings(settings)
        except MissingSettings:
            pass
        else:
            revoked = client.revoke(settings, refresh_token)
    mark_disconnected(
        db,
        connection,
        revoked_at_source=revoked,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        meta=meta,
    )
    return connection
