"""Change orders (F07.4, D-42): the unapproved change orders list, read by every role
(``can_view_jobs``, as the job pages). Nothing here writes; every figure is computed on
read. Money is strings with cents (D-22)."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.schemas import UnapprovedChangeOrderOut, UnapprovedChangeOrdersOut
from app.core.auth import Principal, TenantSession
from app.core.authz import can_view_jobs
from app.domain.estimates.service import money
from app.domain.jobs import service
from app.tenancy.models import Tenant

router = APIRouter(prefix="/change-orders", tags=["change-orders"])

Viewer = Annotated[Principal, Depends(can_view_jobs)]


def _tenant_name(db: Session, tenant_id) -> str:
    row = db.get(Tenant, tenant_id)
    return row.name if row is not None else ""


@router.get("/unapproved", response_model=UnapprovedChangeOrdersOut)
def unapproved(viewer: Viewer, db: TenantSession):
    """Every kept, confirmed, unapproved change-order work area on a job that is sold, in
    progress or substantially complete, with its amount and age, and a total."""
    rows = service.unapproved_change_orders(db, viewer.active_tenant_id)
    return UnapprovedChangeOrdersOut(
        tenant_name=_tenant_name(db, viewer.active_tenant_id),
        as_of=service.tenant_today(db).isoformat(),
        rows=[
            UnapprovedChangeOrderOut(
                job_id=str(r.job_id),
                job_name=r.job_name,
                estimate_number=r.estimate_number,
                estimator=r.estimator,
                order_no=r.order_no,
                name=r.name,
                price=money(r.price),
                first_seen=r.first_seen.isoformat(),
                days=r.days,
                approval_ended=r.approval_ended,
            )
            for r in rows
        ],
        total=money(sum((r.price for r in rows), Decimal("0.00"))),
        count=len(rows),
    )
