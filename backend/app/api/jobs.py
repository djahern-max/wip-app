"""Jobs and the crosswalk (F07). Every role reads jobs (``can_view_jobs``); creating,
attaching, detaching, linking, unlinking, confirming a work-area kind and editing are
for ``firm_admin``, ``firm_staff`` and ``client_admin`` (``can_manage_jobs``), each one
audit row. The customer duplicates list is read-only, for the same three roles.

Money is strings with cents (D-22). Suggestions carry their reason; nothing is
attached or linked except by a person's request naming an id.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    AttachCandidateOut,
    BillingHistoryOut,
    BillingTotalsOut,
    ChoiceOut,
    ConfirmSuggestedOut,
    CustomerPickOut,
    CustomersPageOut,
    DivisionChoiceOut,
    DuplicatePairOut,
    DuplicateSideOut,
    DuplicatesOut,
    EstimateIssueOut,
    JobAliasOut,
    JobBillingOut,
    JobDetailOut,
    JobEstimateOut,
    JobRefOut,
    JobRowOut,
    JobsOut,
    JobWorkAreaOut,
    PaymentAppliedOut,
    PaymentHistoryOut,
    QboRowOut,
    QboRowsOut,
    ReviewEntryOut,
    ReviewOut,
    TieOutOut,
    TrackedOut,
)
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession
from app.core.authz import (
    can_manage_jobs,
    can_track_customers,
    can_view_customer_duplicates,
    can_view_jobs,
)
from app.domain.billing import export
from app.domain.billing.board import Board, TieRow, load_board, money_str, tie_out, tie_out_status
from app.domain.billing.figures import JobFigures, Totals, totals
from app.domain.config.audit import Actor
from app.domain.config.models import Division
from app.domain.estimates.exceptions import Issue
from app.domain.estimates.models import STATUS_LABELS
from app.domain.estimates.service import money
from app.domain.jobs import service
from app.domain.jobs.issues import billing_issues, sentence
from app.domain.jobs.models import (
    ALIAS_SYSTEM_LABELS,
    JOB_STATUS_LABELS,
    JOB_STATUSES,
    REVENUE_METHOD_LABELS,
    REVENUE_METHODS,
    ROLE_LABELS,
    ROLES,
)
from app.domain.jobs.suggest import CustomerRow, suggested_kind
from app.tenancy.models import Tenant

router = APIRouter(prefix="/jobs", tags=["jobs"])
customers_router = APIRouter(prefix="/customers", tags=["customers"])

Viewer = Annotated[Principal, Depends(can_view_jobs)]
Manager = Annotated[Principal, Depends(can_manage_jobs)]
DuplicatesViewer = Annotated[Principal, Depends(can_view_customer_duplicates)]
Tracker = Annotated[Principal, Depends(can_track_customers)]  # F07.2 (D-37)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobCreateIn(_In):
    estimate_id: UUID
    division_id: UUID | None = None
    name: str | None = Field(default=None, max_length=500)


class PoolJobIn(_In):
    name: str = Field(max_length=500)
    division_id: UUID | None = None


class JobPatchIn(_In):
    """Only the fields sent are changed (``model_fields_set``)."""

    name: str | None = Field(default=None, max_length=500)
    division_id: UUID | None = None
    revenue_method: str | None = Field(default=None, max_length=30)
    status: str | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=4000)
    sold_on: date | None = None  # F07.1: corrected by a person; never after today


class AttachIn(_In):
    estimate_id: UUID
    role: str = Field(max_length=20)
    note: str | None = Field(default=None, max_length=2000)


class AliasIn(_In):
    system: str = Field(max_length=30)
    external_id: str = Field(min_length=1, max_length=80)
    set_in_progress: bool = False  # D-35: the first money has moved; offered for a sold job


class KindIn(_In):
    kind: str = Field(max_length=20)


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user.id, role=p.role, meta=request_meta(request))


def _raise(exc: service.JobError) -> None:
    raise HTTPException(status_code=exc.status, detail=str(exc)) from None


def _issues(items: list[Issue]) -> list[EstimateIssueOut]:
    return [EstimateIssueOut(code=i.code, message=i.message) for i in items]


def _divisions(db: Session) -> list[DivisionChoiceOut]:
    return [
        DivisionChoiceOut(id=str(d.id), code=d.code, name=d.name)
        for d in db.execute(
            select(Division).where(Division.active).order_by(Division.code)
        ).scalars()
    ]


def _choices(values: tuple[str, ...], labels: dict[str, str]) -> list[ChoiceOut]:
    return [ChoiceOut(value=v, label=labels[v]) for v in values]


def _contract_note(v: service.JobView) -> str | None:
    c = v.contract
    method = v.job.revenue_method
    if method == "pool":
        return "A pool has no contract (D-30)."
    if method == "recurring_service":
        return "A maintenance or snow program is recognised as billed: no contract (D-35)."
    if method == "time_and_materials":
        return "Time and materials: no revised contract; revenue is what is billed (D-24)."
    if not c.has_original:
        return "No original estimate on this job."
    if c.from_header_price:
        return "No work areas loaded: the original estimate's price."
    if c.to_confirm:
        return f"{c.to_confirm} work area{'s' if c.to_confirm != 1 else ''} to confirm"
    return None


def _eac_note(v: service.JobView) -> str | None:
    if v.job.revenue_method == "pool":
        return "A pool has no EAC (D-30)."
    if v.job.revenue_method == "recurring_service":
        return "A maintenance or snow program has no EAC (D-35)."
    if v.contract.eac_not_computed:
        return (
            "Not computed: an estimate on this job has no EAC in the WIP basis; see its attention."
        )
    if v.contract.eac_in_basis is None:
        return "Not computed"
    return None


def _remaining_note(v: service.JobView, f: JobFigures) -> str | None:
    """Why remaining to bill is not shown (D-24, D-30, D-35), in words."""
    if f.remaining_to_bill is not None:
        return None
    if v.job.revenue_method != "fixed_price":
        return f"Not shown: {REVENUE_METHOD_LABELS[v.job.revenue_method]} has no contract."
    if v.contract.revised_contract is None:
        return "Not shown: no original estimate."
    return None  # a policy key is undecided; the page's note says so


def _deposit_note(f: JobFigures) -> str | None:
    n = len(f.deposit_mismatches)
    if n == 0:
        return None
    return (
        f"{n} document{'' if n == 1 else 's'} look{'s' if n == 1 else ''} like the deposit "
        "but is not identified as one; see Attention."
    )


def _billing(v: service.JobView, f: JobFigures) -> JobBillingOut:
    return JobBillingOut(
        deposit_invoiced=money_str(f.deposit_invoiced),
        deposit_received=money_str(f.deposit_received),
        billed_to_date=money_str(f.billed_to_date),
        fuel_surcharge_billed=money_str(f.fuel_surcharge_billed),
        collected_to_date=money_str(f.collected_to_date),
        open_ar=money_str(f.open_ar),
        unapplied_payments=money_str(f.unapplied_payments),
        remaining_to_bill=money_str(f.remaining_to_bill),
        remaining_to_bill_note=_remaining_note(v, f),
        last_billing_date=f.last_billing_date.isoformat() if f.last_billing_date else None,
        last_payment_date=f.last_payment_date.isoformat() if f.last_payment_date else None,
        days_since_activity=f.days_since_activity,
        deposit_note=_deposit_note(f),
    )


def _totals_out(t: Totals) -> BillingTotalsOut:
    return BillingTotalsOut(
        deposit_invoiced=money_str(t.deposit_invoiced),
        deposit_received=money_str(t.deposit_received),
        billed_to_date=money_str(t.billed_to_date),
        fuel_surcharge_billed=money_str(t.fuel_surcharge_billed),
        collected_to_date=money_str(t.collected_to_date),
        open_ar=money_str(t.open_ar),
        unapplied_payments=money_str(t.unapplied_payments),
        remaining_to_bill=money_str(t.remaining_to_bill),
    )


def _not_on_a_job_out(f: JobFigures) -> BillingTotalsOut:
    return _totals_out(totals([f]))


def _tie_out_out(rows: list[TieRow]) -> TieOutOut:
    off = [r.month for r in rows if not r.balanced]
    return TieOutOut(
        status=tie_out_status(rows), balanced=not off, months=len(rows), months_off=off
    )


def _history(f: JobFigures) -> tuple[list[BillingHistoryOut], list[PaymentHistoryOut]]:
    documents = [
        BillingHistoryOut(
            billing_id=d.doc.id,
            kind=d.doc.kind,
            kind_label=d.kind_label,
            external_id=d.doc.external_id,
            doc_number=d.doc.doc_number,
            txn_date=d.doc.txn_date.isoformat(),
            total=money_str(d.sign * d.doc.total),
            sales_tax=money_str(d.sign * d.doc.tax_total),
            fuel_surcharge=money_str(d.surcharge),
            billed=money_str(d.billed),
            counted=d.counted,
            balance=money_str(d.sign * d.doc.balance),
            is_deposit=d.is_deposit,
            state_label=d.state_label,
        )
        for d in f.documents
    ]
    payments = [
        PaymentHistoryOut(
            payment_id=p.payment_id,
            kind=p.kind,
            kind_label=p.kind_label,
            external_id=p.external_id,
            txn_date=p.txn_date.isoformat(),
            total=money_str(p.total),
            applied=[
                PaymentAppliedOut(document=doc, amount=money_str(amt)) for doc, amt in p.applied
            ],
            unapplied=money_str(p.unapplied),
            on_this_job=p.on_this_job,
            state_label="Deleted" if p.deleted else "",
        )
        for p in f.payments
    ]
    return documents, payments


def _tenant_name(db: Session, tenant_id: UUID) -> str:
    row = db.get(Tenant, tenant_id)
    return row.name if row is not None else ""


def _row(v: service.JobView, f: JobFigures) -> dict:
    job = v.job
    original = v.original
    return {
        "id": str(job.id),
        "name": job.name,
        "estimate_number": original.estimate.external_id if original else None,
        "estimator": original.estimate.estimator if original else None,
        "customer_name": v.customer.display_name if v.customer else None,
        "division_id": str(job.division_id) if job.division_id else None,
        "division_code": v.division.code if v.division else None,
        "revenue_method": job.revenue_method,
        "revenue_method_label": REVENUE_METHOD_LABELS[job.revenue_method],
        "status": job.status,
        "status_label": JOB_STATUS_LABELS[job.status],
        "sold_on": job.sold_on.isoformat(),
        "sold_on_set_when_created": v.sold_on_set_when_created,
        "revised_contract": money(v.contract.revised_contract),
        "revised_contract_note": _contract_note(v),
        "unapproved_change_orders": money(v.contract.unapproved_change_orders),
        "eac_in_basis": money(v.contract.eac_in_basis),
        "eac_note": _eac_note(v),
        "to_confirm": v.contract.to_confirm,
        "qbo_linked": bool(v.qbo_aliases),
        "qbo_names": [
            v.alias_rows[a.external_id].display_name
            if a.external_id in v.alias_rows
            else a.external_id
            for a in v.qbo_aliases
        ],
        "attention": _issues([*v.issues, *billing_issues(job.name, f)]),
        "billing": _billing(v, f),
    }


def _kind_label(kind: str | None, suggested: str | None, kept: bool) -> str:
    """Words first (D-22): what is suggested, what is confirmed (F07.1)."""
    if not kept:
        return "Omitted"
    if kind is not None:
        return "Original, confirmed" if kind == "original" else "Change order, confirmed"
    return "Original (suggested)" if suggested == "original" else "Change order (suggested)"


def _confirm_message(confirmed: int, skipped: int) -> str:
    if confirmed == 0 and skipped == 0:
        return "Nothing was left to confirm."
    text = f"{confirmed} work area{'s' if confirmed != 1 else ''} confirmed as suggested"
    if skipped:
        text += (
            f"; {skipped} kept work area{'s have' if skipped != 1 else ' has'} no suggestion "
            f"and {'are' if skipped != 1 else 'is'} left to confirm"
        )
    return text + "."


def _detail(db: Session, tenant_id: UUID, v: service.JobView) -> JobDetailOut:
    board = load_board(db, tenant_id, [v])
    f = board.per_job[v.job.id]
    documents, payments = _history(f)
    original = v.original
    work_areas: list[JobWorkAreaOut] = []
    if original is not None:
        for w in original.view.work_areas or ():
            row = w.row
            suggestion = suggested_kind(row.change_order_suggested) if row.kept else None
            work_areas.append(
                JobWorkAreaOut(
                    id=str(row.id),
                    order_no=row.order_no,
                    name=row.name,
                    kept=row.kept,
                    kept_label="Kept" if row.kept else "Omitted",
                    price=money(row.price),
                    kind=row.kind,
                    suggested_kind=suggestion,
                    kind_label=_kind_label(row.kind, suggestion, row.kept),
                    confirmed=row.kind is not None,
                )
            )
    return JobDetailOut(
        **_row(v, f),
        billing_history=documents,
        payment_history=payments,
        tenant_name=_tenant_name(db, tenant_id),
        as_of=board.today.isoformat(),
        policy_note=board.policy_note,
        notes=v.job.notes,
        created_at=v.job.created_at.isoformat(),
        created_by=v.users.get(v.job.created_by) if v.job.created_by else None,
        estimates=[
            JobEstimateOut(
                estimate_id=str(a.estimate.id),
                external_id=a.estimate.external_id,
                name=a.estimate.name,
                status_label=STATUS_LABELS.get(a.estimate.status_norm or "", a.estimate.status),
                role=a.link.role,
                role_label=ROLE_LABELS[a.link.role],
                price=money(a.estimate.price),
                note=a.link.note,
                attached_at=a.link.attached_at.isoformat(),
                attached_by=v.users.get(a.link.attached_by) if a.link.attached_by else None,
                work_areas_loaded=a.view.work_areas is not None,
                eac_in_basis=money(a.eac),
            )
            for a in v.attached
        ],
        aliases=[
            JobAliasOut(
                id=str(a.id),
                system=a.system,
                system_label=ALIAS_SYSTEM_LABELS[a.system],
                external_id=a.external_id,
                display_name=v.alias_rows[a.external_id].display_name
                if a.external_id in v.alias_rows
                else None,
                kind_label=v.alias_rows[a.external_id].kind_label
                if a.external_id in v.alias_rows
                else None,
                linked_at=a.linked_at.isoformat(),
                linked_by=v.users.get(a.linked_by) if a.linked_by else None,
            )
            for a in v.aliases
        ],
        original_external_id=original.estimate.external_id if original else None,
        work_areas=work_areas,
        work_areas_loaded=bool(original and original.view.work_areas is not None),
        divisions=_divisions(db),
        revenue_methods=_choices(REVENUE_METHODS, REVENUE_METHOD_LABELS),
        statuses=_choices(JOB_STATUSES, JOB_STATUS_LABELS),
    )


def _fresh_detail(db: Session, p: Principal, job_id: UUID) -> JobDetailOut:
    db.flush()
    db.expire_all()
    return _detail(db, p.active_tenant_id, service.job_detail(db, p.active_tenant_id, job_id))


def _qbo_row(r: CustomerRow, reason: str | None) -> QboRowOut:
    return QboRowOut(
        customer_id=str(r.id),
        external_id=r.external_id,
        display_name=r.display_name,
        parent_name=r.parent_name,
        kind_label=r.kind_label,
        reason=reason,
    )


# --- reads ---------------------------------------------------------------------------


class _BoardPage:
    """The Sold Jobs Board (F08): every job's figures from one board over all jobs (the
    tie-out needs every job), the listed jobs after the filters, the totals over the
    listed jobs, and the not-on-a-job row."""

    def __init__(
        self,
        db: Session,
        tenant_id: UUID,
        *,
        status: str | None,
        division_id: UUID | None,
        revenue_method: str | None,
        no_link: bool,
    ) -> None:
        self.all_views = service.list_jobs(db, tenant_id)
        self.board: Board = load_board(db, tenant_id, self.all_views)
        self.views = [
            v
            for v in self.all_views
            if (not status or v.job.status == status)
            and (division_id is None or v.job.division_id == division_id)
            and (not revenue_method or v.job.revenue_method == revenue_method)
            and (not no_link or not v.qbo_aliases)
        ]
        self.tie_rows = tie_out(db, tenant_id, self.board)
        self.tenant_name = _tenant_name(db, tenant_id)
        self.filters = _filter_words(db, status, division_id, revenue_method, no_link)

    def figures(self, v: service.JobView) -> JobFigures:
        return self.board.per_job[v.job.id]

    @property
    def totals(self) -> Totals:
        return totals([self.figures(v) for v in self.views])


def _filter_words(
    db: Session,
    status: str | None,
    division_id: UUID | None,
    revenue_method: str | None,
    no_link: bool,
) -> str:
    parts = []
    if status:
        parts.append(f"status {JOB_STATUS_LABELS.get(status, status)}")
    if division_id is not None:
        d = db.get(Division, division_id)
        parts.append(f"division {d.code if d else division_id}")
    if revenue_method:
        parts.append(f"revenue method {REVENUE_METHOD_LABELS.get(revenue_method, revenue_method)}")
    if no_link:
        parts.append("no QuickBooks link")
    return "All jobs" if not parts else "Jobs with " + ", ".join(parts)


FilterStatus = Annotated[str | None, Query(max_length=30)]
FilterMethod = Annotated[str | None, Query(max_length=30)]


@router.get("", response_model=JobsOut)
def list_jobs(
    viewer: Viewer,
    db: TenantSession,
    status: FilterStatus = None,
    division_id: UUID | None = None,
    revenue_method: FilterMethod = None,
    no_link: bool = False,
):
    page = _BoardPage(
        db,
        viewer.active_tenant_id,
        status=status or None,
        division_id=division_id,
        revenue_method=revenue_method or None,
        no_link=no_link,
    )
    return JobsOut(
        jobs=[JobRowOut(**_row(v, page.figures(v))) for v in page.views],
        total=len(page.views),
        to_review=len(service.review_queue(db, viewer.active_tenant_id)),
        divisions=_divisions(db),
        revenue_methods=_choices(REVENUE_METHODS, REVENUE_METHOD_LABELS),
        statuses=_choices(JOB_STATUSES, JOB_STATUS_LABELS),
        ledger_items=_issues(service.ledger_items(db)),
        tenant_name=page.tenant_name,
        as_of=page.board.today.isoformat(),
        totals=_totals_out(page.totals),
        not_on_a_job=_not_on_a_job_out(page.board.not_on_a_job),
        policy_note=page.board.policy_note,
        tie_out=_tie_out_out(page.tie_rows),
    )


def _board_row(v: service.JobView, f: JobFigures) -> export.BoardRow:
    b = _billing(v, f)
    notes: dict[str, str] = {}
    if v.contract.revised_contract is None:
        notes["revised_contract"] = "None"
    if f.remaining_to_bill is None:
        notes["remaining_to_bill"] = b.remaining_to_bill_note or "Not decided"
    if f.billed_to_date is None:
        for key in (
            "billed_to_date",
            "fuel_surcharge_billed",
            "deposit_invoiced",
            "deposit_received",
        ):
            notes[key] = "Not decided"
    if f.days_since_activity is None:
        notes["days_since_activity"] = "No activity"
    return export.BoardRow(
        values={
            "job": v.job.name,
            "estimate_number": v.original.estimate.external_id if v.original else "",
            "customer_name": v.customer.display_name if v.customer else "Not linked",
            "division_code": v.division.code if v.division else "Not set",
            "revenue_method_label": REVENUE_METHOD_LABELS[v.job.revenue_method],
            "status_label": JOB_STATUS_LABELS[v.job.status],
            "estimator": (v.original.estimate.estimator or "") if v.original else "",
            "revised_contract": v.contract.revised_contract,
            "deposit_invoiced": f.deposit_invoiced,
            "deposit_received": f.deposit_received,
            "billed_to_date": f.billed_to_date,
            "fuel_surcharge_billed": f.fuel_surcharge_billed,
            "collected_to_date": f.collected_to_date,
            "open_ar": f.open_ar,
            "remaining_to_bill": f.remaining_to_bill,
            "days_since_activity": f.days_since_activity,
        },
        notes=notes,
    )


def _totals_row(label: str, t: Totals, *, revised: Decimal | None) -> export.BoardRow:
    notes: dict[str, str] = {}
    if t.billed_to_date is None:
        for key in (
            "billed_to_date",
            "fuel_surcharge_billed",
            "deposit_invoiced",
            "deposit_received",
        ):
            notes[key] = "Not decided"
    if t.remaining_to_bill is None:
        notes["remaining_to_bill"] = ""
    return export.BoardRow(
        values={
            "job": label,
            "revised_contract": revised,
            "deposit_invoiced": t.deposit_invoiced,
            "deposit_received": t.deposit_received,
            "billed_to_date": t.billed_to_date,
            "fuel_surcharge_billed": t.fuel_surcharge_billed,
            "collected_to_date": t.collected_to_date,
            "open_ar": t.open_ar,
            "remaining_to_bill": t.remaining_to_bill,
        },
        notes=notes,
    )


def _report(page: _BoardPage) -> export.BoardReport:
    rows = tuple(_board_row(v, page.figures(v)) for v in page.views)
    revised = [
        v.contract.revised_contract for v in page.views if v.contract.revised_contract is not None
    ]
    return export.BoardReport(
        tenant_name=page.tenant_name,
        as_of=page.board.today,
        filters=page.filters,
        rows=rows,
        totals=_totals_row(
            "Total", page.totals, revised=sum(revised, Decimal("0.00")) if revised else None
        ),
        not_on_a_job=_totals_row("Not on a job", totals([page.board.not_on_a_job]), revised=None),
        tie_out=tuple(page.tie_rows),
        tie_out_status=tie_out_status(page.tie_rows),
        policy_note=page.board.policy_note,
    )


def _export(
    viewer: Principal,
    db: Session,
    status: str | None,
    division_id: UUID | None,
    revenue_method: str | None,
    no_link: bool,
    *,
    kind: str,
) -> Response:
    page = _BoardPage(
        db,
        viewer.active_tenant_id,
        status=status or None,
        division_id=division_id,
        revenue_method=revenue_method or None,
        no_link=no_link,
    )
    report = _report(page)
    stem = f"sold-jobs-board-{page.board.today.isoformat()}"
    if kind == "xlsx":
        body = export.board_xlsx(report)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        body = export.board_pdf(report)
        media = "application/pdf"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{kind}"'},
    )


@router.get("/export.xlsx", include_in_schema=True)
def export_xlsx(
    viewer: Viewer,
    db: TenantSession,
    status: FilterStatus = None,
    division_id: UUID | None = None,
    revenue_method: FilterMethod = None,
    no_link: bool = False,
):
    """F08: the board as shown, with the filters applied, values from Decimal, and a
    tie-out tab."""
    return _export(viewer, db, status, division_id, revenue_method, no_link, kind="xlsx")


@router.get("/export.pdf", include_in_schema=True)
def export_pdf(
    viewer: Viewer,
    db: TenantSession,
    status: FilterStatus = None,
    division_id: UUID | None = None,
    revenue_method: FilterMethod = None,
    no_link: bool = False,
):
    """F08 (D-40): the same rows and totals as the XLSX."""
    return _export(viewer, db, status, division_id, revenue_method, no_link, kind="pdf")


@router.get("/review", response_model=ReviewOut)
def review(viewer: Viewer, db: TenantSession):
    entries = service.review_queue(db, viewer.active_tenant_id)
    divisions = _divisions(db)
    codes = {d.id: d.code for d in divisions}
    out = []
    for q in entries:
        e = q.view.estimate
        suggestion = str(q.division_suggestion) if q.division_suggestion else None
        out.append(
            ReviewEntryOut(
                estimate_id=str(e.id),
                external_id=e.external_id,
                estimator=e.estimator,
                client_name=e.client_name,
                jobsite=e.jobsite,
                name=e.name,
                price=money(e.price),
                estimate_date=e.estimate_date.isoformat() if e.estimate_date else None,
                division_suggestion_id=suggestion if suggestion in codes else None,
                division_suggestion_code=codes.get(suggestion) if suggestion else None,
                candidates=[
                    AttachCandidateOut(
                        job_id=str(c.job.id),
                        job_name=c.job.name,
                        reasons=list(c.reasons),
                        same_customer=c.same_customer,
                    )
                    for c in q.candidates
                ],
                attention=_issues(q.issues),
            )
        )
    return ReviewOut(
        entries=out,
        total=len(out),
        divisions=divisions,
        roles=_choices(ROLES, ROLE_LABELS),
    )


@router.get("/{job_id}", response_model=JobDetailOut)
def get_job(viewer: Viewer, db: TenantSession, job_id: UUID):
    try:
        return _detail(
            db, viewer.active_tenant_id, service.job_detail(db, viewer.active_tenant_id, job_id)
        )
    except service.JobError as exc:
        _raise(exc)


@router.get("/{job_id}/qbo-candidates", response_model=QboRowsOut)
def qbo_candidates(viewer: Viewer, db: TenantSession, job_id: UUID):
    try:
        found = service.job_qbo_candidates(db, job_id)
    except service.JobError as exc:
        _raise(exc)
    return QboRowsOut(rows=[_qbo_row(c.row, c.reason) for c in found])


@router.get("/{job_id}/qbo-search", response_model=QboRowsOut)
def qbo_search(
    viewer: Viewer, db: TenantSession, job_id: UUID, q: Annotated[str, Query(max_length=200)] = ""
):
    try:
        service.job_detail(db, viewer.active_tenant_id, job_id)
    except service.JobError as exc:
        _raise(exc)
    return QboRowsOut(rows=[_qbo_row(r, None) for r in service.qbo_search(db, q)])


def _pick_row(p: service.PickRow) -> CustomerPickOut:
    r = p.row
    return CustomerPickOut(
        customer_id=str(r.id),
        external_id=r.external_id,
        display_name=r.display_name,
        parent_name=r.parent_name,
        kind_label=r.kind_label,
        active=r.active,
        billing_count=p.billing_count,
        payment_count=p.payment_count,
        tracked=r.tracked,
        job=JobRefOut(id=str(p.job[0]), name=p.job[1]) if p.job else None,
        needs_job=p.needs_job,
    )


def _tracked(db: Session) -> TrackedOut:
    return TrackedOut(rows=[_pick_row(p) for p in service.tracked_customers(db)])


@customers_router.get("", response_model=CustomersPageOut)
def search_customers(
    tracker: Tracker,
    db: TenantSession,
    q: Annotated[str, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
):
    """The picker's search (F07.2): active QuickBooks rows by name, projects first, one
    page at a time; empty text returns no rows."""
    found = service.search_customers(db, q, page)
    return CustomersPageOut(
        rows=[_pick_row(p) for p in found.rows],
        page=found.page,
        pages=found.pages,
        total=found.total,
        page_size=service.PAGE_SIZE,
    )


@customers_router.get("/tracked", response_model=TrackedOut)
def tracked_customers(tracker: Tracker, db: TenantSession):
    return _tracked(db)


@customers_router.post("/{customer_id}/track", response_model=TrackedOut)
def track_customer(request: Request, tracker: Tracker, db: TenantSession, customer_id: UUID):
    """The id and nothing else: no body, no name (D-37)."""
    try:
        service.track_customer(db, tracker.active_tenant_id, customer_id, _actor(tracker, request))
    except service.JobError as exc:
        _raise(exc)
    return _tracked(db)


@customers_router.post("/{customer_id}/untrack", response_model=TrackedOut)
def untrack_customer(request: Request, tracker: Tracker, db: TenantSession, customer_id: UUID):
    try:
        service.untrack_customer(
            db, tracker.active_tenant_id, customer_id, _actor(tracker, request)
        )
    except service.JobError as exc:
        _raise(exc)
    return _tracked(db)


@customers_router.get("/duplicates", response_model=DuplicatesOut)
def duplicates(viewer: DuplicatesViewer, db: TenantSession):
    def side(s: service.DuplicateSide) -> DuplicateSideOut:
        return DuplicateSideOut(
            customer_id=str(s.row.id),
            external_id=s.row.external_id,
            display_name=s.row.display_name,
            billing_count=s.billing_count,
            payment_count=s.payment_count,
        )

    return DuplicatesOut(
        pairs=[
            DuplicatePairOut(
                code="CUSTOMER_FUZZY",
                message=sentence("CUSTOMER_FUZZY", a=a.row.display_name, b=b.row.display_name),
                a=side(a),
                b=side(b),
            )
            for a, b in service.customer_duplicates(db)
        ]
    )


# --- writes (one audit row each) -------------------------------------------------------


@router.post("", response_model=JobDetailOut, status_code=201)
def create_job(request: Request, m: Manager, db: TenantSession, body: JobCreateIn):
    try:
        job = service.create_job_from_estimate(
            db,
            m.active_tenant_id,
            estimate_id=body.estimate_id,
            division_id=body.division_id,
            name=body.name,
            actor=_actor(m, request),
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job.id)


@router.post("/pool", response_model=JobDetailOut, status_code=201)
def create_pool(request: Request, m: Manager, db: TenantSession, body: PoolJobIn):
    try:
        job = service.create_pool_job(
            db,
            m.active_tenant_id,
            name=body.name,
            division_id=body.division_id,
            actor=_actor(m, request),
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job.id)


@router.post("/program", response_model=JobDetailOut, status_code=201)
def create_program(request: Request, m: Manager, db: TenantSession, body: PoolJobIn):
    """D-35: one maintenance or snow program job per division-season, made by hand."""
    try:
        job = service.create_pool_job(
            db,
            m.active_tenant_id,
            name=body.name,
            division_id=body.division_id,
            actor=_actor(m, request),
            revenue_method="recurring_service",
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job.id)


@router.patch("/{job_id}", response_model=JobDetailOut)
def patch_job(request: Request, m: Manager, db: TenantSession, job_id: UUID, body: JobPatchIn):
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        service.update_job(db, m.active_tenant_id, job_id, changes, _actor(m, request))
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.post("/{job_id}/estimates", response_model=JobDetailOut)
def attach(request: Request, m: Manager, db: TenantSession, job_id: UUID, body: AttachIn):
    try:
        service.attach_estimate(
            db,
            m.active_tenant_id,
            job_id,
            estimate_id=body.estimate_id,
            role=body.role,
            note=body.note,
            actor=_actor(m, request),
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.delete("/{job_id}/estimates/{estimate_id}", response_model=JobDetailOut)
def detach(request: Request, m: Manager, db: TenantSession, job_id: UUID, estimate_id: UUID):
    try:
        service.detach_estimate(db, m.active_tenant_id, job_id, estimate_id, _actor(m, request))
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.post("/{job_id}/aliases", response_model=JobDetailOut)
def link(request: Request, m: Manager, db: TenantSession, job_id: UUID, body: AliasIn):
    try:
        service.link_alias(
            db,
            m.active_tenant_id,
            job_id,
            system=body.system,
            external_id=body.external_id,
            actor=_actor(m, request),
            set_in_progress=body.set_in_progress,
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.delete("/{job_id}/aliases/{alias_id}", response_model=JobDetailOut)
def unlink(request: Request, m: Manager, db: TenantSession, job_id: UUID, alias_id: UUID):
    try:
        service.unlink_alias(db, m.active_tenant_id, job_id, alias_id, _actor(m, request))
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.post("/{job_id}/work-areas/{work_area_id}/kind", response_model=JobDetailOut)
def confirm_kind(
    request: Request, m: Manager, db: TenantSession, job_id: UUID, work_area_id: UUID, body: KindIn
):
    try:
        service.confirm_kind(
            db, m.active_tenant_id, job_id, work_area_id, body.kind, _actor(m, request)
        )
    except service.JobError as exc:
        _raise(exc)
    return _fresh_detail(db, m, job_id)


@router.post("/{job_id}/work-areas/kinds/confirm-suggested", response_model=ConfirmSuggestedOut)
def confirm_suggested(request: Request, m: Manager, db: TenantSession, job_id: UUID):
    """F07.1: confirm every kept, unconfirmed work area at its suggested kind (D-01); one
    audit row per work area in this one transaction."""
    try:
        done = service.confirm_suggested_kinds(db, m.active_tenant_id, job_id, _actor(m, request))
    except service.JobError as exc:
        _raise(exc)
    detail = _fresh_detail(db, m, job_id)
    return ConfirmSuggestedOut(
        **detail.model_dump(),
        confirmed=done.confirmed,
        skipped=done.skipped,
        message=_confirm_message(done.confirmed, done.skipped),
    )
