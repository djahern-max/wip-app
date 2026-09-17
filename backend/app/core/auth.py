"""Server-side sessions and the request principal (F02, D-10; F02.1, D-15/D-16).

The cookie holds a random 256-bit token; the ``session`` row holds its SHA-256.
``get_principal`` resolves the cookie to a user inside the request transaction,
enforces idle and absolute expiry, and sets ``app.user_id`` and (when a tenant is
active) ``app.tenant_id`` with ``set_config(..., true)``. Nothing about the tenant
comes from the client: ``require_tenant_id`` reads the principal.

Firm authority comes only from ``firm_membership`` (D-15). The role that applies
inside a tenant is computed by exactly one function, ``effective_role``; nothing
else in the application reads ``Membership.role``. An entry row (NULL role) with
no ``firm_membership`` in that tenant's firm resolves to **no access**.

``read_as_user`` (D-18) is the only sanctioned way to read another user's
``membership`` rows: it swaps ``app.user_id`` for the block and restores the
actor's id in ``finally``. It is called only from ``app.auth.admin``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError, InvalidRequestError
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
from app.tenancy.models import FirmMembership, Membership, Role, Tenant, User, UserSession

# Every state-changing request must carry this header. Browsers add it only to
# same-origin fetches made by our client (cross-site forms cannot set custom
# headers, and a cross-origin fetch would be stopped by the CORS preflight).
CSRF_HEADER = "X-Requested-With"
CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

TOTP_OK = "ok"  # verified for this session, or not required and not enrolled
TOTP_ENROL_REQUIRED = "enrol_required"  # firm user, nothing enrolled (link session only)
TOTP_VERIFY_REQUIRED = "verify_required"  # enrolled, this session not yet verified


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- the one place a tenant role is computed --------------------------------------------------


def effective_role(
    membership: Membership | None,
    tenant_firm_id: UUID | None,
    firm_memberships: list[FirmMembership],
) -> Role | None:
    """The role that applies inside a tenant (D-15, owner condition on option b).

    - A client-role row: that role.
    - An entry row (NULL role): the ``firm_membership`` role in the tenant's firm.
    - An entry row whose user has no ``firm_membership`` in that firm (orphan):
      ``None`` = no access.
    - No row: ``None``.
    """
    if membership is None:
        return None
    if membership.role is not None:
        return membership.role
    return next((fm.role for fm in firm_memberships if fm.firm_id == tenant_firm_id), None)


def highest_firm_role(firm_memberships: list[FirmMembership]) -> Role | None:
    roles = {fm.role for fm in firm_memberships}
    if Role.firm_admin in roles:
        return Role.firm_admin
    if Role.firm_staff in roles:
        return Role.firm_staff
    return None


@dataclass
class Principal:
    user: User
    session: UserSession
    memberships: list[Membership]
    firm_memberships: list[FirmMembership]
    tenant_firm_ids: dict[UUID, UUID] = field(default_factory=dict)  # tenant → firm

    @property
    def active_tenant_id(self) -> UUID | None:
        return self.session.active_tenant_id

    def membership_in(self, tenant_id: UUID | None) -> Membership | None:
        return next((m for m in self.memberships if m.tenant_id == tenant_id), None)

    def role_in(self, tenant_id: UUID | None) -> Role | None:
        return effective_role(
            self.membership_in(tenant_id),
            self.tenant_firm_ids.get(tenant_id),
            self.firm_memberships,
        )

    @property
    def role(self) -> Role | None:
        """Effective role in the active tenant, or None when no tenant is active."""
        return self.role_in(self.active_tenant_id)

    @property
    def firm_role(self) -> Role | None:
        return highest_firm_role(self.firm_memberships)

    @property
    def firm_ids(self) -> frozenset[UUID]:
        return frozenset(fm.firm_id for fm in self.firm_memberships)

    @property
    def firm_id(self) -> UUID | None:
        """The actor's firm (one per deployment, D-17); None for a client user."""
        ids = sorted(self.firm_ids, key=str)
        return ids[0] if ids else None

    @property
    def is_firm_user(self) -> bool:
        return bool(self.firm_memberships)

    @property
    def totp_enrolled(self) -> bool:
        return self.user.totp_enrolled_at is not None

    @property
    def totp_required(self) -> bool:
        return self.is_firm_user or self.totp_enrolled

    @property
    def totp_verified(self) -> bool:
        return self.session.totp_verified_at is not None

    @property
    def totp_state(self) -> str:
        return totp_state(self.user, self.is_firm_user, self.session)

    def accessible_tenant_ids(self) -> list[UUID]:
        return [m.tenant_id for m in self.memberships if self.role_in(m.tenant_id) is not None]


def totp_state(user: User, is_firm_user: bool, session: UserSession) -> str:
    if session.totp_verified_at is not None:
        return TOTP_OK
    if user.totp_enrolled_at is not None:
        return TOTP_VERIFY_REQUIRED
    if is_firm_user:
        return TOTP_ENROL_REQUIRED
    return TOTP_OK


# --- membership / firm lookups (own rows only; D-11) ---------------------------------


