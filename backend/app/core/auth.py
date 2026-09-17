"""Server-side sessions and the request principal (F02, D-10).

The cookie holds a random 256-bit token; the ``session`` row holds its SHA-256.
``get_principal`` resolves the cookie to a user inside the request transaction,
enforces idle and absolute expiry, and sets ``app.user_id`` and (when a tenant is
active) ``app.tenant_id`` with ``set_config(..., true)``. Nothing about the tenant
comes from the client: ``require_tenant_id`` reads the principal.

Firm-level authority is derived from memberships (see the F02 plan): a user holds
``firm_admin`` for a firm if any of their memberships in that firm's tenants
carries the role. ``Principal.firm_ids`` is read through the own-membership
policy (D-11), so it needs no tenant context.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import (
    get_engine,
    get_request_session,
    set_tenant_context,
    set_user_context,
    untenanted_session,
)
from app.core.security import new_token, sha256_hex
from app.tenancy.models import Membership, Role, Tenant, User, UserSession

# Every state-changing request must carry this header. Browsers add it only to
# same-origin fetches made by our client (cross-site forms cannot set custom
# headers, and a cross-origin fetch would be stopped by the CORS preflight).
CSRF_HEADER = "X-Requested-With"
CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

TOTP_OK = "ok"  # verified for this session, or not required and not enrolled
TOTP_ENROL_REQUIRED = "enrol_required"  # firm role, nothing enrolled
TOTP_VERIFY_REQUIRED = "verify_required"  # enrolled, this session not yet verified


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Principal:
    user: User
    session: UserSession
    memberships: list[Membership]
    firm_ids: frozenset[UUID]

    @property
    def active_tenant_id(self) -> UUID | None:
        return self.session.active_tenant_id

    @property
    def role(self) -> Role | None:
        """Role in the active tenant, or None when no tenant is active."""
        tid = self.active_tenant_id
        if tid is None:
            return None
        return next((m.role for m in self.memberships if m.tenant_id == tid), None)

    @property
    def firm_role(self) -> Role | None:
        roles = {m.role for m in self.memberships}
        if Role.firm_admin in roles:
            return Role.firm_admin
        if Role.firm_staff in roles:
            return Role.firm_staff
        return None

    @property
    def totp_enrolled(self) -> bool:
        return self.user.totp_enrolled_at is not None

    @property
    def totp_required(self) -> bool:
        return self.firm_role is not None or self.totp_enrolled

    @property
    def totp_verified(self) -> bool:
        return self.session.totp_verified_at is not None

    @property
    def totp_state(self) -> str:
        return totp_state(self.user, self.memberships, self.session)


def totp_state(user: User, memberships: list[Membership], session: UserSession) -> str:
    firm = any(m.role in (Role.firm_admin, Role.firm_staff) for m in memberships)
    enrolled = user.totp_enrolled_at is not None
    if session.totp_verified_at is not None:
        return TOTP_OK
    if enrolled:
        return TOTP_VERIFY_REQUIRED
    if firm:
        return TOTP_ENROL_REQUIRED
    return TOTP_OK


# --- membership / firm lookups (own rows only; D-11) ---------------------------------


def load_memberships(db: Session, user_id: UUID) -> list[Membership]:
    """Requires ``app.user_id`` on the transaction. Returns the user's own rows."""
    return list(
        db.execute(
            select(Membership).where(Membership.user_id == user_id).order_by(Membership.created_at)
        ).scalars()
    )


def firm_ids_for(db: Session, memberships: list[Membership]) -> frozenset[UUID]:
    tenant_ids = {m.tenant_id for m in memberships}
    if not tenant_ids:
        return frozenset()
    return frozenset(db.execute(select(Tenant.firm_id).where(Tenant.id.in_(tenant_ids))).scalars())


def primary_firm_id(db: Session, memberships: list[Membership]) -> UUID | None:
    """The firm to key a firm_audit_log row on; None when the user has no membership."""
    ids = sorted(firm_ids_for(db, memberships), key=str)
    return ids[0] if ids else None


# --- session store --------------------------------------------------------------------------------


