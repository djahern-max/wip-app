"""Per-IP throttle for the unauthenticated credential routes (F02.1).

Counted from ``firm_audit_log`` (no new table): ``IP_THROTTLE_FAILURES`` rows with
``action = 'login_failure'`` and this IP inside the last ``IP_THROTTLE_MINUTES``
throttle the IP for the rest of the window. The first throttled attempt writes one
``ip_throttled`` row; while that row is inside the window every further attempt
returns 429 and **writes nothing**, so an anonymous caller cannot grow an
insert-only table without bound.

The check runs before any argon2 verification (owner answer to call 5); the
routes call it first and return 429 without touching the service.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import FirmAuditLog
from app.core.audit import FirmEvent, RequestMeta, write_firm_audit
from app.core.config import get_settings


def is_throttled(db: Session, meta: RequestMeta, now: datetime) -> bool:
    """True if the caller's IP is throttled. Writes the single ``ip_throttled``
    row when the threshold is first crossed; otherwise writes nothing."""
    if meta.ip is None:
        return False
    s = get_settings()
    since = now - timedelta(minutes=s.ip_throttle_minutes)
    already = db.execute(
        select(FirmAuditLog.id)
        .where(
            FirmAuditLog.action == str(FirmEvent.ip_throttled),
            FirmAuditLog.ip == meta.ip,
            FirmAuditLog.occurred_at > since,
        )
        .limit(1)
    ).first()
    if already is not None:
        return True
    failures = db.execute(
        select(func.count())
        .select_from(FirmAuditLog)
        .where(
            FirmAuditLog.action == str(FirmEvent.login_failure),
            FirmAuditLog.ip == meta.ip,
            FirmAuditLog.occurred_at > since,
        )
    ).scalar_one()
    if failures < s.ip_throttle_failures:
        return False
    write_firm_audit(
        db,
        firm_id=None,
        action=FirmEvent.ip_throttled,
        entity_type="ip",
        entity_id=None,
        actor_user_id=None,
        actor_role=None,
        detail={
            "failures": failures,
            "window_minutes": s.ip_throttle_minutes,
            "until": (now + timedelta(minutes=s.ip_throttle_minutes)).isoformat(),
        },
        meta=meta,
    )
    return True
