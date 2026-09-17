"""Authentication flows (F02, D-10): login and lockout, TOTP enrolment and
verification, recovery codes, password change and reset, firm_admin user and
membership administration, tenant selection.

Every function runs inside the caller's transaction and writes its audit row into
it (D-12). Expected failures that must be *persisted* (a failed login, a failed
code, a lockout) are returned as results, never raised, so the request
transaction commits. Failures that must roll back raise ``AuthError``.

No function here logs. No return value or exception message carries a password,
secret, code, or session token except the explicit "show once" returns
(``start_totp_enrolment``, ``confirm_totp_enrolment``, ``issue_password_reset``).
"""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import FirmEvent, RequestMeta, TenantEvent, write_firm_audit, write_tenant_audit
from app.core.auth import (
    TOTP_OK,
    Principal,
    create_session,
    find_session_by_token,
    firm_ids_for,
    load_memberships,
    primary_firm_id,
    rotate_session,
    totp_state,
)
from app.core.auth import delete_user_sessions as _delete_user_sessions
from app.core.config import get_settings
from app.core.crypto import get_keyring
from app.core.db import set_tenant_context, set_user_context
from app.core.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    hash_recovery_code,
    match_totp,
    new_recovery_codes,
    new_token,
    new_totp_secret,
    sha256_hex,
    totp_provisioning_uri,
    verify_password,
)
from app.tenancy.models import Membership, Role, Tenant, User, UserSession


