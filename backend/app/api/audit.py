"""Plain JSON reads of the audit tables (F02). Viewer UI is out of scope."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from app.audit.models import AuditLog, FirmAuditLog
from app.core.auth import Principal, TenantSession
from app.core.authz import can_read_firm_audit, can_read_tenant_audit
from app.core.db import RequestSession

router = APIRouter(tags=["audit"])

Limit = Annotated[int, Query(ge=1, le=500)]


def _row(r: AuditLog | FirmAuditLog) -> dict:
    return {
        "id": str(r.id),
        "occurred_at": r.occurred_at.isoformat(),
        "actor_user_id": str(r.actor_user_id) if r.actor_user_id else None,
        "actor_role": r.actor_role.value if r.actor_role else None,
        "action": r.action,
        "entity_type": r.entity_type,
        "entity_id": r.entity_id,
        "detail": r.detail,
        "ip": r.ip,
        "request_id": r.request_id,
    }


@router.get("/firm-audit")
def firm_audit(
    actor: Annotated[Principal, Depends(can_read_firm_audit)],
    db: RequestSession,
    limit: Limit = 200,
) -> list[dict]:
    """Events of the caller's firm(s). Rows with no derivable firm (unknown-e-mail
    login failures) are included; see the F02 plan."""
    stmt = (
        select(FirmAuditLog)
        .where(or_(FirmAuditLog.firm_id.in_(list(actor.firm_ids)), FirmAuditLog.firm_id.is_(None)))
        .order_by(FirmAuditLog.occurred_at.desc())
        .limit(limit)
    )
    return [
        {**_row(r), "firm_id": str(r.firm_id) if r.firm_id else None}
        for r in db.execute(stmt).scalars()
    ]


@router.get("/audit")
def tenant_audit(
    actor: Annotated[Principal, Depends(can_read_tenant_audit)],
    db: TenantSession,
    limit: Limit = 200,
) -> list[dict]:
    """Events of the active tenant (RLS scopes the rows)."""
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)
    return [{**_row(r), "tenant_id": str(r.tenant_id)} for r in db.execute(stmt).scalars()]