def load_memberships(db: Session, user_id: UUID) -> tuple[list[Membership], dict[UUID, UUID]]:
    """Requires ``app.user_id`` on the transaction. Returns the user's own rows and
    the tenant → firm map for them (``tenant`` is global, so one join)."""
    rows = db.execute(
        select(Membership, Tenant.firm_id)
        .join(Tenant, Tenant.id == Membership.tenant_id)
        .where(Membership.user_id == user_id)
        .order_by(Membership.created_at)
    ).all()
    return [m for m, _ in rows], {m.tenant_id: firm_id for m, firm_id in rows}


def load_firm_memberships(db: Session, user_id: UUID) -> list[FirmMembership]:
    """Global table: no context needed."""
    return list(
        db.execute(
            select(FirmMembership)
            .where(FirmMembership.user_id == user_id)
            .order_by(FirmMembership.created_at)
        ).scalars()
    )


@contextmanager
def read_as_user(db: Session, target_user_id: UUID, *, actor_user_id: UUID) -> Iterator[None]:
    """D-18: read another user's own ``membership`` rows through the D-11 policy.

    Sets ``app.user_id`` to the target for the block and restores the actor's id in
    ``finally``, so a failure inside the block cannot leave the transaction acting
    for the wrong user. The policy is ``FOR SELECT`` only: writes to ``membership``
    inside the block still need tenant context. Callers: ``app.auth.admin`` only,
    after the firm-role check, and they filter results to the actor's firm.
    """
    set_user_context(db, target_user_id)
    try:
        yield
    finally:
        try:
            set_user_context(db, actor_user_id)
        except (InvalidRequestError, DBAPIError):
            # A statement inside the block failed: the transaction is aborted and
            # its rollback discards the swapped setting; nothing can act on it.
            pass


# --- session store --------------------------------------------------------------------------------


def create_session(
    db: Session,
    user_id: UUID,
    *,
    active_tenant_id: UUID | None,
    totp_verified_at: datetime | None,
    now: datetime,
    ttl: timedelta | None = None,
) -> tuple[str, UserSession]:
    """Insert a new session row. Returns the raw token (for the cookie) and the row.
    ``ttl`` overrides the absolute lifetime (enrolment-only sessions, D-16)."""
    settings = get_settings()
    token = new_token()
    row = UserSession(
        token_hash=sha256_hex(token),
        user_id=user_id,
        active_tenant_id=active_tenant_id,
        totp_verified_at=totp_verified_at,
        created_at=now,
        last_seen_at=now,
        expires_at=now + (ttl or timedelta(hours=settings.session_absolute_hours)),
    )
    db.add(row)
    db.flush()
    return token, row


def rotate_session(
    db: Session,
    old: UserSession,
    *,
    totp_verified_at: datetime | None,
    now: datetime,
    fresh_expiry: bool = False,
) -> tuple[str, UserSession]:
    """New id and token, same user and tenant; the old row is deleted. The absolute
    expiry is kept unless ``fresh_expiry`` (an enrolment-only session that has just
    completed enrolment becomes a normal session)."""
    token = new_token()
    expires_at = old.expires_at
    if fresh_expiry:
        expires_at = now + timedelta(hours=get_settings().session_absolute_hours)
    row = UserSession(
        token_hash=sha256_hex(token),
        user_id=old.user_id,
        active_tenant_id=old.active_tenant_id,
        totp_verified_at=totp_verified_at,
        created_at=old.created_at,
        last_seen_at=now,
        expires_at=expires_at,
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


def delete_user_sessions(db: Session, user_id: UUID) -> int:
    """Every session of the user, the caller's included (F02.1)."""
    return db.execute(delete(UserSession).where(UserSession.user_id == user_id)).rowcount


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
    # Idle-timeout bookkeeping in its own short transaction, and only when stale by
    # SESSION_TOUCH_SECONDS: the request transaction must not hold a row lock on the
    # session for its whole duration, or an admin ending this user's sessions
    # concurrently would deadlock with it (seen with two admins demoting each other).
    # Everything else about the principal (session row, user, memberships,
    # firm memberships) is read inside the request transaction.
    if now - row.last_seen_at >= timedelta(seconds=get_settings().session_touch_seconds):
        with untenanted_session(get_engine(request)) as touch:
            touch.execute(
                update(UserSession).where(UserSession.id == row.id).values(last_seen_at=now)
            )
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    set_user_context(db, user.id)
    memberships, tenant_firms = load_memberships(db, user.id)
    principal = Principal(
        user=user,
        session=row,
        memberships=memberships,
        firm_memberships=load_firm_memberships(db, user.id),
        tenant_firm_ids=tenant_firms,
    )
    if row.active_tenant_id is not None:
        if principal.role_in(row.active_tenant_id) is None:
            # Membership removed, or entry row orphaned, since the tenant was chosen.
            row.active_tenant_id = None
        else:
            set_tenant_context(db, row.active_tenant_id)
    db.flush()
    return principal


def get_verified_principal(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    """403 for a firm user with no enrolled TOTP (an enrolment-only session), or any
    user whose enrolled TOTP has not been verified for this session."""
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
