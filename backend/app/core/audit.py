"""Audit writers (D-12). The row is added to the caller's transaction, so it commits
or rolls back together with the action it records. Failed logins, which have no
action transaction, use the request transaction on their own.

``detail`` must never carry a password, TOTP secret or code, recovery code, or
session id; the writers refuse those keys outright.
"""

import enum
from dataclasses import dataclass
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.audit.models import AuditLog, FirmAuditLog
from app.tenancy.models import Role

FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "password_hash",
        "secret",
        "totp_secret",
        "code",
        "totp_code",
        "recovery_code",
        "recovery_codes",
        "token",
        "session_id",
        "cookie",
    }
)


class FirmEvent(enum.StrEnum):
    login_success = "login_success"
    login_failure = "login_failure"
    account_locked = "account_locked"
    logout = "logout"
    totp_enrolled = "totp_enrolled"
    totp_reset = "totp_reset"
    recovery_code_used = "recovery_code_used"
    password_reset_issued = "password_reset_issued"
    password_changed = "password_changed"
    user_created = "user_created"


class TenantEvent(enum.StrEnum):
    tenant_enter = "tenant_enter"
    membership_created = "membership_created"
    membership_role_changed = "membership_role_changed"
    membership_removed = "membership_removed"


@dataclass(frozen=True)
class RequestMeta:
    ip: str | None = None
    request_id: str | None = None


def request_meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
    )


def _check_detail(detail: dict | None) -> None:
    if not detail:
        return
    bad = {k for k in detail if k.lower() in FORBIDDEN_DETAIL_KEYS}
    if bad:
        raise ValueError(f"audit detail must not contain {sorted(bad)}")


def write_tenant_audit(
    db: Session,
    *,
    tenant_id: UUID,
    action: TenantEvent,
    entity_type: str,
    entity_id: UUID | str | None,
    actor_user_id: UUID | None,
    actor_role: Role | None,
    detail: dict | None = None,
    meta: RequestMeta | None = None,
) -> AuditLog:
    """Requires ``app.tenant_id`` = ``tenant_id`` on the transaction (RLS)."""
    _check_detail(detail)
    meta = meta or RequestMeta()
    row = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        action=str(action),
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        detail=detail,
        ip=meta.ip,
        request_id=meta.request_id,
    )
    db.add(row)
    db.flush()
    return row


def write_firm_audit(
    db: Session,
    *,
    firm_id: UUID | None,
    action: FirmEvent,
    entity_type: str,
    entity_id: UUID | str | None,
    actor_user_id: UUID | None,
    actor_role: Role | None,
    detail: dict | None = None,
    meta: RequestMeta | None = None,
) -> FirmAuditLog:
    _check_detail(detail)
    meta = meta or RequestMeta()
    row = FirmAuditLog(
        firm_id=firm_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        action=str(action),
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        detail=detail,
        ip=meta.ip,
        request_id=meta.request_id,
    )
    db.add(row)
    db.flush()
    return row
