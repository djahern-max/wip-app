"""Plain JSON reads of the audit tables (F02). Viewer UI is out of scope.

Rows of ``firm_audit_log`` with a null ``firm_id`` (unknown-e-mail login failures,
failed link redemptions, IP throttles) are shown to ``firm_admin`` only (D-17)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from app.api.schemas import AuditRowOut
from app.audit.models import AuditLog, FirmAuditLog
from app.core.auth import Principal, TenantSession
from app.core.authz import can_read_firm_audit, can_read_tenant_audit
from app.core.db import RequestSession
from app.tenancy.models import Role

router = APIRouter(tags=["audit"])

Limit = Annotated[int, Query(ge=1, le=500)]


def _row(r: AuditLog | FirmAuditLog, **scope: str | None) -> AuditRowOut:
    return AuditRowOut(
        id=str(r.id),
        occurred_at=r.occurred_at.isoformat(),
        actor_user_id=str(r.actor_user_id) if r.actor_user_id else None,
        actor_role=r.actor_role.value if r.actor_role else None,
        action=r.action,
        entity_type=r.entity_type,
        entity_id=r.entity_id,
        detail=r.detail,
        ip=r.ip,
        request_id=r.request_id,
        **scope,
    )


@router.get("/firm-audit", response_model=list[AuditRowOut])
def firm_audit(
    actor: Annotated[Principal, Depends(can_read_firm_audit)],
    db: RequestSession,
    limit: Limit = 200,
):
    """Events of the caller's firm. Null-firm rows for firm_admin only."""
    where = FirmAuditLog.firm_id.in_(list(actor.firm_ids))
    if actor.firm_role is Role.firm_admin:
        where = or_(where, FirmAuditLog.firm_id.is_(None))
    stmt = select(FirmAuditLog).where(where).order_by(FirmAuditLog.occurred_at.desc()).limit(limit)
    return [
        _row(r, firm_id=str(r.firm_id) if r.firm_id else None) for r in db.execute(stmt).scalars()
    ]


@router.get("/audit", response_model=list[AuditRowOut])
def tenant_audit(
    actor: Annotated[Principal, Depends(can_read_tenant_audit)],
    db: TenantSession,
    limit: Limit = 200,
):
    """Events of the active tenant (RLS scopes the rows)."""
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)
    return [_row(r, tenant_id=str(r.tenant_id)) for r in db.execute(stmt).scalars()]
