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


class MemoryThrottle:
    """Per-IP window kept in the process (F05.1) for the two unauthenticated QuickBooks
    routes, whose refused requests must **store nothing** (a bad webhook signature, a
    disconnect page hit for an unknown realm), so the audit-log count above cannot
    serve them. Same numbers (``IP_THROTTLE_FAILURES`` in ``IP_THROTTLE_MINUTES``);
    resets on restart; per process, and there is one. ``should_log`` is true once
    per window per IP, so a flood of bad signatures is one log line."""

    def __init__(self) -> None:
        self._failures: dict[str, list[datetime]] = {}
        self._logged_at: dict[str, datetime] = {}

    def _window(self, now: datetime) -> datetime:
        return now - timedelta(minutes=get_settings().ip_throttle_minutes)

    def refused(self, ip: str | None, now: datetime) -> bool:
        if ip is None:
            return False
        since = self._window(now)
        recent = [t for t in self._failures.get(ip, ()) if t > since]
        self._failures[ip] = recent
        return len(recent) >= get_settings().ip_throttle_failures

    def record_failure(self, ip: str | None, now: datetime) -> None:
        if ip is None:
            return
        since = self._window(now)
        self._failures[ip] = [t for t in self._failures.get(ip, ()) if t > since] + [now]

    def should_log(self, ip: str | None, now: datetime) -> bool:
        key = ip or "-"
        last = self._logged_at.get(key)
        if last is not None and last > self._window(now):
            return False
        self._logged_at[key] = now
        return True

    def reset(self) -> None:
        self._failures.clear()
        self._logged_at.clear()
