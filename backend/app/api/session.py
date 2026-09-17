"""Who am I, which tenants can I enter, enter one (F02). The active tenant lives in
the server-side session; nothing here trusts a header."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.auth import session_payload
from app.auth import service
from app.core.audit import request_meta
from app.core.auth import AnyPrincipal, VerifiedPrincipal
from app.core.db import RequestSession

router = APIRouter(prefix="/session", tags=["session"])


class TenantIn(BaseModel):
    tenant_id: UUID


@router.get("/me")
def me(principal: AnyPrincipal) -> dict:
    return session_payload(principal)


@router.get("/tenants")
def tenants(principal: VerifiedPrincipal, db: RequestSession) -> list[dict]:
    return [
        {"tenant_id": str(t.id), "name": t.name, "slug": t.slug, "role": m.role.value}
        for m, t in service.tenants_for(db, principal)
    ]


@router.post("/tenant")
def enter_tenant(
    body: TenantIn, request: Request, principal: VerifiedPrincipal, db: RequestSession
) -> dict:
    m = service.set_active_tenant(db, principal, body.tenant_id, meta=request_meta(request))
    return {"active_tenant_id": str(body.tenant_id), "role": m.role.value}