class AuthError(Exception):
    """Mapped to an HTTP error by ``app.main``; the transaction rolls back."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Outcome(enum.StrEnum):
    ok = "ok"
    invalid = "invalid"
    locked = "locked"


@dataclass
class LoginResult:
    outcome: Outcome
    token: str | None = None
    user: User | None = None
    session: UserSession | None = None
    totp: str = TOTP_OK
    active_tenant_id: UUID | None = None
    role: Role | None = None


@dataclass
class CodeResult:
    outcome: Outcome
    token: str | None = None
    recovery_codes: list[str] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _firm_role(memberships: list[Membership]) -> Role | None:
    roles = {m.role for m in memberships}
    if Role.firm_admin in roles:
        return Role.firm_admin
    if Role.firm_staff in roles:
        return Role.firm_staff
    return None


def _role_in(memberships: list[Membership], tenant_id: UUID | None) -> Role | None:
    return next((m.role for m in memberships if m.tenant_id == tenant_id), None)


# --- lockout --------------------------------------------------------------------------------------


def _is_locked(user: User, now: datetime) -> bool:
    return user.locked_until is not None and user.locked_until > now


def _record_failure(
    db: Session,
    user: User,
    *,
    firm_id: UUID | None,
    firm_role: Role | None,
    step: str,
    now: datetime,
    meta: RequestMeta,
) -> None:
    """Count a failed attempt; apply the lock on the Nth failure inside the window."""
    s = get_settings()
    window = timedelta(minutes=s.login_lockout_minutes)
    if user.failed_login_window_start is None or user.failed_login_window_start + window <= now:
        user.failed_login_window_start = now
        user.failed_login_count = 1
    else:
        user.failed_login_count += 1
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.login_failure,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=firm_role,
        detail={"step": step, "attempt": user.failed_login_count},
        meta=meta,
    )
    if user.failed_login_count >= s.login_lockout_attempts:
        user.locked_until = now + window
        user.failed_login_count = 0
        user.failed_login_window_start = None
        write_firm_audit(
            db,
            firm_id=firm_id,
            action=FirmEvent.account_locked,
            entity_type="user",
            entity_id=user.id,
            actor_user_id=user.id,
            actor_role=firm_role,
            detail={"until": user.locked_until.isoformat(), "step": step},
            meta=meta,
        )
    db.flush()


def _clear_failures(user: User) -> None:
    user.failed_login_count = 0
    user.failed_login_window_start = None
    user.locked_until = None


# --- login ----------------------------------------------------------------------------------------


def login(
    db: Session,
    *,
    email: str,
    password: str,
    old_token: str | None,
    meta: RequestMeta,
    now: datetime | None = None,
) -> LoginResult:
    now = now or _now()
    email = email.strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        verify_password(None, password)  # same cost as a real check
        write_firm_audit(
            db,
            firm_id=None,
            action=FirmEvent.login_failure,
            entity_type="user",
            entity_id=None,
            actor_user_id=None,
            actor_role=None,
            detail={"step": "password", "reason": "unknown_email", "email": email},
            meta=meta,
        )
        return LoginResult(Outcome.invalid)

    # The server now acts for this account: its own memberships become readable
    # (D-11), which is what tenant auto-selection and firm attribution need.
    set_user_context(db, user.id)
    memberships = load_memberships(db, user.id)
    firm_id = primary_firm_id(db, memberships)
    firm_role = _firm_role(memberships)

    if _is_locked(user, now):
        write_firm_audit(
            db,
            firm_id=firm_id,
            action=FirmEvent.login_failure,
            entity_type="user",
            entity_id=user.id,
            actor_user_id=user.id,
            actor_role=firm_role,
            detail={"step": "password", "reason": "locked"},
            meta=meta,
        )
        return LoginResult(Outcome.locked)

    if not verify_password(user.password_hash, password):
        _record_failure(
            db, user, firm_id=firm_id, firm_role=firm_role, step="password", now=now, meta=meta
        )
        return LoginResult(Outcome.invalid)

    _clear_failures(user)
    if old_token:
        old = find_session_by_token(db, old_token)
        if old is not None:
            db.delete(old)
            db.flush()

    active = memberships[0].tenant_id if len(memberships) == 1 else None
    token, row = create_session(
        db, user.id, active_tenant_id=active, totp_verified_at=None, now=now
    )
    state = totp_state(user, memberships, row)
    if state == TOTP_OK:
        _complete_login(db, user, row, memberships, firm_id=firm_id, method="password", meta=meta)
    return LoginResult(
        Outcome.ok,
        token=token,
        user=user,
        session=row,
        totp=state,
        active_tenant_id=active,
        role=_role_in(memberships, active),
    )


def _complete_login(
    db: Session,
    user: User,
    row: UserSession,
    memberships: list[Membership],
    *,
    firm_id: UUID | None,
    method: str,
    meta: RequestMeta,
) -> None:
    """The session is now fully usable: record it, and the auto-selected tenant."""
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.login_success,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=_firm_role(memberships),
        detail={
            "method": method,
            "active_tenant_id": str(row.active_tenant_id) if row.active_tenant_id else None,
        },
        meta=meta,
    )
    if row.active_tenant_id is not None:
        set_tenant_context(db, row.active_tenant_id)
        write_tenant_audit(
            db,
            tenant_id=row.active_tenant_id,
            action=TenantEvent.tenant_enter,
            entity_type="tenant",
            entity_id=row.active_tenant_id,
            actor_user_id=user.id,
            actor_role=_role_in(memberships, row.active_tenant_id),
            detail={"via": "auto"},
            meta=meta,
        )


def logout(db: Session, principal: Principal, *, meta: RequestMeta) -> None:
    write_firm_audit(
        db,
        firm_id=primary_firm_id(db, principal.memberships),
        action=FirmEvent.logout,
        entity_type="user",
        entity_id=principal.user.id,
        actor_user_id=principal.user.id,
        actor_role=principal.firm_role,
        meta=meta,
    )
    db.delete(principal.session)
    db.flush()


# --- TOTP -----------------------------------------------------------------------------------------


def _totp_secret(user: User) -> str:
    if user.totp_secret_enc is None or user.totp_key_id is None:
        raise AuthError(409, "TOTP is not set up")
    keyring = get_keyring()
    return keyring.decrypt(user.totp_key_id, user.totp_secret_enc, aad=user.id.bytes).decode()


def _store_totp_secret(user: User, secret: str) -> None:
    user.totp_key_id, user.totp_secret_enc = get_keyring().encrypt(
        secret.encode(), aad=user.id.bytes
    )


def start_totp_enrolment(db: Session, principal: Principal, *, issuer: str) -> tuple[str, str]:
    """Generate and store (encrypted) a pending secret. Returns ``(secret, otpauth_uri)``:
    the only time the secret leaves the server. 409 if already enrolled."""
    user = principal.user
    if user.totp_enrolled_at is not None:
        raise AuthError(409, "TOTP is already enrolled; ask a firm admin to reset it")
    secret = new_totp_secret()
    _store_totp_secret(user, secret)
    user.totp_last_counter = None
    db.flush()
    return secret, totp_provisioning_uri(secret, user.email, issuer)


def _accept_code(
    db: Session, principal: Principal, code: str, *, now: datetime, meta: RequestMeta
) -> tuple[Outcome, int | None]:
    """Shared by confirm and verify: lockout, ±1 step, no replay."""
    user = principal.user
    firm_id = primary_firm_id(db, principal.memberships)
    if _is_locked(user, now):
        return Outcome.locked, None
    counter = match_totp(_totp_secret(user), code, now)
    last = user.totp_last_counter
    if counter is None or (last is not None and counter <= last):
        _record_failure(
            db,
            user,
            firm_id=firm_id,
            firm_role=principal.firm_role,
            step="totp",
            now=now,
            meta=meta,
        )
        return Outcome.invalid, None
    user.totp_last_counter = counter
    _clear_failures(user)
    return Outcome.ok, counter


def _verified_session(
    db: Session, principal: Principal, *, method: str, now: datetime, meta: RequestMeta
) -> str:
    """Rotate the session id, mark it verified, and complete the login if it was not."""
    was_verified = principal.session.totp_verified_at is not None
    token, row = rotate_session(db, principal.session, totp_verified_at=now, now=now)
    principal.session = row
    if not was_verified:
        _complete_login(
            db,
            principal.user,
            row,
            principal.memberships,
            firm_id=primary_firm_id(db, principal.memberships),
            method=method,
            meta=meta,
        )
    return token


def confirm_totp_enrolment(
    db: Session, principal: Principal, code: str, *, meta: RequestMeta, now: datetime | None = None
) -> CodeResult:
    now = now or _now()
    user = principal.user
    if user.totp_enrolled_at is not None:
        raise AuthError(409, "TOTP is already enrolled")
    if user.totp_secret_enc is None:
        raise AuthError(409, "start enrolment first")
    outcome, _ = _accept_code(db, principal, code, now=now, meta=meta)
    if outcome is not Outcome.ok:
        return CodeResult(outcome)
    codes = new_recovery_codes()
    user.totp_enrolled_at = now
    user.recovery_code_hashes = [hash_recovery_code(c) for c in codes]
    write_firm_audit(
        db,
        firm_id=primary_firm_id(db, principal.memberships),
        action=FirmEvent.totp_enrolled,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=principal.firm_role,
        detail={"recovery_codes_issued": len(codes)},
        meta=meta,
    )
    token = _verified_session(db, principal, method="password+totp", now=now, meta=meta)
    return CodeResult(Outcome.ok, token=token, recovery_codes=codes)


def verify_totp(
    db: Session, principal: Principal, code: str, *, meta: RequestMeta, now: datetime | None = None
) -> CodeResult:
    now = now or _now()
    if principal.user.totp_enrolled_at is None:
        raise AuthError(409, "TOTP is not enrolled")
    outcome, _ = _accept_code(db, principal, code, now=now, meta=meta)
    if outcome is not Outcome.ok:
        return CodeResult(outcome)
    token = _verified_session(db, principal, method="password+totp", now=now, meta=meta)
    return CodeResult(Outcome.ok, token=token)


def use_recovery_code(
    db: Session, principal: Principal, code: str, *, meta: RequestMeta, now: datetime | None = None
) -> CodeResult:
    now = now or _now()
    user = principal.user
    if user.totp_enrolled_at is None:
        raise AuthError(409, "TOTP is not enrolled")
    firm_id = primary_firm_id(db, principal.memberships)
    if _is_locked(user, now):
        return CodeResult(Outcome.locked)
    hashes = list(user.recovery_code_hashes or [])
    h = hash_recovery_code(code)
    if h not in hashes:
        _record_failure(
            db,
            user,
            firm_id=firm_id,
            firm_role=principal.firm_role,
            step="recovery",
            now=now,
            meta=meta,
        )
        return CodeResult(Outcome.invalid)
    hashes.remove(h)
    user.recovery_code_hashes = hashes
    _clear_failures(user)
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.recovery_code_used,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=principal.firm_role,
        detail={"remaining": len(hashes)},
        meta=meta,
    )
    token = _verified_session(db, principal, method="password+recovery", now=now, meta=meta)
    return CodeResult(Outcome.ok, token=token)


def reset_totp(
    db: Session, actor: Principal | None, target: User, *, meta: RequestMeta, via: str
) -> None:
    """Clear enrolment so the user must enrol again. Ends all of their sessions."""
    target.totp_secret_enc = None
    target.totp_key_id = None
    target.totp_enrolled_at = None
    target.totp_last_counter = None
    target.recovery_code_hashes = None
    _delete_user_sessions(db, target.id)
    write_firm_audit(
        db,
        firm_id=_firm_for_target(db, actor, target),
        action=FirmEvent.totp_reset,
        entity_type="user",
        entity_id=target.id,
        actor_user_id=actor.user.id if actor else None,
        actor_role=actor.firm_role if actor else None,
        detail={"via": via},
        meta=meta,
    )
    db.flush()


# --- passwords ------------------------------------------------------------------------------------


def _check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def change_password(
    db: Session,
    principal: Principal,
    *,
    current_password: str,
    new_password: str,
    meta: RequestMeta,
    now: datetime | None = None,
) -> bool:
    """False if the current password is wrong (persisted as a failure)."""
    now = now or _now()
    _check_new_password(new_password)
    user = principal.user
    if not verify_password(user.password_hash, current_password):
        return False
    _set_password(db, user, new_password, now=now)
    _delete_user_sessions(db, user.id, keep=principal.session.id)
    write_firm_audit(
        db,
        firm_id=primary_firm_id(db, principal.memberships),
        action=FirmEvent.password_changed,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=principal.firm_role,
        detail={"via": "self"},
        meta=meta,
    )
    return True


def _set_password(db: Session, user: User, password: str, *, now: datetime) -> None:
    user.password_hash = hash_password(password)
    user.password_changed_at = now
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    _clear_failures(user)
    db.flush()


def issue_password_reset(
    db: Session,
    actor: Principal | None,
    target: User,
    *,
    meta: RequestMeta,
    now: datetime | None = None,
) -> str:
    """One-time link for the target. Returned to the caller to hand over out of band
    (no e-mail in F02). Ends the target's sessions."""
    now = now or _now()
    s = get_settings()
    token = new_token()
    target.password_reset_token_hash = sha256_hex(token)
    target.password_reset_expires_at = now + timedelta(hours=s.password_reset_ttl_hours)
    _delete_user_sessions(db, target.id)
    write_firm_audit(
        db,
        firm_id=_firm_for_target(db, actor, target),
        action=FirmEvent.password_reset_issued,
        entity_type="user",
        entity_id=target.id,
        actor_user_id=actor.user.id if actor else None,
        actor_role=actor.firm_role if actor else None,
        detail={"expires_at": target.password_reset_expires_at.isoformat()},
        meta=meta,
    )
    db.flush()
    return f"{s.app_base_url.rstrip('/')}/reset-password?token={token}"


