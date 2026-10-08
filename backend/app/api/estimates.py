"""Estimates (F06): read-only. Every role reads (``can_view_estimates``); uploading
goes through ``/api/imports`` with the ``estimate_template`` source kind. Money is
strings with cents (D-22); ``status_label`` and the exception sentences are what a
person sees, ``status_norm`` and the codes are the machine values."""

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import (
    DismissedOut,
    EstimateBurdenDivisionOut,
    EstimateCategoryTotalOut,
    EstimateCostLineOut,
    EstimateDetailOut,
    EstimateIssueOut,
    EstimateJobOut,
    EstimateRowOut,
    EstimatesOut,
    EstimateTotalsOut,
    EstimateVersionOut,
    EstimateWorkAreaOut,
)
from app.core.auth import Principal, TenantSession
from app.core.authz import can_view_estimates
from app.domain.estimates import service
from app.domain.estimates.burden import BURDEN_SLOT
from app.domain.estimates.models import STATUS_LABELS
from app.domain.estimates.service import (
    IN_BASIS_LABELS,
    BurdenView,
    EstimateView,
    Grid,
    money,
    status_label,
)
from app.domain.estimates.totals import with_burden
from app.domain.exceptions import service as exceptions
from app.domain.exceptions.collect import estimate_attention
from app.domain.jobs.models import ROLE_LABELS
from app.domain.jobs.service import estimate_job

router = APIRouter(prefix="/estimates", tags=["estimates"])

Viewer = Annotated[Principal, Depends(can_view_estimates)]


def _row(view: EstimateView, attention: list) -> EstimateRowOut:
    """F09: ``attention`` is the one computation (``estimate_attention``) less the dismissed
    exceptions; the list and the detail pass the same list, burden sentences included."""
    e = view.estimate
    return EstimateRowOut(
        id=str(e.id),
        external_id=e.external_id,
        estimator=e.estimator,
        client_name=e.client_name,
        jobsite=e.jobsite,
        name=e.name,
        status=e.status,
        status_norm=e.status_norm,
        status_label=status_label(e),
        price=money(e.price),
        estimate_date=e.estimate_date.isoformat() if e.estimate_date else None,
        versions=view.version_count,
        attention=[EstimateIssueOut(code=i.code, message=i.message) for i in attention],
    )


def _attention(db, view: EstimateView, grid: Grid, inputs, dismissed) -> list:
    burden = service.estimate_burden(db, view, grid, inputs)
    return exceptions.without_dismissed(
        estimate_attention(view, burden), "estimate", view.estimate.id, dismissed
    )


@router.get("", response_model=EstimatesOut)
def list_estimates(
    viewer: Viewer,
    db: TenantSession,
    status: Annotated[str | None, Query(pattern="^(pending|sold|lost|unknown)$")] = None,
    estimator: Annotated[str | None, Query(max_length=200)] = None,
):
    views, estimators, grid = service.list_estimates(
        db, viewer.active_tenant_id, status=status, estimator=estimator
    )
    # F09: the burden sentences on the list too (the rates and the time zone read once), and
    # the dismissed exceptions left out (one statement for the page).
    inputs = service.load_burden_inputs(db)
    dismissed = exceptions.dismissed_keys(db)
    return EstimatesOut(
        estimates=[_row(v, _attention(db, v, grid, inputs, dismissed)) for v in views],
        estimators=estimators,
        total=len(views),
    )


def _work_area(w: service.WorkAreaView, burden: BurdenView | None = None) -> EstimateWorkAreaOut:
    wa = w.as_in()
    return EstimateWorkAreaOut(
        order_no=w.row.order_no,
        name=w.row.name,
        kept=w.row.kept,
        kept_label="Kept" if w.row.kept else "Omitted",
        change_order_suggested=w.row.change_order_suggested,
        hours=money(wa.hours),
        cost=money(wa.cost),
        price=money(w.row.price),
        notes=w.row.notes,
        burden=None if burden is None else money(burden.result.burden_for(w.row.order_no)),
        lines=[
            EstimateCostLineOut(
                cost_code=lv.line.cost_code,
                category_name=lv.category_name,
                division_code=lv.division_code,
                hours=money(lv.line.hours),
                amount=money(lv.line.amount),
                notes=lv.line.notes,
            )
            for lv in w.lines
        ],
    )


