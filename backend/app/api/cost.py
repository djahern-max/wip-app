"""The Cost page (F10; BLUEPRINT §8.5 check 2, §11; D-22, D-47, D-49, D-50, D-51): a firm
screen read by the roles that read Connections (``can_view_cost``: ``firm_admin``,
``firm_staff``, ``client_admin``). The tie-out by month and account with its five parts
and the ledger total, the unassigned list by document, labor in the ledger by month and
division, and the XLSX export. Everything is computed on read from ``ledger_line``, the
confirmed ``account_map`` and the tenant's WIP basis; nothing is stored. Money is strings
with cents; words say what a figure means.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.schemas import (
    CostLaborRowOut,
    CostPageOut,
    CostTieRowOut,
    CostUnassignedOut,
)
from app.core.auth import Principal, TenantSession
from app.core.authz import can_view_cost
from app.domain.config.policy import POLICY_KEYS, WIP_BASIS, history_start
from app.domain.cost import export, figures, report
from app.domain.cost.export import customer_words
from app.domain.cost.load import CostReport, cost_report, money_str
from app.domain.cost.models import TXN_TYPE_LABELS
from app.tenancy.models import Tenant

router = APIRouter(prefix="/cost", tags=["cost"])

Viewer = Annotated[Principal, Depends(can_view_cost)]


def _tenant_name(db: Session, tenant_id) -> str:
    row = db.get(Tenant, tenant_id)
    return row.name if row is not None else ""


def _policy_note(r: CostReport) -> str | None:
    if r.policy.basis is not None:
        return None
    label = POLICY_KEYS[WIP_BASIS].label
    return (
        f"The WIP basis is not decided (Configuration, Policy, {label}): every job-cost line "
        "on a job is shown as on jobs, and no job shows cost to date in the basis."
    )


def _skipped(db: Session, tenant_id, start) -> int:
    return sum(report.skipped_counts(db, tenant_id, history_start=start).values())


def _tie_row(t: figures.CostTieRow) -> CostTieRowOut:
    return CostTieRowOut(
        month=t.month,
        account_key=t.account.key,
        account_label=t.account.label,
        in_chart=t.account.in_chart,
        on_jobs=money_str(t.on_jobs) or "0.00",
        memo_on_jobs=money_str(t.memo) or "0.00",
        unassigned=money_str(t.unassigned) or "0.00",
        labor_in_ledger=money_str(t.labor) or "0.00",
        outside_job_cost=money_str(t.outside) or "0.00",
        total=money_str(t.total) or "0.00",
        difference=money_str(t.difference) or "0.00",
        balanced=t.balanced,
    )


def _page(db: Session, tenant_id) -> tuple[CostReport, int, str | None]:
    r = cost_report(db, tenant_id)
    start = history_start(db)
    skipped = _skipped(db, tenant_id, start)
    return r, skipped, start.isoformat() if start else None


@router.get("", response_model=CostPageOut)
def cost_page(viewer: Viewer, db: TenantSession):
    r, skipped, start = _page(db, viewer.active_tenant_id)
    return CostPageOut(
        tenant_name=_tenant_name(db, viewer.active_tenant_id),
        as_of=r.as_of.isoformat(),
        history_start=start,
        status=figures.tie_out_status(r.tie_out, skipped=skipped),
        balanced=r.balanced and bool(r.tie_out),
        months=len({t.month for t in r.tie_out}),
        accounts=len({t.account.key for t in r.tie_out}),
        skipped=skipped,
        lines_held=r.lines_held,
        policy_note=_policy_note(r),
        memo_label=figures.MEMO_LABEL,
        tie_out=[_tie_row(t) for t in r.tie_out],
        unassigned=[
            CostUnassignedOut(
                txn_type=u.doc.txn_type,
                txn_type_label=TXN_TYPE_LABELS.get(u.doc.txn_type, u.doc.txn_type),
                txn_external_id=u.doc.txn_external_id,
                document=u.doc.document,
                txn_date=u.doc.txn_date.isoformat(),
                vendor=u.vendor_name,
                accounts=", ".join(u.doc.account_labels),
                amount=money_str(u.doc.amount) or "0.00",
                customer_label=customer_words(u),
                customer_id=u.customer_id,
                customer_tracked=u.customer_tracked,
                lines=u.doc.lines,
            )
            for u in r.unassigned
        ],
        labor=[
            CostLaborRowOut(
                month=row.month,
                division_code=row.division_code,
                division_name=row.division_name,
                amount=money_str(row.amount) or "0.00",
            )
            for row in r.labor
        ],
    )


@router.get("/export.xlsx", include_in_schema=True)
def export_xlsx(viewer: Viewer, db: TenantSession):
    """The tie-out tab and the unassigned tab, values from Decimal (the F08 writer)."""
    r, skipped, _start = _page(db, viewer.active_tenant_id)
    body = export.cost_xlsx(
        export.CostExport(
            tenant_name=_tenant_name(db, viewer.active_tenant_id),
            as_of=r.as_of,
            status=figures.tie_out_status(r.tie_out, skipped=skipped),
            policy_note=_policy_note(r),
            tie_out=tuple(r.tie_out),
            unassigned=tuple(r.unassigned),
        )
    )
    stem = f"cost-{r.as_of.isoformat()}"
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'},
    )