def complete_password_reset(
    db: Session,
    *,
    token: str,
    new_password: str,
    meta: RequestMeta,
    now: datetime | None = None,
) -> User:
    now = now or _now()
    _check_new_password(new_password)
    user = db.execute(
        select(User).where(User.password_reset_token_hash == sha256_hex(token))
    ).scalar_one_or_none()
    expires = None if user is None else user.password_reset_expires_at
    if user is None or expires is None or expires <= now:
        raise AuthError(400, "invalid or expired reset link")
    set_user_context(db, user.id)  # proof of the link: act for the account
    memberships = load_memberships(db, user.id)
    _set_password(db, user, new_password, now=now)
    _delete_user_sessions(db, user.id)
    write_firm_audit(
        db,
        firm_id=primary_firm_id(db, memberships),
        action=FirmEvent.password_changed,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=_firm_role(memberships),
        detail={"via": "reset_link"},
        meta=meta,
    )
    return user


def set_password_directly(
    db: Session, target: User, password: str, *, meta: RequestMeta, via: str
) -> None:
    """Last-resort path for the CLI. Ends the user's sessions."""
    _check_new_password(password)
    _set_password(db, target, password, now=_now())
    _delete_user_sessions(db, target.id)
    write_firm_audit(
        db,
        firm_id=_firm_for_target(db, None, target),
        action=FirmEvent.password_changed,
        entity_type="user",
        entity_id=target.id,
        actor_user_id=None,
        actor_role=None,
        detail={"via": via},
        meta=meta,
    )


