"""Authentication flows (F02, D-10; F02.1, D-16): login and lockout, TOTP enrolment
and verification, recovery codes, password change, activation links, tenant
selection. Administration lives in ``app.auth.admin``.

Every function runs inside the caller's transaction and writes its audit row into
it (D-12). Expected failures that must be *persisted* (a failed login, a failed
code, a lockout, a failed link redemption) are returned as results, never raised,
so the request transaction commits. Failures that must roll back raise ``AuthError``.

No function here logs. No return value or exception message carries a password,
secret, code, token or attempted e-mail address except the explicit "show once"
returns (``start_totp_enrolment``, ``confirm_totp_enrolment``,
``issue_activation_link``).
"""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, undefer_group

from app.core.audit import FirmEvent, RequestMeta, TenantEvent, write_firm_audit, write_tenant_audit
from app.core.auth import (
    TOTP_OK,
    Principal,
    create_session,
    delete_user_sessions,
    effective_role,
    find_session_by_token,
    highest_firm_role,
    load_firm_memberships,
    load_memberships,
    rotate_session,
)
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
from app.tenancy.models import CREDENTIAL_GROUP, FirmMembership, Membership, Role, Tenant, User


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
    principal: Principal | None = None


@dataclass
class CodeResult:
    outcome: Outcome
    token: str | None = None
    recovery_codes: list[str] | None = None


NEXT_TOTP_ENROL = "totp_enrol"
NEXT_LOGIN = "login"


@dataclass
class ActivationResult:
    outcome: Outcome
    next: str | None = None  # NEXT_TOTP_ENROL (token set) or NEXT_LOGIN
    token: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def with_credentials() -> tuple:
    """Loader option: undefer the credential columns (auth service only)."""
    return (undefer_group(CREDENTIAL_GROUP),)


def user_by_email(db: Session, email: str) -> User | None:
    return db.execute(
        select(User).options(*with_credentials()).where(User.email == email)
    ).scalar_one_or_none()


def user_with_credentials(db: Session, user_id: UUID) -> User | None:
    return db.execute(
        select(User).options(*with_credentials()).where(User.id == user_id)
    ).scalar_one_or_none()


def firm_id_for(
    firm_memberships: list[FirmMembership], tenant_firm_ids: dict[UUID, UUID]
) -> UUID | None:
    """The firm to key a firm_audit_log row on: the user's own firm, else the firm of
    their client tenants, else None."""
    if firm_memberships:
        return sorted((fm.firm_id for fm in firm_memberships), key=str)[0]
    firms = sorted(set(tenant_firm_ids.values()), key=str)
    return firms[0] if firms else None


def principal_firm_id(principal: Principal) -> UUID | None:
    return firm_id_for(principal.firm_memberships, principal.tenant_firm_ids)


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
    """The caller has already applied the IP throttle (``app.core.throttle``)."""
    now = now or _now()
    email = email.strip().lower()
    user = user_by_email(db, email)
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
            # Never the address itself: people type passwords into that field.
            detail={
                "step": "password",
                "reason": "unknown_email",
                "email_sha256": sha256_hex(email),
            },
            meta=meta,
        )
        return LoginResult(Outcome.invalid)

    # The server now acts for this account: its own memberships become readable
    # (D-11), which is what tenant auto-selection and firm attribution need.
    set_user_context(db, user.id)
    memberships, tenant_firms = load_memberships(db, user.id)
    firm_memberships = load_firm_memberships(db, user.id)
    firm_id = firm_id_for(firm_memberships, tenant_firms)
    firm_role = highest_firm_role(firm_memberships)

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

    if firm_memberships and user.totp_enrolled_at is None:
        # D-16: a firm user enrols TOTP only through an activation link. The
        # response is the generic failure; the row records why for the admin.
        write_firm_audit(
            db,
            firm_id=firm_id,
            action=FirmEvent.login_failure,
            entity_type="user",
            entity_id=user.id,
            actor_user_id=user.id,
            actor_role=firm_role,
            detail={"step": "password", "reason": "totp_not_enrolled"},
            meta=meta,
        )
        return LoginResult(Outcome.invalid)

    _clear_failures(user)
    # A client user starts optional enrolment from a normal session; this login opens
    # a later one, which must not be shown a secret an earlier session abandoned.
    _clear_pending_totp(user)
    if old_token:
        old = find_session_by_token(db, old_token)
        if old is not None:
            db.delete(old)
            db.flush()

    active = auto_selected_tenant(memberships, tenant_firms, firm_memberships)
    token, row = create_session(
        db, user.id, active_tenant_id=active, totp_verified_at=None, now=now
    )
    principal = Principal(
        user=user,
        session=row,
        memberships=memberships,
        firm_memberships=firm_memberships,
        tenant_firm_ids=tenant_firms,
    )
    if principal.totp_state == TOTP_OK:
        _complete_login(db, principal, method="password", meta=meta)
    return LoginResult(Outcome.ok, token=token, principal=principal)


