"""Queue operations (D-19; plan call 1). Every function takes a session that already
carries the tenant context (``tenant_session``); RLS scopes each statement to that
tenant, so a claim can never pick another tenant's row.

Lease: claiming marks the task ``running`` with ``locked_until = now + lease`` and
increments ``attempts``. An expired lease makes the task claimable again; one that
expires at ``max_attempts`` is marked ``failed`` by ``sweep_expired``. Completion
and failure writes are conditional on still holding the lease (``locked_by`` =
this worker and ``status = 'running'``): a worker whose lease was reclaimed writes
nothing and its caller logs "lease lost". Backoff between attempts is
``base × 2^(attempt−1)`` capped (settings); no heartbeat, no jitter.

``enqueue`` sends ``pg_notify`` inside the transaction, so the worker wakes only
if the enqueue commits. The payload is the tenant id and nothing else.

Error text stored on a task (``last_error``) is the exception type, or the
SQLSTATE and primary message for a database error, never the exception's own
message: a parser's message may echo file content.

A task raises ``PermanentTaskError`` for a failure that no retry can fix (a source's
``parse`` raising, a batch already failed): the task is ``failed`` at once, attempts
unchanged. Every other exception retries with backoff up to ``max_attempts``.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import describe_db_error
from app.worker.models import DEDUPE_INDEX_WHERE, Task

log = logging.getLogger("app.worker")

NOTIFY_CHANNEL = "wip_tasks"


class PermanentTaskError(Exception):
    """No retry: the cause is in the data, not in the environment. ``reason`` is a
    short code stored as ``last_error``; the cause's type is appended when given."""

    def __init__(self, reason: str, cause: BaseException | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, PermanentTaskError):
        return exc.reason if exc.cause is None else f"{exc.reason}: {type(exc.cause).__name__}"
    if isinstance(exc, DBAPIError):
        return describe_db_error(exc)[:500]
    return type(exc).__name__


def backoff_delay(attempt: int) -> timedelta:
    """Delay before the retry after the ``attempt``-th failure (1-based)."""
    s = get_settings()
    seconds = min(
        s.task_backoff_base_seconds * (2 ** max(attempt - 1, 0)), s.task_backoff_cap_seconds
    )
    return timedelta(seconds=seconds)


def enqueue(
    db: Session,
    tenant_id: UUID,
    kind: str,
    payload: dict,
    *,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int | None = None,
) -> Task | None:
    """Insert a task; ``None`` when a non-terminal task with the same ``dedupe_key``
    exists. Wakes the worker on commit."""
    stmt = (
        insert(Task)
        .values(
            tenant_id=tenant_id,
            kind=kind,
            payload=payload,
            status="queued",
            attempts=0,
            max_attempts=max_attempts or get_settings().task_max_attempts,
            run_after=run_after or func.now(),
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "kind", "dedupe_key"],
            index_where=text(DEDUPE_INDEX_WHERE),
        )
        .returning(Task)
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is not None:
        notify(db, tenant_id)
    return row


def open_task(db: Session, kind: str, dedupe_key: str) -> Task | None:
    """The queued or running task holding this dedupe key, if any."""
    return db.execute(
        select(Task).where(
            Task.kind == kind,
            Task.dedupe_key == dedupe_key,
            Task.status.in_(("queued", "running")),
        )
    ).scalar_one_or_none()


def notify(db: Session, tenant_id: UUID) -> None:
    db.execute(
        text("SELECT pg_notify(:channel, :tenant)"),
        {"channel": NOTIFY_CHANNEL, "tenant": str(tenant_id)},
    )


def sweep_expired(db: Session) -> int:
    """Expired leases on tasks already at ``max_attempts`` become ``failed``."""
    result = db.execute(
        update(Task)
        .where(
            Task.status == "running",
            Task.locked_until < func.now(),
            Task.attempts >= Task.max_attempts,
        )
        .values(
            status="failed",
            last_error="lease expired at max_attempts",
            finished_at=func.now(),
            locked_by=None,
            locked_until=None,
        )
    )
    return result.rowcount


def claim_one(db: Session, *, worker: str, lease_seconds: int) -> Task | None:
    """At most one due task of this tenant, ``FOR UPDATE SKIP LOCKED``."""
    candidate = (
        select(Task.id)
        .where(
            or_(
                and_(Task.status == "queued", Task.run_after <= func.now()),
                and_(Task.status == "running", Task.locked_until < func.now()),
            ),
            Task.attempts < Task.max_attempts,
        )
        .order_by(Task.run_after, Task.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(Task)
        .where(Task.id == candidate)
        .values(
            status="running",
            attempts=Task.attempts + 1,
            locked_by=worker,
            locked_until=func.now() + timedelta(seconds=lease_seconds),
        )
        .returning(Task)
    )
    return db.execute(stmt).scalar_one_or_none()


def _held(task_id: UUID, worker: str):
    return and_(Task.id == task_id, Task.locked_by == worker, Task.status == "running")


def complete(db: Session, task_id: UUID, *, worker: str) -> bool:
    """``True`` when this worker still held the lease and the task is now succeeded."""
    result = db.execute(
        update(Task)
        .where(_held(task_id, worker))
        .values(status="succeeded", finished_at=func.now(), locked_by=None, locked_until=None)
    )
    return result.rowcount == 1


def fail(db: Session, task_id: UUID, *, worker: str, error: str, permanent: bool = False) -> bool:
    """Retry with backoff, or ``failed`` at ``max_attempts`` or when ``permanent``.
    ``False`` = lease lost."""
    row = db.execute(
        select(Task).where(_held(task_id, worker)).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return False
    if permanent or row.attempts >= row.max_attempts:
        row.status = "failed"
        row.finished_at = datetime.now(UTC)
    else:
        row.status = "queued"
        row.run_after = datetime.now(UTC) + backoff_delay(row.attempts)
    row.last_error = error[:500]
    row.locked_by = None
    row.locked_until = None
    db.flush()
    return True
