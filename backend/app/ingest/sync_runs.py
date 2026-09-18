"""``sync_run`` bookkeeping (F03). Created and closed here; nothing calls an API
until F05. ``cursor`` is JSONB (plan call 3) and holds whatever position the
source needs to resume from; ``error`` is short text, never a payload."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.ingest.models import SyncRun


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
    run.finished_at = datetime.now(UTC)
    run.outcome = outcome
    run.records_fetched = records_fetched
    run.records_stored = records_stored
    run.cursor = cursor
    run.error = None if error is None else error[:500]
    db.flush()
    return run
