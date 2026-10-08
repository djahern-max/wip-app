"""The queue's reads and a person's actions (F09; D-22, D-46; the owner's answers A to C,
2026-10-08). Requires ``app.tenant_id`` on the session (RLS). Refusals are one sentence
with the HTTP status the API answers with. Assign, dismiss and reopen write one event
and one audit row each; a note writes its event and no audit row (it changes no state;
the event row is the record). Nobody resolves an exception by hand: ``resolved`` is set
only by the run (``run.py``).

``dismissed_keys`` is the one statement a page needs to leave dismissed exceptions out
of the board, Home and the Estimates list (Plan answer 5): bounded by the number of
dismissed exceptions, never a query per row. ``open_counts`` is the firm count (Plan
answer 6): one short tenant-scoped transaction per company, never two contexts in one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.core.db import tenant_session
from app.domain.billing.models import Customer
from app.domain.config.audit import Actor, audit
from app.domain.estimates.exceptions import Issue
from app.domain.estimates.models import Estimate
from app.domain.exceptions.models import (
    EVENT_LABELS,
    SEVERITIES,
    SEVERITY_LABELS,
    STATUS_LABELS,
    ExceptionEvent,
    ReviewException,
)
from app.domain.exceptions.registry import Key, identity, may_dismiss
from app.domain.exceptions.run import REFRESH_KIND
from app.domain.jobs.models import Job
from app.tenancy.models import Membership, User
from app.worker.models import Task

SEVERITY_ORDER = {"block_close": 0, "warn": 1, "info": 2}
STATUS_ORDER = {"open": 0, "dismissed": 1, "resolved": 2}


class ExceptionError(Exception):
    """One sentence for the person (D-22) and the HTTP status the API answers with."""

    status = 422


class NotFound(ExceptionError):
    status = 404


class Conflict(ExceptionError):
    status = 409


class Invalid(ExceptionError):
    status = 422


# --- what the pages need -------------------------------------------------------------------


def dismissed_keys(db: Session) -> frozenset[Key]:
    """Every dismissed exception's identity, in one statement."""
    rows = db.execute(
        select(
            ReviewException.subject_type,
            ReviewException.subject_id,
            ReviewException.code,
            ReviewException.item_key,
        ).where(ReviewException.status == "dismissed")
    ).all()
    return frozenset((t, str(s), c, k) for t, s, c, k in rows)


def without_dismissed(
    issues: Sequence[Issue], subject_type: str, subject_id: UUID | str, dismissed: frozenset[Key]
) -> list[Issue]:
    """The sentences a page shows as needs: the subject's issues less the dismissed ones."""
    if not dismissed:
        return list(issues)
    sid = str(subject_id)
    return [i for i in issues if identity(subject_type, sid, i) not in dismissed]


@dataclass(frozen=True)
class DismissedView:
    id: str
    code: str
    message: str  # the sentence as last raised
    note: str
    dismissed_by: str | None
    dismissed_at: str


def dismissed_for(
    db: Session, subject_type: str, subject_id: UUID, estimate_ids: Sequence[UUID] = ()
) -> list[DismissedView]:
    """The subject's dismissed exceptions with their latest dismissal note (the job page
    and the estimate page list them under "Dismissed"); two statements. A job page passes
    its attached estimates' ids too, so an estimate's dismissed sentence shows on the job."""
    own = (ReviewException.subject_type == subject_type) & (
        ReviewException.subject_id == subject_id
    )
    where = own
    if estimate_ids:
        where = own | (
            (ReviewException.subject_type == "estimate")
            & ReviewException.subject_id.in_(list(estimate_ids))
        )
    rows = list(
        db.execute(
            select(ReviewException)
            .where(where, ReviewException.status == "dismissed")
            .order_by(ReviewException.dismissed_at, ReviewException.id)
        ).scalars()
    )
    if not rows:
        return []
    notes: dict[UUID, ExceptionEvent] = {}
    for e in db.execute(
        select(ExceptionEvent)
        .where(
            ExceptionEvent.exception_id.in_([r.id for r in rows]),
            ExceptionEvent.kind == "dismissed",
        )
        .order_by(ExceptionEvent.occurred_at, ExceptionEvent.id)
    ).scalars():
        notes[e.exception_id] = e  # the latest wins
    users = _user_names(db, {r.dismissed_by for r in rows})
    return [
        DismissedView(
            id=str(r.id),
            code=r.code,
            message=r.message,
            note=notes[r.id].text if r.id in notes and notes[r.id].text else "",
            dismissed_by=users.get(r.dismissed_by),
            dismissed_at=r.dismissed_at.isoformat() if r.dismissed_at else "",
        )
        for r in rows
    ]


