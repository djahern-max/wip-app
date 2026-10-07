"""Pay applications: the reads and the three writes (F08.1 Part 2; D-26, D-36, D-39,
D-42, D-43). Every function requires ``app.tenant_id`` on the session (RLS). A draft is
made from a billing request and replaced by the next request until it is issued; issue
freezes the surcharge rate, billed before and the amount due on the row (the owner's yes
on Plan answer 5) and void closes it with a reason; an issued application's lines and
figures are never changed by any route. One audit row per draft, issue and void. Only a
``fixed_price`` job has pay applications (D-24). The platform writes nothing to
QuickBooks: the controller keys the invoice from the application and the tie is read.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.domain.billing.figures import ZERO, DocFigures, JobFigures
from app.domain.billing.models import PayApplication, PayApplicationLine
from app.domain.billing.pay_applications import (
    ApplicationView,
    Draft,
    NotOnSchedule,
    PreviousIn,
    RequestIn,
    StoredLine,
    Summary,
    billed_before,
    draft_lines,
    earned,
    line_views,
    match_invoice,
    next_number,
    schedule,
    summary,
)
from app.domain.billing.work_areas import AreaKey, AreaRef, JobWorkAreas
from app.domain.config.audit import Actor, audit
from app.domain.config.policy import FUEL_SURCHARGE_TREATMENT, POLICY_KEYS, surcharge_treatment
from app.domain.jobs import service
from app.domain.jobs.service import Conflict, Invalid, JobView, NotFound

NOT_FIXED_PRICE = "Only a fixed-price job has pay applications (D-24)."
_RATE_KEY = POLICY_KEYS[FUEL_SURCHARGE_TREATMENT].label
RATE_UNDECIDED = (
    f"Decide the fuel surcharge rate on the policy key {_RATE_KEY} before issuing an "
    "application with the surcharge (D-39)."
)
CHOICE_UNANSWERED = (
    "Say whether the fuel surcharge applies to this application before issuing it (D-39)."
)


# --- reads -------------------------------------------------------------------------------------


def load_applications(
    db: Session, job_ids: Sequence[UUID]
) -> dict[UUID, list[tuple[PayApplication, list[PayApplicationLine]]]]:
    """Per job, its applications in number order, each with its lines. Two reads."""
    if not job_ids:
        return {}
    apps = list(
        db.execute(
            select(PayApplication)
            .where(PayApplication.job_id.in_(job_ids))
            .order_by(PayApplication.job_id, PayApplication.number)
        ).scalars()
    )
    lines: dict[UUID, list[PayApplicationLine]] = {a.id: [] for a in apps}
    if apps:
        for ln in db.execute(
            select(PayApplicationLine)
            .where(PayApplicationLine.pay_application_id.in_(list(lines)))
            .order_by(PayApplicationLine.order_no)
        ).scalars():
            lines[ln.pay_application_id].append(ln)
    out: dict[UUID, list[tuple[PayApplication, list[PayApplicationLine]]]] = {}
    for a in apps:
        out.setdefault(a.job_id, []).append((a, lines[a.id]))
    return out


def _stored_lines(
    rows: Sequence[PayApplicationLine], areas: dict[AreaKey, AreaRef], numbers: dict[str, str]
) -> list[StoredLine]:
    out = []
    for ln in rows:
        key = (str(ln.estimate_id), ln.order_no)
        ref = areas.get(key)
        out.append(
            StoredLine(
                estimate_id=str(ln.estimate_id),
                estimate_number=numbers.get(
                    str(ln.estimate_id), ref.estimate_number if ref else ""
                ),
                role=ref.role if ref else "original",
                work_area_id=str(ln.estimate_work_area_id),
                order_no=ln.order_no,
                name=ln.work_area_name,
                scheduled_value=ln.scheduled_value,
                percent=ln.percent_complete,
            )
        )
    out.sort(key=lambda s: (s.role != "original", s.estimate_number, s.order_no))
    return out


def previous_for(
    number: int,
    earlier: Sequence[tuple[PayApplication, list[PayApplicationLine]]],
    wa: JobWorkAreas,
) -> dict[AreaKey, PreviousIn]:
    """Per work area, what the latest earlier issued application says (its percent and
    earned to date), else what the "#n" and assigned lines billed on it (D-45; a void
    application says nothing)."""
    out: dict[AreaKey, PreviousIn] = {
        key: PreviousIn(None, amount) for key, amount in wa.billed_lines.items()
    }
    for app, rows in sorted(earlier, key=lambda ar: ar[0].number):
        if app.status != "issued" or app.number >= number:
            continue
        for ln in rows:
            key = (str(ln.estimate_id), ln.order_no)
            out[key] = PreviousIn(
                ln.percent_complete, earned(ln.scheduled_value, ln.percent_complete), app.number
            )
    return out


def _approved_on(view: JobView) -> dict[AreaKey, date]:
    return {
        (str(eid), order): a.latest.agreed_on
        for (eid, order), a in view.approvals.items()
        if a.applies and a.latest.agreed_on is not None
    }


def application_views(
    view: JobView,
    areas: Sequence[AreaRef],
    wa: JobWorkAreas,
    apps: Sequence[tuple[PayApplication, list[PayApplicationLine]]],
    figures: JobFigures,
    rate: Decimal | None,
    rate_decided: bool,
    users: dict[UUID, str],
) -> list[ApplicationView]:
    """Every application of the job as shown and printed; the issued ones from their
    frozen figures, a draft from today's rows."""
    by_key = {a.key: a for a in areas}
    numbers = {str(a.estimate.id): a.estimate.external_id for a in view.attached}
    original = view.original
    estimate_number = original.estimate.external_id if original else ""
    estimate_numbers = frozenset(numbers.values())
    out: list[ApplicationView] = []
    for app, rows in apps:
        prev = previous_for(app.number, apps, wa)
        lines = line_views(_stored_lines(rows, by_key, numbers), prev)
        earned_total = sum((ln.earned_to_date for ln in lines), ZERO)
        undecided = False
        if app.status == "draft":
            before = billed_before(
                figures.documents, app.application_date, estimate_numbers, app.number
            )
            undecided = before is None
            s = summary(earned_total, before or ZERO, app.surcharge_applies, rate)
        else:
            s = summary(
                earned_total, app.billed_before or ZERO, app.surcharge_applies, app.surcharge_rate
            )
        v = ApplicationView(
            id=str(app.id),
            number=app.number,
            estimate_number=estimate_number,
            application_date=app.application_date,
            status=app.status,
            surcharge_applies=app.surcharge_applies,
            lines=tuple(lines),
            summary=s,
            billed_before_undecided=undecided,
            rate_undecided=bool(
                app.surcharge_applies and app.status == "draft" and not rate_decided
            ),
            created_by=users.get(app.created_by),
            created_at=app.created_at.isoformat(),
            issued_by=users.get(app.issued_by) if app.issued_by else None,
            issued_at=app.issued_at.isoformat() if app.issued_at else None,
            voided_by=users.get(app.voided_by) if app.voided_by else None,
            voided_at=app.voided_at.isoformat() if app.voided_at else None,
            void_reason=app.void_reason,
        )
        inv = match_invoice(v, figures.documents) if app.status == "issued" else None
        out.append(ApplicationView(**{**v.__dict__, "invoice": inv}))
    return out


