"""A person assigns an earlier invoice, credit-memo or sales-receipt line to a work area,
splits it across work areas by amount (F08.4, D-52), reassigns it, or clears it (F08.1
Part 1; D-45). Every write takes ids: the line's row id and the work areas' row ids. No
name reaches a comparison here; the suggestion by name is computed in ``work_areas.py``
for the response only and comes back, if a person presses "Confirm all as suggested", as
pairs of ids (the owner's answer 4, 2026-10-07). Each write is one **event**: the rows of
``billing_line_work_area`` appended in this request (one per part, sharing
``recorded_at``) and exactly one ``audit_log`` row per line acted on. Refusals are one
sentence and write nothing. Requires ``app.tenant_id`` on the session (RLS).
"""

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.domain.billing.board import load_board
from app.domain.billing.figures import ZERO, words
from app.domain.billing.models import BillingLineWorkArea
from app.domain.billing.work_areas import AreaRef, AssignmentIn, LineTie
from app.domain.config.audit import Actor, audit
from app.domain.jobs import service
from app.domain.jobs.service import Conflict, Invalid, NotFound

NOT_ON_JOB = "That line is not on a document of this job."

Part = tuple[UUID, Decimal]  # (estimate_work_area_id, signed amount)


def _job_lines(db: Session, tenant_id: UUID, job_id: UUID) -> tuple[service.JobView, list[LineTie]]:
    view = service.job_detail(db, tenant_id, job_id)
    board = load_board(db, tenant_id, [view], other=False)
    return view, list(board.work_areas[view.job.id].lines)


def _line(lines: Sequence[LineTie], billing_line_id: UUID) -> LineTie:
    found = next((t for t in lines if t.line.id == str(billing_line_id)), None)
    if found is None:
        raise NotFound(NOT_ON_JOB)
    return found


def _document(t: LineTie) -> str:
    return t.doc.doc.doc_number or t.doc.doc.external_id


def _where(t: LineTie) -> str:
    return f"line {t.line.line_no} of {_document(t)}"


def _cap(text: str) -> str:
    """The first letter up, the rest as it is (``str.capitalize`` would lowercase a
    document number)."""
    return text[:1].upper() + text[1:]


def _assignable(t: LineTie) -> None:
    if not t.offered:
        raise Invalid(f"{_cap(_where(t))} cannot be assigned: {t.not_offered}")
    if t.how == "number":
        raise Invalid(
            f"{_cap(_where(t))} begins with #{t.area.order_no} and is tied to work area "
            f"#{t.area.order_no} by its number; an assignment cannot change it."
        )
    if t.how == "pay_application":
        raise Invalid(
            f"{_cap(_where(t))} is on an invoice keyed from a pay application; its work "
            "areas are the application's (D-36)."
        )


def _label(order_no: int, name: str, estimate_number: str | None, on_original: bool) -> str:
    text = f"#{order_no} {name}"
    return text if on_original or not estimate_number else f"{text} ({estimate_number})"


def _parts_detail(view: service.JobView, rows: Sequence[AssignmentIn]) -> list[dict]:
    """The parts as the audit row lists them: ``{"work_area", "amount"}`` strings."""
    numbers = {str(a.estimate.id): a.estimate.external_id for a in view.attached}
    original = view.original.estimate.id if view.original else None
    return [
        {
            "work_area": _label(
                r.order_no,
                r.work_area_name,
                numbers.get(r.estimate_id),
                original is not None and r.estimate_id == str(original),
            ),
            "amount": str(r.amount),
        }
        for r in rows
    ]