# --- the queue ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SubjectRef:
    type: str  # job | estimate | customer
    id: str
    label: str  # the job's name, the estimate's number, the customer row's name


@dataclass(frozen=True)
class EventView:
    id: str
    kind: str
    kind_label: str
    occurred_at: str
    actor: str | None  # None: the run
    assigned_to: str | None
    text: str | None
    detail: dict | None


@dataclass(frozen=True)
class ExceptionView:
    id: str
    code: str
    severity: str
    severity_label: str
    message: str
    subject: SubjectRef
    status: str
    status_label: str
    may_dismiss: bool
    assigned_to_id: str | None
    assigned_to: str | None
    first_raised_at: str
    last_raised_at: str
    resolved_at: str | None
    dismissed_at: str | None
    dismissed_by: str | None
    events: tuple[EventView, ...] = ()


@dataclass(frozen=True)
class Member:
    id: str
    name: str


def _user_names(db: Session, ids: set[UUID | None]) -> dict[UUID, str]:
    wanted = [i for i in ids if i is not None]
    if not wanted:
        return {}
    return {
        u.id: u.display_name for u in db.execute(select(User).where(User.id.in_(wanted))).scalars()
    }


def _subjects(db: Session, rows: Sequence[ReviewException]) -> dict[tuple[str, str], SubjectRef]:
    by_type: dict[str, set[UUID]] = {}
    for r in rows:
        by_type.setdefault(r.subject_type, set()).add(r.subject_id)
    out: dict[tuple[str, str], SubjectRef] = {}
    if by_type.get("job"):
        for j in db.execute(select(Job).where(Job.id.in_(by_type["job"]))).scalars():
            out[("job", str(j.id))] = SubjectRef("job", str(j.id), j.name)
    if by_type.get("estimate"):
        for e in db.execute(select(Estimate).where(Estimate.id.in_(by_type["estimate"]))).scalars():
            out[("estimate", str(e.id))] = SubjectRef("estimate", str(e.id), e.external_id)
    if by_type.get("customer"):
        for c in db.execute(select(Customer).where(Customer.id.in_(by_type["customer"]))).scalars():
            out[("customer", str(c.id))] = SubjectRef("customer", str(c.id), c.display_name)
    return out


def _iso(d: datetime | None) -> str | None:
    return None if d is None else d.isoformat()


def _view(
    r: ReviewException,
    subjects: dict[tuple[str, str], SubjectRef],
    users: dict[UUID, str],
    events: Sequence[ExceptionEvent] = (),
) -> ExceptionView:
    subject = subjects.get(
        (r.subject_type, str(r.subject_id)), SubjectRef(r.subject_type, str(r.subject_id), "")
    )
    return ExceptionView(
        id=str(r.id),
        code=r.code,
        severity=r.severity,
        severity_label=SEVERITY_LABELS[r.severity],
        message=r.message,
        subject=subject,
        status=r.status,
        status_label=STATUS_LABELS[r.status],
        may_dismiss=may_dismiss(r.severity),
        assigned_to_id=str(r.assigned_to) if r.assigned_to else None,
        assigned_to=users.get(r.assigned_to) if r.assigned_to else None,
        first_raised_at=r.first_raised_at.isoformat(),
        last_raised_at=r.last_raised_at.isoformat(),
        resolved_at=_iso(r.resolved_at),
        dismissed_at=_iso(r.dismissed_at),
        dismissed_by=users.get(r.dismissed_by) if r.dismissed_by else None,
        events=tuple(
            EventView(
                id=str(e.id),
                kind=e.kind,
                kind_label=EVENT_LABELS[e.kind],
                occurred_at=e.occurred_at.isoformat(),
                actor=users.get(e.actor_user_id) if e.actor_user_id else None,
                assigned_to=users.get(e.assigned_to) if e.assigned_to else None,
                text=e.text,
                detail=e.detail,
            )
            for e in events
        ),
    )