def create_session(
    db: Session,
    user_id: UUID,
    *,
    active_tenant_id: UUID | None,
    totp_verified_at: datetime | None,
    now: datetime,
) -> tuple[str, UserSession]:
    """Insert a new session row. Returns the raw token (for the cookie) and the row."""
    settings = get_settings()
    token = new_token()
    row = UserSession(
        token_hash=sha256_hex(token),
        user_id=user_id,
        active_tenant_id=active_tenant_id,
        totp_verified_at=totp_verified_at,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_absolute_hours),
    )
    db.add(row)
    db.flush()
    return token, row


def rotate_session(
    db: Session, old: UserSession, *, totp_verified_at: datetime | None, now: datetime
) -> tuple[str, UserSession]:
    """New id and token, same user, tenant and absolute expiry; the old row is deleted."""
    token = new_token()
    row = UserSession(
        token_hash=sha256_hex(token),
        user_id=old.user_id,
        active_tenant_id=old.active_tenant_id,
        totp_verified_at=totp_verified_at,
        created_at=old.created_at,
        last_seen_at=now,
        expires_at=old.expires_at,
    )
    db.delete(old)
    db.flush()
    db.add(row)
    db.flush()
    return token, row


def find_session_by_token(db: Session, token: str) -> UserSession | None:
    return db.execute(
        select(UserSession).where(UserSession.token_hash == sha256_hex(token))
    ).scalar_one_or_none()


def is_expired(row: UserSession, now: datetime) -> bool:
    idle = timedelta(minutes=get_settings().session_idle_minutes)
    return row.expires_at <= now or row.last_seen_at + idle <= now


def delete_user_sessions(db: Session, user_id: UUID, *, keep: UUID | None = None) -> int:
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep is not None:
        stmt = stmt.where(UserSession.id != keep)
    return db.execute(stmt).rowcount


# --- cookie ---------------------------------------------------------------------------------------


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=s.session_cookie_name,
        value=token,
        max_age=s.session_absolute_hours * 3600,
        path="/",
        secure=s.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(
        key=s.session_cookie_name,
        path="/",
        secure=s.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


# --- FastAPI dependencies -------------------------------------------------------------------------


def get_principal(
    request: Request, db: Annotated[Session, Depends(get_request_session)]
) -> Principal:
    """401 unless the cookie names a live session. Sets ``app.user_id`` and, when a
    tenant is active, ``app.tenant_id`` on the request transaction."""
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    now = utcnow()
    row = find_session_by_token(db, token)
    if row is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if is_expired(row, now):
        # The 401 below rolls the request transaction back, so the expired row is
        # removed in a transaction of its own.
        with untenanted_session(get_engine(request)) as cleanup:
            cleanup.execute(delete(UserSession).where(UserSession.id == row.id))
        raise HTTPException(status_code=401, detail="session expired")
    row.last_seen_at = now
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    set_user_context(db, user.id)
    memberships = load_memberships(db, user.id)
    if row.active_tenant_id is not None:
        if not any(m.tenant_id == row.active_tenant_id for m in memberships):
            # Membership removed since the tenant was chosen: leave the tenant.
            row.active_tenant_id = None
        else:
            set_tenant_context(db, row.active_tenant_id)
    db.flush()
    return Principal(
        user=user,
        session=row,
        memberships=memberships,
        firm_ids=firm_ids_for(db, memberships),
    )


def get_verified_principal(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    """403 for a firm-role user with no enrolled TOTP, or any user whose enrolled TOTP
    has not been verified for this session. The ``detail`` tells the client which."""
    state = principal.totp_state
    if state != TOTP_OK:
        raise HTTPException(status_code=403, detail=state)
    return principal


def require_tenant_id(
    principal: Annotated[Principal, Depends(get_verified_principal)],
) -> UUID:
    """The active tenant of the session. Nothing from the request is consulted."""
    if principal.active_tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant context required")
    return principal.active_tenant_id


def get_tenant_session(
    db: Annotated[Session, Depends(get_request_session)],
    _tenant_id: Annotated[UUID, Depends(require_tenant_id)],
) -> Session:
    """The request transaction, guaranteed to carry ``app.tenant_id``."""
    return db


AnyPrincipal = Annotated[Principal, Depends(get_principal)]
VerifiedPrincipal = Annotated[Principal, Depends(get_verified_principal)]
TenantSession = Annotated[Session, Depends(get_tenant_session)]
