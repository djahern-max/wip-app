"""Audit writers (D-12). The row is added to the caller's transaction, so it commits
or rolls back together with the action it records. Failed logins, which have no
action transaction, use the request transaction on their own.

``detail`` must never carry a password, TOTP secret or code, recovery code,
session id, activation token or its hash, or an attempted e-mail address in
clear; the writers refuse those keys outright.

Client IP (F02.1): taken from ``X-Forwarded-For`` only when ``TRUSTED_PROXY_COUNT``
says a proxy is present (the N-th address from the right); otherwise, and when
the header is missing or shorter than N entries, from the socket.
"""

import enum
from dataclasses import dataclass
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.audit.models import AuditLog, FirmAuditLog
from app.core.config import get_settings
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
        "token_hash",
        "activation_token",
        "activation_token_hash",
        "session_id",
        "cookie",
        "email",  # F02.1: an attempted address is stored only as email_sha256
        "access_token",  # F03: connection tokens are never written anywhere in clear
        "refresh_token",
        "payload",  # F03: source data belongs in raw_record, not in an audit row
    }
)


class FirmEvent(enum.StrEnum):
    login_success = "login_success"
    login_failure = "login_failure"
    account_locked = "account_locked"
    ip_throttled = "ip_throttled"
    logout = "logout"
    totp_enrolled = "totp_enrolled"
    totp_reset = "totp_reset"
    recovery_code_used = "recovery_code_used"
    activation_link_issued = "activation_link_issued"
    password_changed = "password_changed"
    user_created = "user_created"
    tenant_created = "tenant_created"
    firm_membership_created = "firm_membership_created"
    firm_membership_role_changed = "firm_membership_role_changed"
    firm_membership_removed = "firm_membership_removed"


class TenantEvent(enum.StrEnum):
    tenant_enter = "tenant_enter"
    membership_created = "membership_created"
    membership_role_changed = "membership_role_changed"
    membership_removed = "membership_removed"
    # F03
    import_uploaded = "import_uploaded"
    import_duplicate = "import_duplicate"
    import_downloaded = "import_downloaded"
    connection_tokens_set = "connection_tokens_set"


@dataclass(frozen=True)
class RequestMeta:
    ip: str | None = None
    request_id: str | None = None


def client_ip(request: Request) -> str | None:
    socket_ip = request.client.host if request.client else None
    n = get_settings().trusted_proxy_count
    if n <= 0:
        return socket_ip
    header = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in header.split(",") if h.strip()]
    if len(hops) < n:
        return socket_ip
    return hops[-n]


def request_meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip=client_ip(request),
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
