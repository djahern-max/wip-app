"""The run (F09, Plan answer 1; D-19, D-46): one worker task per tenant that calls the
generators through ``collect`` and persists what they return. Idempotent: a second run
with nothing changed writes nothing (no row, no event, no audit row).

For each sentence raised: a new identity opens an exception (``raised``); an open one
has its sentence, detail and severity refreshed when they differ (no event); a resolved
one opens again as the same exception (``raised_again``; ``first_raised_at`` stays); a
dismissed one stays dismissed while what it states is unchanged and opens again when
that changes (``reopened``, no actor, the before and after in the event's detail, the
earlier ``dismissed`` event kept; the owner's answer B). An open or dismissed exception
the generators no longer raise becomes ``resolved`` (``resolved``), the only way one is
resolved. ``last_raised_at`` is written when the row changes; the queue's "as of" is the
last succeeded run's time from the worker's own ``task`` row.

Triggers (``request_refresh``): the QuickBooks ``normalize`` task when it applied a
change, the import ``process_batch`` task when it loaded rows, and the API's after-write
hook on a person's state-changing request. Dedupe key ``refresh`` collapses a burst into
one queued run; while a run is running the next request queues under ``refresh:next``,
so a change applied during a run is caught by the run after it. A page read never runs
it. The run writes no audit row: its effect is in the rows and their events.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.domain.exceptions.collect import Raised, collect
from app.domain.exceptions.models import ExceptionEvent, ReviewException
from app.domain.exceptions.registry import Key, item_key, stated
from app.worker.queue import enqueue, open_task
from app.worker.registry import task

log = logging.getLogger("app.exceptions")

REFRESH_KIND = "exceptions.refresh"
DEDUPE = "refresh"
DEDUPE_NEXT = "refresh:next"


@dataclass
class RunResult:
    raised: int = 0
    raised_again: int = 0
    reopened: int = 0
    resolved: int = 0
    refreshed: int = 0  # open rows whose sentence, detail or severity was refreshed

    @property
    def writes(self) -> int:
        return self.raised + self.raised_again + self.reopened + self.resolved + self.refreshed


def _event(
    db: Session, row: ReviewException, kind: str, now: datetime, detail: dict | None = None
) -> None:
    db.add(
        ExceptionEvent(
            tenant_id=row.tenant_id,
            exception_id=row.id,
            kind=kind,
            occurred_at=now,
            actor_user_id=None,
            detail=detail,
        )
    )


def _plain(detail: dict) -> dict:
    """The generator's detail as JSON holds it (strings, numbers, lists; a Decimal is
    already a string in every generator)."""
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in detail.items()}


def refresh(db: Session, tenant_id: UUID, *, now: datetime | None = None) -> RunResult:
    """One run for one tenant, inside the caller's tenant-scoped transaction."""
    now = now or datetime.now(UTC)
    result = RunResult()
    raised: dict[Key, Raised] = {}
    for r in collect(db, tenant_id):
        raised.setdefault(r.key, r)  # one exception per identity (EST_UNKNOWN_COST_CODE)
    existing: dict[Key, ReviewException] = {
        row.key: row for row in db.execute(select(ReviewException)).scalars()
    }
    for key, r in raised.items():
        row = existing.get(key)
        detail = _plain(r.issue.detail)
        if row is None:
            row = ReviewException(
                tenant_id=tenant_id,
                code=r.issue.code,
                severity=r.severity,
                subject_type=r.subject_type,
                subject_id=UUID(r.subject_id),
                item_key=item_key(r.issue),
                message=r.issue.message,
                detail=detail,
                status="open",
                first_raised_at=now,
                last_raised_at=now,
            )
            db.add(row)
            db.flush()
            _event(db, row, "raised", now)
            result.raised += 1
            continue
        changed = (
            row.message != r.issue.message or row.detail != detail or row.severity != r.severity
        )
        if row.status == "resolved":
            row.status = "open"
            row.resolved_at = None
            row.last_raised_at = now
            _event(db, row, "raised_again", now)
            result.raised_again += 1
        elif row.status == "dismissed":
            before, after = stated(row.code, row.detail), stated(row.code, detail)
            if before != after:
                row.status = "open"
                row.dismissed_at = None
                row.dismissed_by = None
                row.last_raised_at = now
                _event(db, row, "reopened", now, {"before": before, "after": after})
                result.reopened += 1
        elif changed:
            result.refreshed += 1
        if changed:
            row.message = r.issue.message
            row.detail = detail
            row.severity = r.severity
            row.last_raised_at = now
    for key, row in existing.items():
        if key in raised or row.status == "resolved":
            continue
        row.status = "resolved"
        row.resolved_at = now
        row.dismissed_at = None
        row.dismissed_by = None
        _event(db, row, "resolved", now)
        result.resolved += 1
    db.flush()
    return result


@task(REFRESH_KIND)
def refresh_task(tenant_id: UUID, *, engine: Engine) -> None:
    from app.core.db import tenant_session

    with tenant_session(engine, tenant_id) as db:
        result = refresh(db, tenant_id)
    log.info(
        "exceptions refresh raised=%d again=%d reopened=%d resolved=%d refreshed=%d tenant=%s",
        result.raised,
        result.raised_again,
        result.reopened,
        result.resolved,
        result.refreshed,
        tenant_id,
    )


def request_refresh(db: Session, tenant_id: UUID) -> None:
    """Queue one run for the tenant in the caller's transaction (``pg_notify`` goes with
    the commit). A run already queued takes the change; one already running is followed
    by one more (``refresh:next``), never by two."""
    running = open_task(db, REFRESH_KIND, DEDUPE)
    if running is not None and running.status == "running":
        enqueue(db, tenant_id, REFRESH_KIND, {}, dedupe_key=DEDUPE_NEXT)
        return
    enqueue(db, tenant_id, REFRESH_KIND, {}, dedupe_key=DEDUPE)