def _fields(view: service.JobView, t: LineTie, rows: Sequence[AssignmentIn]) -> dict:
    """One side of the audit detail: F08.1's ``work_area`` for one row (None for none or
    for a split), and the ``parts`` list for any number of rows (D-52)."""
    numbers = {str(a.estimate.id): a.estimate.external_id for a in view.attached}
    original = str(view.original.estimate.id) if view.original else None
    rows = sorted(
        rows, key=lambda r: (r.estimate_id != original, numbers.get(r.estimate_id, ""), r.order_no)
    )
    work_area = None
    if len(rows) == 1:
        a = rows[0]
        work_area = {
            "estimate": numbers.get(a.estimate_id),
            "estimate_id": a.estimate_id,
            "order_no": a.order_no,
            "name": a.work_area_name,
        }
    return {
        "work_area": work_area,
        "parts": _parts_detail(view, rows),
        "document": _document(t),
        "line_no": t.line.line_no,
        "line_amount": str(t.amount),
    }


def _summary(t: LineTie, before: Sequence[AssignmentIn], parts: Sequence[dict]) -> str:
    """The sentence the audit page prints (Plan answer 4)."""
    head = f"{_cap(_where(t))} ({words(t.amount)})"
    listed = "; ".join(f"{p['work_area']} {words(Decimal(p['amount']))}" for p in parts)
    n = len(parts)
    if n == 0:
        if len(before) > 1:
            return f"{head} is no longer split across {len(before)} work areas."
        return f"{head} is no longer assigned to a work area."
    if n == 1:
        verb = "reassigned to" if before else "assigned to"
        return f"{head} {verb} work area {listed}."
    again = "again " if before else ""
    return f"{head} split {again}across {n} work areas: {listed}."


def _resolve(
    db: Session, tenant_id: UUID, view: service.JobView, parts: Sequence[Part]
) -> list[tuple[AreaRef, Decimal]]:
    """Each part's work area by id (kept, on an attached estimate), once each, no 0.00."""
    if not parts:
        raise Invalid("Name at least one work area.")
    seen: set[UUID] = set()
    out: list[tuple[AreaRef, Decimal]] = []
    for work_area_id, amount in parts:
        found = service._find_area(db, tenant_id, view.job, work_area_id)
        area, est, link = found.area, found.estimate, found.link
        if work_area_id in seen:
            raise Invalid(
                f"Work area #{area.order_no} is named twice; a work area may appear once in a "
                "split."
            )
        seen.add(work_area_id)
        if not area.kept:
            raise Invalid(
                f"Work area #{area.order_no} is omitted; a line cannot be assigned to an omitted "
                "work area."
            )
        if amount == ZERO:
            raise Invalid(
                f"Work area #{area.order_no} has a part of 0.00; leave a work area out of the "
                "split instead."
            )
        out.append(
            (
                AreaRef(
                    estimate_id=str(est.id),
                    estimate_number=est.external_id,
                    role=link.role,
                    work_area_id=str(area.id),
                    order_no=area.order_no,
                    name=area.name,
                    kept=area.kept,
                    price=area.price,
                    kind=area.kind,
                ),
                amount,
            )
        )
    return out


