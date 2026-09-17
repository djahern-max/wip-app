"""Who am I, which tenants can I enter, enter one (F02). The active tenant lives in
the server-side session; nothing here trusts a header. Roles are the effective
roles (``app.core.auth.effective_role``); an orphan entry row is not listed and
cannot be entered."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.auth import session_payload
from app.api.schemas import SessionOut, TenantEnteredOut, TenantOut
from app.auth import service
from app.core.audit import request_meta
from app.core.auth import AnyPrincipal, VerifiedPrincipal
from app.core.db import RequestSession

router = APIRouter(prefix="/session", tags=["session"])


class TenantIn(BaseModel):
    tenant_id: UUID


@router.get("/me", response_model=SessionOut)
def me(principal: AnyPrincipal):
    return session_payload(principal)


@router.get("/tenants", response_model=list[TenantOut])
def tenants(principal: VerifiedPrincipal, db: RequestSession):
    return [
        TenantOut(tenant_id=str(t.id), name=t.name, slug=t.slug, role=role.value)
        for t, role in service.tenants_for(db, principal)
    ]


@router.post("/tenant", response_model=TenantEnteredOut)
def enter_tenant(
    body: TenantIn, request: Request, principal: VerifiedPrincipal, db: RequestSession
):
    role = service.set_active_tenant(db, principal, body.tenant_id, meta=request_meta(request))
    return TenantEnteredOut(active_tenant_id=str(body.tenant_id), role=role.value)
