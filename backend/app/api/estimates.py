"""Estimates (F06): read-only. Every role reads (``can_view_estimates``); uploading
goes through ``/api/imports`` with the ``estimate_template`` source kind. Money is
strings with cents (D-22); ``status_label`` and the exception sentences are what a
person sees, ``status_norm`` and the codes are the machine values."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.schemas import (
    EstimateCategoryTotalOut,
    EstimateCostLineOut,
    EstimateDetailOut,
    EstimateIssueOut,
    EstimateRowOut,
    EstimatesOut,
    EstimateTotalsOut,
    EstimateVersionOut,
    EstimateWorkAreaOut,
)
from app.core.auth import Principal, TenantSession
from app.core.authz import can_view_estimates
from app.domain.estimates import service
from app.domain.estimates.models import STATUS_LABELS
from app.domain.estimates.service import IN_BASIS_LABELS, EstimateView, Grid, money, status_label

router = APIRouter(prefix="/estimates", tags=["estimates"])

Viewer = Annotated[Principal, Depends(can_view_estimates)]


def _row(view: EstimateView) -> EstimateRowOut:
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
        attention=[EstimateIssueOut(code=i.code, message=i.message) for i in view.issues()],
    )


@router.get("", response_model=EstimatesOut)
def list_estimates(
    viewer: Viewer,
    db: TenantSession,
    status: Annotated[str | None, Query(pattern="^(pending|sold|lost|unknown)$")] = None,
    estimator: Annotated[str | None, Query(max_length=200)] = None,
):
    views, estimators, _grid = service.list_estimates(
        db, viewer.active_tenant_id, status=status, estimator=estimator
    )
    return EstimatesOut(estimates=[_row(v) for v in views], estimators=estimators, total=len(views))


def _work_area(w: service.WorkAreaView) -> EstimateWorkAreaOut:
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


def _totals(view: EstimateView, grid: Grid) -> EstimateTotalsOut:
    t = view.totals(grid)
    return EstimateTotalsOut(
        kept_original=money(t.kept_original),
        kept_change_orders=money(t.kept_change_orders),
        kept_total=money(t.kept_total),
        omitted=money(t.omitted),
        kept_hours=money(t.kept_hours),
        kept_cost=money(t.kept_cost),
        eac_in_basis=money(t.eac_in_basis),
        basis_decided=t.basis_decided,
        by_category=[
            EstimateCategoryTotalOut(
                slot=c.slot,
                name=c.name,
                amount=money(c.amount),
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
    baseline = next((vv.version.version_no for vv in versions if vv.version.is_baseline), None)
    return EstimateDetailOut(
        **_row(view).model_dump(),
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
        work_areas=[_work_area(w) for w in view.work_areas or ()],
        totals=_totals(view, grid),
    )