def rate_of(db: Session) -> tuple[Decimal | None, bool]:
    """The tenant's surcharge rate and whether it is decided (D-39: no default)."""
    treatment = surcharge_treatment(db)
    if treatment is None:
        return None, False
    return treatment.rate, treatment.rate is not None


# --- writes ------------------------------------------------------------------------------------


def _job_view(db: Session, tenant_id: UUID, job_id: UUID) -> JobView:
    view = service.job_detail(db, tenant_id, job_id)
    if view.job.revenue_method != "fixed_price":
        raise Invalid(NOT_FIXED_PRICE)
    return view


def _fields(app: PayApplication) -> dict:
    return {
        "number": app.number,
        "status": app.status,
        "application_date": app.application_date.isoformat(),
        "surcharge_applies": app.surcharge_applies,
        "surcharge_rate": str(app.surcharge_rate) if app.surcharge_rate is not None else None,
        "billed_before": str(app.billed_before) if app.billed_before is not None else None,
        "amount_due": str(app.amount_due) if app.amount_due is not None else None,
    }


def draft_application(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    request: RequestIn,
    *,
    areas: Sequence[AreaRef],
    wa: JobWorkAreas,
    documents: Sequence[DocFigures],
    estimate_numbers: frozenset[str],
    actor: Actor,
) -> tuple[PayApplication, Draft]:
    """A draft from a billing request (D-26): the job's open draft is replaced, an issued
    application is never touched. The request's exceptions come back with the draft."""
    view = _job_view(db, tenant_id, job_id)
    apps = load_applications(db, [job_id]).get(job_id, [])
    existing = next((a for a, _rows in apps if a.status == "draft"), None)
    number = (
        existing.number
        if existing
        else next_number(
            (d.doc.doc_number for d in documents if not d.doc.deleted),
            estimate_numbers,
            (a.number for a, _rows in apps),
        )
    )
    listed = schedule(areas, request.application_date, _approved_on(view))
    prev = previous_for(number, apps, wa)
    try:
        draft = draft_lines(request, listed, areas, prev)
    except NotOnSchedule as exc:
        raise Invalid(str(exc)) from None
    if existing is None:
        app = PayApplication(
            tenant_id=tenant_id,
            job_id=job_id,
            number=number,
            application_date=request.application_date,
            status="draft",
            surcharge_applies=request.surcharge_applies,
            created_by=actor.user_id,
        )
        db.add(app)
        db.flush()
        before = None
    else:
        before = _fields(existing)
        app = existing
        app.application_date = request.application_date
        app.surcharge_applies = request.surcharge_applies
        db.execute(
            delete(PayApplicationLine).where(PayApplicationLine.pay_application_id == app.id)
        )
        db.flush()
    for ln in draft.lines:
        db.add(
            PayApplicationLine(
                tenant_id=tenant_id,
                pay_application_id=app.id,
                estimate_id=UUID(ln.area.estimate_id),
                order_no=ln.area.order_no,
                work_area_name=ln.area.name,
                estimate_work_area_id=UUID(ln.area.work_area_id),
                scheduled_value=ln.area.price,
                percent_complete=ln.percent,
            )
        )
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.pay_application_drafted,
        "job",
        job_id,
        actor,
        before=before,
        after={
            **_fields(app),
            "lines": len(draft.lines),
            "exceptions": [i.code for i in draft.issues],
        },
        rows={"pay_application": str(app.id)},
    )
    return app, draft