def _percent(rate: Decimal) -> str:
    return format((rate * 100).quantize(Decimal("0.01")), "f")


def _totals(view: EstimateView, grid: Grid, burden: BurdenView) -> EstimateTotalsOut:
    t = view.totals(grid)
    r = burden.result
    wb = with_burden(t, grid.basis, r.total)
    return EstimateTotalsOut(
        kept_original=money(t.kept_original),
        kept_change_orders=money(t.kept_change_orders),
        kept_total=money(t.kept_total),
        omitted=money(t.omitted),
        kept_hours=money(t.kept_hours),
        kept_cost=money(t.kept_cost),
        eac_in_basis=money(wb.eac_in_basis),
        basis_decided=t.basis_decided,
        eac_in_basis_as_estimated=money(t.eac_in_basis),
        eac_not_computed=wb.eac_not_computed,
        cost_total_as_estimated=money(t.kept_cost),
        cost_total_with_burden=money(wb.kept_cost),
        burden_total=money(r.total),
        burden_computed=r.total is not None,
        burden_date=r.day.isoformat() if r.day else None,
        burden_date_source=burden.day_source,
        burden_by_division=[
            EstimateBurdenDivisionOut(
                division_code=d.division_code,
                labor_amount=money(d.labor),
                rate=None if d.rate is None else format(d.rate.rate, "f"),
                rate_percent=None if d.rate is None else _percent(d.rate.rate),
                rate_effective_from=None if d.rate is None else d.rate.effective_from.isoformat(),
                basis_note=None if d.rate is None else d.rate.basis_note,
                burden=money(d.burden),
            )
            for d in r.by_division
        ],
        by_category=[
            EstimateCategoryTotalOut(
                slot=c.slot,
                name=c.name,
                amount=money(c.amount),
                amount_with_burden=money(wb.labor_burden)
                if c.slot == BURDEN_SLOT
                else money(c.amount),
                hours=money(c.hours),
                in_basis=c.in_basis,
                in_basis_label=IN_BASIS_LABELS[c.in_basis],
            )
            for c in t.by_category
        ],
    )


@router.get("/{estimate_id}", response_model=EstimateDetailOut)
def get_estimate(viewer: Viewer, db: TenantSession, estimate_id: UUID):
    found = service.estimate_detail(db, viewer.active_tenant_id, estimate_id)
    if found is None:
        raise HTTPException(status_code=404, detail="estimate not found")
    view, versions, grid = found
    burden = service.estimate_burden(db, view, grid)
    baseline = next((vv.version.version_no for vv in versions if vv.version.is_baseline), None)
    attention = exceptions.without_dismissed(
        estimate_attention(view, burden),
        "estimate",
        view.estimate.id,
        exceptions.dismissed_keys(db),
    )
    row = _row(view, attention).model_dump()
    dismissed = [
        DismissedOut(
            id=d.id,
            code=d.code,
            message=d.message,
            note=d.note,
            dismissed_by=d.dismissed_by,
            dismissed_at=d.dismissed_at,
        )
        for d in exceptions.dismissed_for(db, "estimate", view.estimate.id)
    ]
    on_job = estimate_job(db, view.estimate.id)  # F07: the "Job:" line
    return EstimateDetailOut(
        **row,
        job=None
        if on_job is None
        else EstimateJobOut(
            id=str(on_job[0].id),
            name=on_job[0].name,
            role=on_job[1].role,
            role_label=ROLE_LABELS[on_job[1].role],
        ),
        to_review=on_job is None and view.estimate.status_norm == "sold",
        dismissed=dismissed,
        versions_list=[
            EstimateVersionOut(
                id=str(vv.version.id),
                version_no=vv.version.version_no,
                received_at=vv.version.received_at.isoformat(),
                is_baseline=vv.version.is_baseline,
                has_work_areas=vv.version.work_areas_raw_record_id is not None,
                kept_total=money(vv.version.kept_total),
                status_label=STATUS_LABELS.get(vv.version.status_norm or "", "Unknown status"),
                import_batch_id=str(vv.version.import_batch_id),
                original_filename=vv.original_filename,
            )
            for vv in versions
        ],
        baseline_version_no=baseline,
        work_areas=[_work_area(w, burden) for w in view.work_areas or ()],
        totals=_totals(view, grid, burden),
    )