def order_key(r: ReviewException) -> tuple:
    """Open first, then by severity (block-close, warn, info), then by age (oldest first)."""
    return (STATUS_ORDER[r.status], SEVERITY_ORDER[r.severity], r.first_raised_at, str(r.id))


def list_exceptions(
    db: Session,
    *,
    status: str | None = None,
    severity: str | None = None,
    assigned_to: UUID | None = None,
) -> list[ExceptionView]:
    stmt = select(ReviewException)
    if status:
        stmt = stmt.where(ReviewException.status == status)
    if severity:
        stmt = stmt.where(ReviewException.severity == severity)
    if assigned_to is not None:
        stmt = stmt.where(ReviewException.assigned_to == assigned_to)
    rows = sorted(db.execute(stmt).scalars(), key=order_key)
    subjects = _subjects(db, rows)
    users = _user_names(db, {r.assigned_to for r in rows} | {r.dismissed_by for r in rows})
    return [_view(r, subjects, users) for r in rows]


def open_counts_here(db: Session) -> dict[str, int]:
    """The open count by severity in the session's tenant (one statement)."""
    counts = {s: 0 for s in SEVERITIES}
    for severity, n in db.execute(
        select(ReviewException.severity, func.count())
        .where(ReviewException.status == "open")
        .group_by(ReviewException.severity)
    ).all():
        counts[severity] = int(n)
    return counts


def open_counts(engine: Engine, tenant_id: UUID) -> dict[str, int]:
    """The firm count for one company: its own short transaction with its own context
    (D-19's pattern); the caller loops over the companies the person may enter."""
    with tenant_session(engine, tenant_id) as db:
        return open_counts_here(db)


def last_run_at(db: Session) -> datetime | None:
    """When the tenant's exceptions were last refreshed: the latest succeeded run's
    finish time from the worker's own task row; None before the first run."""
    return db.execute(
        select(func.max(Task.finished_at)).where(
            Task.kind == REFRESH_KIND, Task.status == "succeeded"
        )
    ).scalar_one()


def members(db: Session) -> list[Member]:
    """Everyone who can open the company: the tenant's membership rows (a firm user's
    entry row included), by display name."""
    rows = db.execute(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .order_by(func.lower(User.display_name))
    ).scalars()
    return [Member(str(u.id), u.display_name) for u in rows]


def _row(db: Session, exception_id: UUID) -> ReviewException:
    row = db.get(ReviewException, exception_id)
    if row is None:
        raise NotFound("That exception does not exist.")
    return row


def get_exception(db: Session, exception_id: UUID) -> ExceptionView:
    row = _row(db, exception_id)
    events = list(
        db.execute(
            select(ExceptionEvent)
            .where(ExceptionEvent.exception_id == row.id)
            .order_by(ExceptionEvent.occurred_at, ExceptionEvent.id)
        ).scalars()
    )
    users = _user_names(
        db,
        {row.assigned_to, row.dismissed_by}
        | {e.actor_user_id for e in events}
        | {e.assigned_to for e in events},
    )
    return _view(row, _subjects(db, [row]), users, events)


# --- a person's actions ---------------------------------------------------------------------


def _clean(text: str | None, limit: int = 2000) -> str | None:
    if text is None:
        return None
    value = " ".join(text.split())
    return value[:limit] if value else None


