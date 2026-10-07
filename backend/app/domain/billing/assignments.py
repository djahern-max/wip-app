"""A person assigns an earlier invoice, credit-memo or sales-receipt line to a work area,
reassigns it, or clears it (F08.1 Part 1; D-45). Every write takes ids: the line's row id
and the work area's row id. No name reaches a comparison here; the suggestion by name is
computed in ``work_areas.py`` for the response only and comes back, if a person presses
"Confirm all as suggested", as pairs of ids (the owner's answer 4, 2026-10-07). Each
write is one appended row of ``billing_line_work_area`` and exactly one ``audit_log``
row. Refusals are one sentence and write nothing. Requires ``app.tenant_id`` on the
session (RLS).
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.domain.billing.board import load_board
from app.domain.billing.models import BillingLineWorkArea
from app.domain.billing.work_areas import AssignmentIn, LineTie
from app.domain.config.audit import Actor, audit
from app.domain.jobs import service
from app.domain.jobs.service import Conflict, Invalid, NotFound

NOT_ON_JOB = "That line is not on a document of this job."


def _job_lines(db: Session, tenant_id: UUID, job_id: UUID) -> tuple[service.JobView, list[LineTie]]:
    view = service.job_detail(db, tenant_id, job_id)
    board = load_board(db, tenant_id, [view], other=False)
    return view, list(board.work_areas[view.job.id].lines)


def _line(lines: Sequence[LineTie], billing_line_id: UUID) -> LineTie:
    found = next((t for t in lines if t.line.id == str(billing_line_id)), None)
    if found is None:
        raise NotFound(NOT_ON_JOB)
    return found


def _where(t: LineTie) -> str:
    return f"line {t.line.line_no} of {t.doc.doc.doc_number or t.doc.doc.external_id}"


def _assignable(t: LineTie) -> None:
    if not t.offered:
        raise Invalid(f"{_where(t).capitalize()} cannot be assigned: {t.not_offered}")
    if t.how == "number":
        raise Invalid(
            f"{_where(t).capitalize()} begins with #{t.area.order_no} and is tied to work area "
            f"#{t.area.order_no} by its number; an assignment cannot change it."
        )


def _fields(a: AssignmentIn | None, estimate_number: str | None = None) -> dict:
    if a is None:
        return {"work_area": None}
    return {
        "work_area": {
            "estimate": estimate_number,
            "estimate_id": a.estimate_id,
            "order_no": a.order_no,
            "name": a.work_area_name,
        }
    }


def _rows(row: BillingLineWorkArea, t: LineTie) -> dict:
    return {
        "billing_line_work_area": str(row.id),
        "billing_line": t.line.id,
        "document": t.doc.doc.doc_number or t.doc.doc.external_id,
        "estimate_work_area": str(row.estimate_work_area_id) if row.estimate_work_area_id else None,
    }


def _assign(
    db: Session,
    tenant_id: UUID,
    view: service.JobView,
    t: LineTie,
    work_area_id: UUID,
    note: str | None,
    actor: Actor,
) -> BillingLineWorkArea:
    _assignable(t)
    found = service._find_area(db, tenant_id, view.job, work_area_id)
    area, est = found.area, found.estimate
    if not area.kept:
        raise Invalid(
            f"Work area #{area.order_no} is omitted; a line cannot be assigned to an omitted "
            "work area."
        )
    current = t.assignment
    if current is not None and current.key == (str(est.id), area.order_no):
        raise Conflict(
            f"{_where(t).capitalize()} is already assigned to work area #{area.order_no}."
        )
    row = BillingLineWorkArea(
        tenant_id=tenant_id,
        billing_line_id=UUID(t.line.id),
        action="assigned",
        estimate_id=est.id,
        order_no=area.order_no,
        work_area_name=area.name,
        estimate_work_area_id=area.id,
        note=service._clean(note, 2000),
        recorded_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    numbers = {str(a.estimate.id): a.estimate.external_id for a in view.attached}
    audit(
        db,
        tenant_id,
        TenantEvent.billing_line_reassigned if current else TenantEvent.billing_line_assigned,
        "job",
        view.job.id,
        actor,
        before=_fields(current, numbers.get(current.estimate_id) if current else None),
        after={
            **_fields(
                AssignmentIn(
                    str(row.id),
                    t.line.id,
                    str(est.id),
                    area.order_no,
                    area.name,
                    str(actor.user_id),
                    row.recorded_at,
                ),
                est.external_id,
            ),
            "note": row.note,
        },
        rows=_rows(row, t),
    )
    return row


def assign_line(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    billing_line_id: UUID,
    work_area_id: UUID,
    *,
    note: str | None,
    actor: Actor,
) -> BillingLineWorkArea:
    """One line to one work area, for its whole amount (D-45)."""
    view, lines = _job_lines(db, tenant_id, job_id)
    return _assign(db, tenant_id, view, _line(lines, billing_line_id), work_area_id, note, actor)


def clear_line(
    db: Session, tenant_id: UUID, job_id: UUID, billing_line_id: UUID, *, actor: Actor
) -> BillingLineWorkArea:
    """The line counts in no work area's billed to date again; a ``cleared`` row."""
    view, lines = _job_lines(db, tenant_id, job_id)
    t = _line(lines, billing_line_id)
    if t.how == "number":
        _assignable(t)
    if t.assignment is None:
        raise Conflict(f"{_where(t).capitalize()} is not assigned to a work area.")
    row = BillingLineWorkArea(
        tenant_id=tenant_id,
        billing_line_id=UUID(t.line.id),
        action="cleared",
        recorded_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    numbers = {str(a.estimate.id): a.estimate.external_id for a in view.attached}
    audit(
        db,
        tenant_id,
        TenantEvent.billing_line_cleared,
        "job",
        view.job.id,
        actor,
        before=_fields(t.assignment, numbers.get(t.assignment.estimate_id)),
        after=_fields(None),
        rows=_rows(row, t),
    )
    return row


def assign_pairs(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    pairs: Sequence[tuple[UUID, UUID]],
    *,
    actor: Actor,
) -> int:
    """ "Confirm all as suggested": the pairs the screen showed as suggested, as ids, one
    row and one audit row each; a bad pair refuses the whole request and writes nothing
    (the transaction is the request's)."""
    view, lines = _job_lines(db, tenant_id, job_id)
    seen: set[UUID] = set()
    for line_id, _area in pairs:
        if line_id in seen:
            raise Invalid("A line is named twice in the request.")
        seen.add(line_id)
    for line_id, area_id in pairs:
        t = _line(lines, line_id)
        if t.area is not None:
            raise Conflict(
                f"{_where(t).capitalize()} is already tied to work area #{t.area.order_no}."
            )
        _assign(db, tenant_id, view, t, area_id, None, actor)
    return len(pairs)