def _write(
    db: Session,
    tenant_id: UUID,
    view: service.JobView,
    t: LineTie,
    parts: Sequence[Part],
    note: str | None,
    actor: Actor,
) -> list[BillingLineWorkArea]:
    """One event: the rows of the parts, then one audit row for the line."""
    _assignable(t)
    resolved = _resolve(db, tenant_id, view, parts)
    resolved.sort(key=lambda r: (r[0].role != "original", r[0].estimate_number, r[0].order_no))
    if len(resolved) > 1:
        total = sum((amount for _area, amount in resolved), ZERO)
        if total != t.amount:
            raise Invalid(
                f"The parts sum to {words(total)} and the line is {words(t.amount)}; the parts "
                "must sum to the line exactly."
            )
    current = t.assignments
    wanted = [(area.key, amount) for area, amount in resolved]
    if current and sorted(wanted) == sorted((a.key, a.amount) for a in current):
        if len(wanted) == 1:
            raise Conflict(
                f"{_cap(_where(t))} is already assigned to work area #{resolved[0][0].order_no}."
            )
        raise Conflict(f"{_cap(_where(t))} is already split that way.")
    cleaned = service._clean(note, 2000)
    rows = [
        BillingLineWorkArea(
            tenant_id=tenant_id,
            billing_line_id=UUID(t.line.id),
            action="assigned",
            estimate_id=UUID(area.estimate_id),
            order_no=area.order_no,
            work_area_name=area.name,
            estimate_work_area_id=UUID(area.work_area_id),
            note=cleaned,
            recorded_by=actor.user_id,
            amount=amount,
        )
        for area, amount in resolved
    ]
    db.add_all(rows)
    db.flush()
    after_rows = [
        AssignmentIn(
            str(r.id),
            t.line.id,
            str(r.estimate_id),
            r.order_no,
            r.work_area_name,
            str(actor.user_id),
            r.recorded_at,
            r.amount,
            r.note,
        )
        for r in rows
    ]
    if len(rows) > 1:
        event = TenantEvent.billing_line_split
    elif current:
        event = TenantEvent.billing_line_reassigned
    else:
        event = TenantEvent.billing_line_assigned
    after = {**_fields(view, t, after_rows), "note": cleaned}
    audit(
        db,
        tenant_id,
        event,
        "job",
        view.job.id,
        actor,
        before=_fields(view, t, current),
        after=after,
        rows={
            "billing_line_work_area": [str(r.id) for r in rows],
            "billing_line": t.line.id,
            "document": _document(t),
            "estimate_work_area": [str(r.estimate_work_area_id) for r in rows],
        },
        summary=_summary(t, current, after["parts"]),
    )
    return rows


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
    t = _line(lines, billing_line_id)
    return _write(db, tenant_id, view, t, [(work_area_id, t.amount)], note, actor)[0]


def split_line(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    billing_line_id: UUID,
    parts: Sequence[Part],
    *,
    note: str | None,
    actor: Actor,
) -> list[BillingLineWorkArea]:
    """One line across work areas by amount, the parts summing to the line to the cent
    (D-52); one row per part, one audit row for the line. One part is a plain
    assignment of the whole line and is refused when its amount is not the line's."""
    view, lines = _job_lines(db, tenant_id, job_id)
    t = _line(lines, billing_line_id)
    if len(parts) == 1 and parts[0][1] != t.amount:
        raise Invalid(
            f"The parts sum to {words(parts[0][1])} and the line is {words(t.amount)}; the "
            "parts must sum to the line exactly."
        )
    return _write(db, tenant_id, view, t, parts, note, actor)


def clear_line(
    db: Session, tenant_id: UUID, job_id: UUID, billing_line_id: UUID, *, actor: Actor
) -> BillingLineWorkArea:
    """The line counts in no work area's billed to date again; one ``cleared`` row ends
    every current row (a split's parts together)."""
    view, lines = _job_lines(db, tenant_id, job_id)
    t = _line(lines, billing_line_id)
    if t.how in ("number", "pay_application"):
        _assignable(t)
    if not t.assignments:
        raise Conflict(f"{_cap(_where(t))} is not assigned to a work area.")
    row = BillingLineWorkArea(
        tenant_id=tenant_id,
        billing_line_id=UUID(t.line.id),
        action="cleared",
        recorded_by=actor.user_id,
        amount=t.amount,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.billing_line_cleared,
        "job",
        view.job.id,
        actor,
        before=_fields(view, t, t.assignments),
        after=_fields(view, t, ()),
        rows={
            "billing_line_work_area": [str(row.id)],
            "billing_line": t.line.id,
            "document": _document(t),
            "estimate_work_area": [],
        },
        summary=_summary(t, t.assignments, []),
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
        if t.parts:
            tied = t.area.order_no if t.area else "more than one"
            raise Conflict(f"{_cap(_where(t))} is already tied to work area #{tied}.")
        _write(db, tenant_id, view, t, [(area_id, t.amount)], None, actor)
    return len(pairs)