def _fields(row: ReviewException) -> dict:
    return {
        "status": row.status,
        "assigned_to": str(row.assigned_to) if row.assigned_to else None,
        "code": row.code,
        "severity": row.severity,
        "subject_type": row.subject_type,
        "subject_id": str(row.subject_id),
        "item_key": row.item_key,
    }


def _person_event(
    db: Session,
    row: ReviewException,
    kind: str,
    actor: Actor,
    now: datetime,
    *,
    text: str | None = None,
    assigned_to: UUID | None = None,
) -> None:
    db.add(
        ExceptionEvent(
            tenant_id=row.tenant_id,
            exception_id=row.id,
            kind=kind,
            occurred_at=now,
            actor_user_id=actor.user_id,
            assigned_to=assigned_to,
            text=text,
        )
    )
    db.flush()


def assign(
    db: Session, tenant_id: UUID, exception_id: UUID, user_id: UUID | None, actor: Actor
) -> ExceptionView:
    """To a member of the company, or to nobody; one event, one audit row."""
    row = _row(db, exception_id)
    if user_id is not None:
        member = db.execute(
            select(Membership).where(Membership.user_id == user_id)
        ).scalar_one_or_none()
        if member is None:
            raise Invalid("That user is not a member of this company; assign to a member.")
    if row.assigned_to == user_id:
        raise Conflict(
            "The exception is already assigned to that person."
            if user_id
            else "The exception is already assigned to nobody."
        )
    before = _fields(row)
    row.assigned_to = user_id
    now = datetime.now(UTC)
    _person_event(db, row, "assigned", actor, now, assigned_to=user_id)
    audit(
        db,
        tenant_id,
        TenantEvent.exception_assigned,
        "exception",
        row.id,
        actor,
        before=before,
        after=_fields(row),
    )
    return get_exception(db, row.id)


def add_note(
    db: Session, tenant_id: UUID, exception_id: UUID, text: str | None, actor: Actor
) -> ExceptionView:
    """Any number of notes, kept in order with who and when; no audit row."""
    row = _row(db, exception_id)
    note = _clean(text)
    if note is None:
        raise Invalid("Write the note: an empty note is not kept.")
    _person_event(db, row, "note", actor, datetime.now(UTC), text=note)
    return get_exception(db, row.id)


def dismiss(
    db: Session, tenant_id: UUID, exception_id: UUID, note: str | None, actor: Actor
) -> ExceptionView:
    """The owner's answer A (D-46): an info or warn exception, open, with a note."""
    row = _row(db, exception_id)
    if not may_dismiss(row.severity):
        raise Conflict(
            "This exception blocks the period close and cannot be dismissed; it closes when "
            "its cause is gone (D-46)."
        )
    if row.status != "open":
        raise Conflict(f"The exception is {STATUS_LABELS[row.status].lower()}, not open.")
    text = _clean(note)
    if text is None:
        raise Invalid("Give the note: an exception is dismissed only with a note (D-46).")
    before = _fields(row)
    now = datetime.now(UTC)
    row.status = "dismissed"
    row.dismissed_at = now
    row.dismissed_by = actor.user_id
    _person_event(db, row, "dismissed", actor, now, text=text)
    audit(
        db,
        tenant_id,
        TenantEvent.exception_dismissed,
        "exception",
        row.id,
        actor,
        before=before,
        after=_fields(row),
    )
    return get_exception(db, row.id)


def reopen(db: Session, tenant_id: UUID, exception_id: UUID, actor: Actor) -> ExceptionView:
    row = _row(db, exception_id)
    if row.status != "dismissed":
        raise Conflict(f"The exception is {STATUS_LABELS[row.status].lower()}, not dismissed.")
    before = _fields(row)
    now = datetime.now(UTC)
    row.status = "open"
    row.dismissed_at = None
    row.dismissed_by = None
    _person_event(db, row, "reopened", actor, now)
    audit(
        db,
        tenant_id,
        TenantEvent.exception_reopened,
        "exception",
        row.id,
        actor,
        before=before,
        after=_fields(row),
    )
    return get_exception(db, row.id)
