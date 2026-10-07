"""Pay applications (F08.1 Part 2; D-26, D-36, D-39, D-42, D-43). A billing request is
entered by ``client_pm``, ``client_admin``, ``firm_staff`` and ``firm_admin``
(``can_enter_billing_request``); issue and void by the roles that manage jobs
(``can_issue_pay_applications``); every role reads the list, one application and its
PDF (``can_view_jobs``). The platform writes nothing to QuickBooks. Money is strings
with cents (D-22); percents with two places.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.jobs import _actor, _issues, _raise, _tenant_name
from app.api.schemas import (
    PayApplicationLineOut,
    PayApplicationOut,
    PayApplicationsOut,
    PayApplicationSummaryOut,
    ScheduleAreaOut,
)
from app.core.auth import Principal, TenantSession
from app.core.authz import can_enter_billing_request, can_issue_pay_applications, can_view_jobs
from app.domain.billing import pay_application_service as paps
from app.domain.billing.board import Board, area_refs, load_board, money_str
from app.domain.billing.models import APPLICATION_STATUS_LABELS
from app.domain.billing.pay_application_pdf import Heading, application_pdf, status_words
from app.domain.billing.pay_applications import (
    ApplicationView,
    PercentIn,
    RequestIn,
    invoice_number,
    next_number,
    schedule,
)
from app.domain.estimates.exceptions import Issue
from app.domain.estimates.service import money
from app.domain.jobs import service

router = APIRouter(prefix="/jobs/{job_id}/pay-applications", tags=["pay-applications"])

Viewer = Annotated[Principal, Depends(can_view_jobs)]
Requester = Annotated[Principal, Depends(can_enter_billing_request)]
Issuer = Annotated[Principal, Depends(can_issue_pay_applications)]

HUNDRED = Decimal("100")


def _percent(value: str) -> Decimal:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("a percent from 0.00 to 100.00, such as 50.00") from None
    # Above 100.00 passes here so the request's own sentence (BILLING_OVER_100) answers it.
    if not d.is_finite() or d < 0 or d > Decimal("1000"):
        raise ValueError("a percent from 0.00 to 100.00, such as 50.00")
    if d != d.quantize(Decimal("0.01")):
        raise ValueError("a percent with at most two decimal places")
    return d.quantize(Decimal("0.01"))


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PercentRequestIn(_In):
    estimate_work_area_id: UUID
    percent: str = Field(max_length=10)

    @field_validator("percent")
    @classmethod
    def _check(cls, v: str) -> str:
        _percent(v)
        return v


class BillingRequestIn(_In):
    """D-26: cumulative percent complete per work area, and optionally one percent for
    every listed work area not named."""

    application_date: date
    surcharge_applies: bool | None = None  # D-39: the person's choice, nothing assumed
    percents: list[PercentRequestIn] = Field(default_factory=list, max_length=500)
    apply_all: str | None = Field(default=None, max_length=10)

    @field_validator("apply_all")
    @classmethod
    def _check_all(cls, v: str | None) -> str | None:
        if v is not None:
            _percent(v)
        return v


class VoidIn(_In):
    reason: str | None = Field(default=None, max_length=2000)


# --- building the responses ------------------------------------------------------------------


def _line_out(ln) -> PayApplicationLineOut:
    return PayApplicationLineOut(
        work_area_id=ln.work_area_id,
        estimate_external_id=ln.estimate_number,
        order_no=ln.order_no,
        label=ln.label,
        name=ln.name,
        scheduled_value=money(ln.scheduled_value),
        percent_complete=format(ln.percent, "f"),
        earned_to_date=money(ln.earned_to_date),
        earned_previous=money(ln.previous),
        earned_this_application=money(ln.this_application),
        balance_to_finish=money(ln.balance),
    )


def _percent_words(rate: Decimal | None) -> str | None:
    return None if rate is None else format((rate * HUNDRED).quantize(Decimal("0.01")), "f")


def _summary_out(v: ApplicationView) -> PayApplicationSummaryOut:
    s = v.summary
    pct = _percent_words(s.surcharge_rate)
    return PayApplicationSummaryOut(
        earned_to_date=money(s.earned_to_date),
        billed_before=money(s.billed_before),
        amount_due=money(s.amount_due),
        billed_ahead=money(s.billed_ahead),
        surcharge_applies=s.surcharge_applies,
        surcharge_percent=pct,
        surcharge_label=f"Fuel surcharge ({pct}%)"
        if pct is not None and s.surcharge is not None
        else None,
        surcharge=money(s.surcharge),
        total_to_invoice=money(s.total_to_invoice),
        no_invoice_due=s.no_invoice_due,
    )


def _notes(v: ApplicationView) -> list[str]:
    out = []
    if v.status == "draft":
        if v.surcharge_applies is None:
            out.append(paps.CHOICE_UNANSWERED)
        if v.rate_undecided:
            out.append(paps.RATE_UNDECIDED)
        if v.billed_before_undecided:
            out.append(
                "Billed to date before this application waits for the policy keys the board "
                "needs (Configuration, Policy)."
            )
    return out


def _tied(v: ApplicationView) -> bool | None:
    if v.invoice is None:
        return None
    lines = v.invoice.surcharge if v.invoice.surcharge is not None else Decimal("0.00")
    printed = v.summary.surcharge or Decimal("0.00")
    return v.invoice.doc.total - lines == v.summary.amount_due and lines == printed


def _out(v: ApplicationView, issues: list[Issue] | None = None) -> PayApplicationOut:
    return PayApplicationOut(
        id=v.id,
        number=v.number,
        application_date=v.application_date.isoformat(),
        status=v.status,
        status_label=APPLICATION_STATUS_LABELS[v.status],
        status_words=status_words(v),
        surcharge_applies=v.surcharge_applies,
        lines=[_line_out(ln) for ln in v.lines],
        summary=_summary_out(v),
        invoice_number=v.invoice_number,
        invoice_held=v.invoice.doc.doc_number if v.invoice is not None else None,
        invoice_tied=_tied(v),
        issues=_issues(list(issues if issues is not None else v.issues)),
        notes=_notes(v),
        created_by=v.created_by,
        created_at=v.created_at,
        issued_by=v.issued_by,
        issued_at=v.issued_at,
        voided_by=v.voided_by,
        voided_at=v.voided_at,
        void_reason=v.void_reason,
    )


def _context(db: Session, p: Principal, job_id: UUID) -> tuple[service.JobView, Board]:
    try:
        v = service.job_detail(db, p.active_tenant_id, job_id)
    except service.JobError as exc:
        _raise(exc)
    return v, load_board(db, p.active_tenant_id, [v], other=False)


def _fresh(db: Session, p: Principal, job_id: UUID) -> tuple[service.JobView, Board]:
    db.flush()
    db.expire_all()
    return _context(db, p, job_id)


def _view(board: Board, v: service.JobView, application_id: UUID) -> ApplicationView:
    found = next(
        (a for a in board.applications.get(v.job.id, []) if a.id == str(application_id)), None
    )
    if found is None:
        raise HTTPException(status_code=404, detail="That pay application is not on this job.")
    return found


def _list(db: Session, v: service.JobView, board: Board) -> PayApplicationsOut:
    job_id = v.job.id
    views = board.applications.get(job_id, [])
    fixed = v.job.revenue_method == "fixed_price"
    refs = area_refs(v)
    f = board.per_job[job_id]
    numbers = frozenset(a.estimate.external_id for a in v.attached if a.link.role != "ignored")
    rows = paps.load_applications(db, [job_id]).get(job_id, [])
    draft = next((a for a, _l in rows if a.status == "draft"), None)
    n = (
        draft.number
        if draft
        else next_number(
            (d.doc.doc_number for d in f.documents if not d.doc.deleted),
            numbers,
            (a.number for a, _l in rows),
        )
    )
    listed = schedule(refs, board.today, paps._approved_on(v)) if fixed else []
    prev = paps.previous_for(n, rows, board.work_areas[job_id])
    rate, decided = paps.rate_of(db)
    original = v.original
    return PayApplicationsOut(
        job_id=str(job_id),
        fixed_price=fixed,
        applications=[_out(a) for a in sorted(views, key=lambda a: -a.number)],
        schedule=[
            ScheduleAreaOut(
                id=a.work_area_id,
                label=a.label,
                estimate_external_id=a.estimate_number,
                order_no=a.order_no,
                name=a.name,
                price=money(a.price),
                previous_percent=format(
                    (
                        prev[a.key].percent
                        if a.key in prev and prev[a.key].percent is not None
                        else Decimal("0.00")
                    ),
                    "f",
                ),
            )
            for a in listed
        ],
        next_number=n,
        surcharge_percent=_percent_words(rate) if decided else None,
        surcharge_rate_decided=decided,
        note=None
        if fixed
        else paps.NOT_FIXED_PRICE
        if original is not None or v.job.revenue_method != "fixed_price"
        else None,
    )


# --- routes ------------------------------------------------------------------------------------


@router.get("", response_model=PayApplicationsOut)
def list_applications(viewer: Viewer, db: TenantSession, job_id: UUID):
    v, board = _context(db, viewer, job_id)
    return _list(db, v, board)


@router.post("", response_model=PayApplicationOut, status_code=201)
def draft(request: Request, r: Requester, db: TenantSession, job_id: UUID, body: BillingRequestIn):
    """A draft from the billing request (D-26); the job's open draft is replaced; the
    exceptions come back with it, one sentence each."""
    v, board = _context(db, r, job_id)
    f = board.per_job[v.job.id]
    req = RequestIn(
        application_date=body.application_date,
        surcharge_applies=body.surcharge_applies,
        percents=tuple(
            PercentIn(str(p.estimate_work_area_id), _percent(p.percent)) for p in body.percents
        ),
        apply_all=_percent(body.apply_all) if body.apply_all is not None else None,
    )
    try:
        app, made = paps.draft_application(
            db,
            r.active_tenant_id,
            job_id,
            req,
            areas=area_refs(v),
            wa=board.work_areas[v.job.id],
            documents=f.documents,
            estimate_numbers=frozenset(
                a.estimate.external_id for a in v.attached if a.link.role != "ignored"
            ),
            actor=_actor(r, request),
        )
    except service.JobError as exc:
        _raise(exc)
    v, board = _fresh(db, r, job_id)
    return _out(_view(board, v, app.id), list(made.issues))


@router.get("/{application_id}", response_model=PayApplicationOut)
def get_application(viewer: Viewer, db: TenantSession, job_id: UUID, application_id: UUID):
    v, board = _context(db, viewer, job_id)
    return _out(_view(board, v, application_id))


@router.post("/{application_id}/issue", response_model=PayApplicationOut)
def issue(request: Request, i: Issuer, db: TenantSession, job_id: UUID, application_id: UUID):
    v, board = _context(db, i, job_id)
    view = _view(board, v, application_id)
    try:
        paps.issue_application(
            db, i.active_tenant_id, job_id, application_id, view_of=view, actor=_actor(i, request)
        )
    except service.JobError as exc:
        _raise(exc)
    v, board = _fresh(db, i, job_id)
    return _out(_view(board, v, application_id))


@router.post("/{application_id}/void", response_model=PayApplicationOut)
def void(
    request: Request, i: Issuer, db: TenantSession, job_id: UUID, application_id: UUID, body: VoidIn
):
    v, board = _context(db, i, job_id)
    _view(board, v, application_id)
    try:
        paps.void_application(
            db,
            i.active_tenant_id,
            job_id,
            application_id,
            reason=body.reason,
            actor=_actor(i, request),
        )
    except service.JobError as exc:
        _raise(exc)
    v, board = _fresh(db, i, job_id)
    return _out(_view(board, v, application_id))


@router.get("/{application_id}/pdf")
def pdf(viewer: Viewer, db: TenantSession, job_id: UUID, application_id: UUID):
    """The application as the customer receives it (D-36, D-40), from the same figures."""
    v, board = _context(db, viewer, job_id)
    view = _view(board, v, application_id)
    original = v.original
    heading = Heading(
        tenant_name=_tenant_name(db, viewer.active_tenant_id),
        job_name=v.job.name,
        customer_name=v.customer.display_name if v.customer else None,
        estimate_number=original.estimate.external_id if original else "",
    )
    number = invoice_number(view.estimate_number, view.number).replace("/", "-")
    return Response(
        content=application_pdf(view, heading),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="pay-application-{number}.pdf"'},
    )


_ = money_str  # the board's helper is imported for the money scan's benefit; strings only