def _application(db: Session, job_id: UUID, application_id: UUID) -> PayApplication:
    app = db.get(PayApplication, application_id)
    if app is None or app.job_id != job_id:
        raise NotFound("That pay application is not on this job.")
    return app


def issue_application(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    application_id: UUID,
    *,
    view_of: ApplicationView,
    actor: Actor,
) -> PayApplication:
    """Issue: the surcharge choice must be answered and, when it applies, the rate decided
    (refused in words, D-39); the rate, billed before and the amount due are frozen on
    the row; one audit row. ``view_of``: the draft as computed today."""
    _job_view(db, tenant_id, job_id)
    app = _application(db, job_id, application_id)
    if app.status != "draft":
        raise Conflict(f"Pay application {app.number} is {app.status}; only a draft is issued.")
    if app.surcharge_applies is None:
        raise Invalid(CHOICE_UNANSWERED)
    if app.surcharge_applies and view_of.rate_undecided:
        raise Invalid(RATE_UNDECIDED)
    if view_of.billed_before_undecided:
        raise Invalid(
            "Billed to date before this application waits for the policy keys the board needs "
            "(Configuration, Policy)."
        )
    if not view_of.lines:
        raise Invalid("The application lists no work area; enter the billing request first.")
    before = _fields(app)
    app.status = "issued"
    app.issued_by = actor.user_id
    app.issued_at = datetime.now(UTC)
    app.surcharge_rate = view_of.summary.surcharge_rate if app.surcharge_applies else None
    app.billed_before = view_of.summary.billed_before
    app.amount_due = view_of.summary.amount_due
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.pay_application_issued,
        "job",
        job_id,
        actor,
        before=before,
        after={
            **_fields(app),
            "earned_to_date": str(view_of.summary.earned_to_date),
            "surcharge": str(view_of.summary.surcharge)
            if view_of.summary.surcharge is not None
            else None,
            "total_to_invoice": str(view_of.summary.total_to_invoice)
            if view_of.summary.total_to_invoice is not None
            else None,
            "invoice": view_of.invoice_number,
        },
        rows={"pay_application": str(app.id)},
    )
    return app


def void_application(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    application_id: UUID,
    *,
    reason: str | None,
    actor: Actor,
) -> PayApplication:
    """Void with a reason (D-36: a correction is void and a new application); one audit row."""
    _job_view(db, tenant_id, job_id)
    app = _application(db, job_id, application_id)
    text = service._clean(reason, 2000)
    if text is None:
        raise Invalid("Give the reason: a pay application is voided only with a reason.")
    if app.status == "void":
        raise Conflict(f"Pay application {app.number} is already void.")
    before = _fields(app)
    app.status = "void"
    app.voided_by = actor.user_id
    app.voided_at = datetime.now(UTC)
    app.void_reason = text
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.pay_application_voided,
        "job",
        job_id,
        actor,
        before=before,
        after={**_fields(app), "reason": text},
        rows={"pay_application": str(app.id)},
    )
    return app


def _summary_words(s: Summary) -> str:  # pragma: no cover - a helper for logs and tests
    return f"earned {s.earned_to_date} before {s.billed_before} due {s.amount_due}"