# --- users and memberships (firm_admin) -----------------------------------------------------------


def _firm_for_target(db: Session, actor: Principal | None, target: User) -> UUID | None:
    if actor is not None:
        ids = sorted(actor.firm_ids, key=str)
        if ids:
            return ids[0]
    # No actor (CLI) or actor without a firm: attribute to the target's own firm.
    saved = db.execute(select(Membership).where(Membership.user_id == target.id)).scalars().all()
    return primary_firm_id(db, list(saved))


def create_user(
    db: Session,
    *,
    email: str,
    display_name: str,
    password: str | None,
    actor: Principal | None,
    firm_id: UUID | None,
    meta: RequestMeta,
    via: str = "api",
) -> User:
    email = email.strip().lower()
    if "@" not in email or len(email) < 3:
        raise AuthError(422, "invalid e-mail address")
    if db.execute(select(User.id).where(User.email == email)).scalar_one_or_none():
        raise AuthError(409, "a user with that e-mail already exists")
    if password is not None:
        _check_new_password(password)
    user = User(email=email, display_name=display_name.strip())
    if password is not None:
        user.password_hash = hash_password(password)
        user.password_changed_at = _now()
    db.add(user)
    db.flush()
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.user_created,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=actor.user.id if actor else None,
        actor_role=actor.firm_role if actor else None,
        detail={"email": email, "via": via, "password_set": password is not None},
        meta=meta,
    )
    return user


