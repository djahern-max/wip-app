"""``sync_run`` bookkeeping (F03). Created and closed here; nothing calls an API
until F05. ``cursor`` is JSONB (plan call 3) and holds whatever position the
source needs to resume from; ``error`` is short text, never a payload.

Cursor rules (F03 close-out): timestamps inside a cursor are ISO-8601 **UTC
strings** made by ``iso_utc`` (``2026-09-17T14:03:09Z``), never ``datetime``
objects; ``finish_sync_run`` refuses a cursor that holds one. The cursor to resume
from is ``latest_successful_cursor``: the cursor of the latest run that
**succeeded** for that connection and kind. A later failed run never advances it.
"""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.models import SyncRun


def iso_utc(moment: datetime) -> str:
    """``datetime`` → ISO-8601 UTC string with a ``Z`` suffix, second precision.
    Naive datetimes are refused: a cursor must not depend on the machine's zone."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("cursor timestamps must be timezone-aware")
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check_cursor(value: Any, path: str = "$") -> None:
    if isinstance(value, datetime | date):
        raise TypeError(f"cursor timestamp at {path} must be an ISO-8601 UTC string (iso_utc)")
    if isinstance(value, dict):
        for k, v in value.items():
            _check_cursor(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _check_cursor(v, f"{path}[{i}]")


def start_sync_run(db: Session, tenant_id: UUID, connection_id: UUID, kind: str) -> SyncRun:
    row = SyncRun(tenant_id=tenant_id, connection_id=connection_id, kind=kind)
    db.add(row)
    db.flush()
    return row


def finish_sync_run(
    db: Session,
    run: SyncRun,
    *,
    outcome: str,
    records_fetched: int = 0,
    records_stored: int = 0,
    cursor: dict | None = None,
    error: str | None = None,
) -> SyncRun:
    if outcome not in ("succeeded", "failed"):
        raise ValueError("outcome must be succeeded or failed")
    _check_cursor(cursor)
    run.finished_at = datetime.now(UTC)
    run.outcome = outcome
    run.records_fetched = records_fetched
    run.records_stored = records_stored
    run.cursor = cursor
    run.error = None if error is None else error[:500]
    db.flush()
    return run


def latest_successful_cursor(db: Session, connection_id: UUID, kind: str) -> dict | None:
    """The cursor to resume from, or ``None`` when no run of this kind has succeeded.
    Requires the tenant context (RLS)."""
    return db.execute(
        select(SyncRun.cursor)
        .where(
            SyncRun.connection_id == connection_id,
            SyncRun.kind == kind,
            SyncRun.outcome == "succeeded",
        )
        .order_by(SyncRun.finished_at.desc(), SyncRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