def auto_selected_tenant(
    memberships: list[Membership],
    tenant_firms: dict[UUID, UUID],
    firm_memberships: list[FirmMembership],
) -> UUID | None:
    """The tenant a newly usable session opens in: the only one the user can enter,
    else None (the switcher asks). One rule for password login and for the
    enrolment-only session that becomes a normal one at TOTP confirm."""
    accessible = [
        m.tenant_id
        for m in memberships
        if effective_role(m, tenant_firms.get(m.tenant_id), firm_memberships) is not None
    ]
    return accessible[0] if len(accessible) == 1 else None


def _complete_login(db: Session, principal: Principal, *, method: str, meta: RequestMeta) -> None:
    """The session is now fully usable: record it, and the auto-selected tenant."""
    row = principal.session
    write_firm_audit(
        db,
        firm_id=principal_firm_id(principal),
        action=FirmEvent.login_success,
        entity_type="user",
        entity_id=principal.user.id,
        actor_user_id=principal.user.id,
        actor_role=principal.firm_role,
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
            actor_user_id=principal.user.id,
            actor_role=principal.role_in(row.active_tenant_id),
            detail={"via": "auto"},
            meta=meta,
        )


def logout(db: Session, principal: Principal, *, meta: RequestMeta) -> None:
    write_firm_audit(
        db,
        firm_id=principal_firm_id(principal),
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


def _clear_pending_totp(user: User) -> None:
    """Drop an unconfirmed secret (no-op once enrolled). A pending secret belongs to
    one enrolment session: every event that ends that session, or opens a later one,
    calls this, so an abandoned secret is never shown again."""
    if user.totp_enrolled_at is None:
        user.totp_secret_enc = None
        user.totp_key_id = None
        user.totp_last_counter = None


def start_totp_enrolment(db: Session, principal: Principal, *, issuer: str) -> tuple[str, str]:
    """Return ``(secret, otpauth_uri)`` for the pending enrolment: the only time the
    secret leaves the server. 409 if already enrolled.

    Idempotent while an enrolment is pending: a repeated call (refresh, double
    click, a doubled mount effect) returns the stored secret; a new one is generated
    only when none is pending. The user row is locked first, so two concurrent calls
    return the same secret: the second waits, then reads what the first stored.

    Reachable by a firm user only from the enrolment-only session an activation
    link creates (password login refuses a firm user without TOTP), and by a
    client user from any authenticated session (optional TOTP)."""
    user = db.execute(
        select(User)
        .options(*with_credentials())
        .where(User.id == principal.user.id)
        .with_for_update()
        .execution_options(populate_existing=True)  # re-read under the lock
    ).scalar_one()
    if user.totp_enrolled_at is not None:
        raise AuthError(409, "TOTP is already enrolled; ask a firm admin to reset it")
    if user.totp_secret_enc is not None:
        secret = _totp_secret(user)
    else:
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
    if _is_locked(user, now):
        return Outcome.locked, None
    counter = match_totp(_totp_secret(user), code, now)
    last = user.totp_last_counter
    if counter is None or (last is not None and counter <= last):
        _record_failure(
            db,
            user,
            firm_id=principal_firm_id(principal),
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
    db: Session,
    principal: Principal,
    *,
    method: str,
    now: datetime,
    meta: RequestMeta,
    fresh_expiry: bool = False,
) -> str:
    """Rotate the session id, mark it verified, and complete the login if it was not."""
    was_verified = principal.session.totp_verified_at is not None
    token, row = rotate_session(
        db, principal.session, totp_verified_at=now, now=now, fresh_expiry=fresh_expiry
    )
    principal.session = row
    if not was_verified:
        _complete_login(db, principal, method=method, meta=meta)
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
        firm_id=principal_firm_id(principal),
        action=FirmEvent.totp_enrolled,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=principal.firm_role,
        detail={"recovery_codes_issued": len(codes)},
        meta=meta,
    )
    # An enrolment-only session (15 min) becomes a normal one now. It was opened by a
    # link with no tenant; apply login's auto-selection before the rotation copies
    # the row, so ``_complete_login`` records ``tenant_enter`` in this transaction.
    if principal.session.active_tenant_id is None:
        principal.session.active_tenant_id = auto_selected_tenant(
            principal.memberships, principal.tenant_firm_ids, principal.firm_memberships
        )
    token = _verified_session(
        db, principal, method="password+totp", now=now, meta=meta, fresh_expiry=True
    )
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
    firm_id = principal_firm_id(principal)
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
    db: Session,
    actor: Principal | None,
    target: User,
    *,
    firm_id: UUID | None,
    meta: RequestMeta,
    via: str,
) -> None:
    """Clear enrolment. Ends all of the target's sessions. The caller issues the new
    activation link (``app.auth.admin.reset_totp_and_issue_link``)."""
    target.totp_secret_enc = None
    target.totp_key_id = None
    target.totp_enrolled_at = None
    target.totp_last_counter = None
    target.recovery_code_hashes = None
    delete_user_sessions(db, target.id)
    write_firm_audit(
        db,
        firm_id=firm_id,
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


def check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(422, f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def _set_password(db: Session, user: User, password: str, *, now: datetime) -> None:
    user.password_hash = hash_password(password)
    user.password_changed_at = now
    user.activation_token_hash = None
    user.activation_expires_at = None
    _clear_failures(user)
    db.flush()


def change_password(
    db: Session,
    principal: Principal,
    *,
    current_password: str,
    new_password: str,
    meta: RequestMeta,
    now: datetime | None = None,
) -> bool:
    """False if the current password is wrong. On success every session of the
    user ends, the caller's included (F02.1)."""
    now = now or _now()
    check_new_password(new_password)
    user = user_with_credentials(db, principal.user.id) or principal.user
    if not verify_password(user.password_hash, current_password):
        return False
    _set_password(db, user, new_password, now=now)
    delete_user_sessions(db, user.id)
    write_firm_audit(
        db,
        firm_id=principal_firm_id(principal),
        action=FirmEvent.password_changed,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=principal.firm_role,
        detail={"via": "self"},
        meta=meta,
    )
    return True


# --- activation links (D-16) ----------------------------------------------------------------------


def issue_activation_link(
    db: Session,
    actor: Principal | None,
    target: User,
    *,
    firm_id: UUID | None,
    meta: RequestMeta,
    via: str = "api",
    now: datetime | None = None,
) -> str:
    """One-time link for the target, returned to the caller to hand over out of band
    (no e-mail). Overwrites any earlier unredeemed link (which stops working), drops
    a pending TOTP secret, and ends the target's sessions."""
    now = now or _now()
    s = get_settings()
    token = new_token()
    target.activation_token_hash = sha256_hex(token)
    target.activation_expires_at = now + timedelta(hours=s.activation_link_ttl_hours)
    _clear_pending_totp(target)
    delete_user_sessions(db, target.id)
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.activation_link_issued,
        entity_type="user",
        entity_id=target.id,
        actor_user_id=actor.user.id if actor else None,
        actor_role=actor.firm_role if actor else None,
        detail={"expires_at": target.activation_expires_at.isoformat(), "via": via},
        meta=meta,
    )
    db.flush()
    # The token rides in the URL fragment: browsers never send a fragment to any
    # server, so it stays out of access logs and referrers; the page posts it in a body.
    return f"{s.app_base_url.rstrip('/')}/activate#token={token}"


def redeem_activation_link(
    db: Session,
    *,
    token: str,
    new_password: str,
    meta: RequestMeta,
    now: datetime | None = None,
) -> ActivationResult:
    """Set the password and consume the link. For a firm user without TOTP, open an
    enrolment-only session (``ENROL_SESSION_TTL_MINUTES``) so the same flow enrols
    TOTP and shows the recovery codes; otherwise the user signs in normally.

    An unknown, used or expired link is one generic failure, persisted as a
    ``login_failure`` (step ``link``) so it counts toward the IP throttle. Neither
    the token nor its hash is stored in ``detail``. The caller has applied the
    throttle already."""
    now = now or _now()
    check_new_password(new_password)
    user = db.execute(
        select(User)
        .options(*with_credentials())
        .where(User.activation_token_hash == sha256_hex(token))
    ).scalar_one_or_none()
    expires = None if user is None else user.activation_expires_at
    if user is None or expires is None or expires <= now:
        write_firm_audit(
            db,
            firm_id=None,
            action=FirmEvent.login_failure,
            entity_type="activation_link",
            entity_id=None,
            actor_user_id=None,
            actor_role=None,
            detail={"step": "link", "reason": "invalid_or_expired"},
            meta=meta,
        )
        return ActivationResult(Outcome.invalid)

    set_user_context(db, user.id)  # proof of the link: act for the account
    _, tenant_firms = load_memberships(db, user.id)
    firm_memberships = load_firm_memberships(db, user.id)
    firm_id = firm_id_for(firm_memberships, tenant_firms)
    _set_password(db, user, new_password, now=now)
    _clear_pending_totp(user)
    delete_user_sessions(db, user.id)
    write_firm_audit(
        db,
        firm_id=firm_id,
        action=FirmEvent.password_changed,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        actor_role=highest_firm_role(firm_memberships),
        detail={"via": "activation_link"},
        meta=meta,
    )
    if firm_memberships and user.totp_enrolled_at is None:
        token, _ = create_session(
            db,
            user.id,
            active_tenant_id=None,
            totp_verified_at=None,
            now=now,
            ttl=timedelta(minutes=get_settings().enrol_session_ttl_minutes),
        )
        return ActivationResult(Outcome.ok, next=NEXT_TOTP_ENROL, token=token)
    return ActivationResult(Outcome.ok, next=NEXT_LOGIN)


# --- tenant selection -----------------------------------------------------------------------------


def set_active_tenant(
    db: Session, principal: Principal, tenant_id: UUID, *, meta: RequestMeta
) -> Role:
    """Re-check access, switch the session, record ``tenant_enter``. 403 (and a
    rollback, so the session is unchanged) without an effective role there: no
    membership, or an orphan entry row."""
    memberships, tenant_firms = load_memberships(db, principal.user.id)
    principal.memberships = memberships
    principal.tenant_firm_ids = tenant_firms
    role = principal.role_in(tenant_id)
    if role is None:
        raise AuthError(403, "no membership in that tenant")
    principal.session.active_tenant_id = tenant_id
    db.flush()
    set_tenant_context(db, tenant_id)
    write_tenant_audit(
        db,
        tenant_id=tenant_id,
        action=TenantEvent.tenant_enter,
        entity_type="tenant",
        entity_id=tenant_id,
        actor_user_id=principal.user.id,
        actor_role=role,
        detail={"via": "switcher"},
        meta=meta,
    )
    return role


def tenants_for(db: Session, principal: Principal) -> list[tuple[Tenant, Role]]:
    """Tenants the user can enter, with the effective role in each. Orphan entry
    rows are absent."""
    ids = principal.accessible_tenant_ids()
    if not ids:
        return []
    tenants = {t.id: t for t in db.execute(select(Tenant).where(Tenant.id.in_(ids))).scalars()}
    out: list[tuple[Tenant, Role]] = []
    for m in principal.memberships:
        t = tenants.get(m.tenant_id)
        role = principal.role_in(m.tenant_id)
        if t is not None and role is not None:
            out.append((t, role))
    return out