def enter_tenant_for_admin(db: Session, actor: Principal, tenant_id: UUID) -> Tenant:
    """Set ``app.tenant_id`` to a tenant of the actor's firm for an administrative
    write. The actor need not hold a membership there (first membership in a new
    tenant). 404 for a tenant outside the actor's firms."""
    tenant = db.get(Tenant, tenant_id)
    if tenant is None or tenant.firm_id not in actor.firm_ids:
        raise AuthError(404, "tenant not found")
    set_tenant_context(db, tenant_id)
    return tenant


def create_membership(
    db: Session,
    actor: Principal | None,
    *,
    tenant_id: UUID,
    user: User,
    role: Role,
    meta: RequestMeta,
) -> Membership:
    """Requires ``app.tenant_id`` = ``tenant_id`` (``enter_tenant_for_admin`` or
    ``tenant_session``)."""
    existing = db.execute(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == user.id)
    ).scalar_one_or_none()
    if existing is not None:
        raise AuthError(409, "membership already exists")
    m = Membership(tenant_id=tenant_id, user_id=user.id, role=role)
    db.add(m)
    db.flush()
    write_tenant_audit(
        db,
        tenant_id=tenant_id,
        action=TenantEvent.membership_created,
        entity_type="membership",
        entity_id=m.id,
        actor_user_id=actor.user.id if actor else None,
        actor_role=actor.firm_role if actor else None,
        detail={"user_id": str(user.id), "role": role.value},
        meta=meta,
    )
    return m


def change_membership_role(
    db: Session,
    actor: Principal | None,
    *,
    tenant_id: UUID,
    user_id: UUID,
    role: Role,
    meta: RequestMeta,
) -> Membership:
    m = db.execute(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
    ).scalar_one_or_none()
    if m is None:
        raise AuthError(404, "membership not found")
    old = m.role
    m.role = role
    db.flush()
    write_tenant_audit(
        db,
        tenant_id=tenant_id,
        action=TenantEvent.membership_role_changed,
        entity_type="membership",
        entity_id=m.id,
        actor_user_id=actor.user.id if actor else None,
        actor_role=actor.firm_role if actor else None,
        detail={"user_id": str(user_id), "old_role": old.value, "new_role": role.value},
        meta=meta,
    )
    return m


def remove_membership(
    db: Session,
    actor: Principal | None,
    *,
    tenant_id: UUID,
    user_id: UUID,
    meta: RequestMeta,
) -> None:
    m = db.execute(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
    ).scalar_one_or_none()
    if m is None:
        raise AuthError(404, "membership not found")
    write_tenant_audit(
        db,
        tenant_id=tenant_id,
        action=TenantEvent.membership_removed,
        entity_type="membership",
        entity_id=m.id,
        actor_user_id=actor.user.id if actor else None,
        actor_role=actor.firm_role if actor else None,
        detail={"user_id": str(user_id), "role": m.role.value},
        meta=meta,
    )
    db.delete(m)
    db.flush()


# --- tenant selection -----------------------------------------------------------------------------


def set_active_tenant(
    db: Session, principal: Principal, tenant_id: UUID, *, meta: RequestMeta
) -> Membership:
    """Re-check membership, switch the session, record ``tenant_enter``. 403 (and a
    rollback, so the session is unchanged) without a membership."""
    memberships = load_memberships(db, principal.user.id)
    m = next((m for m in memberships if m.tenant_id == tenant_id), None)
    if m is None:
        raise AuthError(403, "no membership in that tenant")
    principal.session.active_tenant_id = tenant_id
    principal.memberships = memberships
    db.flush()
    set_tenant_context(db, tenant_id)
    write_tenant_audit(
        db,
        tenant_id=tenant_id,
        action=TenantEvent.tenant_enter,
        entity_type="tenant",
        entity_id=tenant_id,
        actor_user_id=principal.user.id,
        actor_role=m.role,
        detail={"via": "switcher"},
        meta=meta,
    )
    return m


def tenants_for(db: Session, principal: Principal) -> list[tuple[Membership, Tenant]]:
    tenants = {
        t.id: t
        for t in db.execute(
            select(Tenant).where(Tenant.id.in_([m.tenant_id for m in principal.memberships]))
        ).scalars()
    }
    return [(m, tenants[m.tenant_id]) for m in principal.memberships if m.tenant_id in tenants]


def firm_ids_of(db: Session, principal: Principal) -> frozenset[UUID]:
    return firm_ids_for(db, principal.memberships)
