"""firm_admin administration (F02): users, memberships, password and TOTP resets.

Firm-level routes need no active tenant. Routes that write to a tenant name it in
the path; the handler enters that tenant for the write (``enter_tenant_for_admin``)
so the membership row and its audit row land in one tenant, in one transaction.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth import service
from app.auth.service import AuthError
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession
from app.core.authz import can_list_tenant_users, can_manage_memberships, can_manage_users
from app.core.db import RequestSession
from app.core.security import MIN_PASSWORD_LENGTH
from app.tenancy.models import Membership, Role, User

router = APIRouter(prefix="/admin", tags=["admin"])


class UserIn(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    password: Annotated[str | None, Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)] = None


class MembershipIn(BaseModel):
    user_id: UUID
    role: Role


class RoleIn(BaseModel):
    role: Role


def _user(db, user_id: UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise AuthError(404, "user not found")
    return user


def _firm_of(actor: Principal) -> UUID | None:
    ids = sorted(actor.firm_ids, key=str)
    return ids[0] if ids else None


@router.post("/users", status_code=201)
def create_user(
    body: UserIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_users)],
    db: RequestSession,
) -> dict:
    """Create a user. Without a password, a one-time reset link is issued and
    returned to the admin to hand over; no e-mail is sent in F02."""
    meta = request_meta(request)
    user = service.create_user(
        db,
        email=body.email,
        display_name=body.display_name,
        password=body.password,
        actor=actor,
        firm_id=_firm_of(actor),
        meta=meta,
    )
    out = {"id": str(user.id), "email": user.email, "display_name": user.display_name}
    if body.password is None:
        out["password_reset_url"] = service.issue_password_reset(db, actor, user, meta=meta)
    return out


@router.get("/users")
def list_tenant_users(
    _actor: Annotated[Principal, Depends(can_list_tenant_users)], db: TenantSession
) -> list[dict]:
    """Users of the active tenant (RLS scopes the membership rows)."""
    rows = db.execute(
        select(Membership, User).join(User, User.id == Membership.user_id).order_by(User.email)
    ).all()
    return [
        {
            "user_id": str(u.id),
            "email": u.email,
            "display_name": u.display_name,
            "role": m.role.value,
            "totp_enrolled": u.totp_enrolled_at is not None,
        }
        for m, u in rows
    ]


@router.post("/users/{user_id}/password-reset")
def password_reset(
    user_id: UUID,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_users)],
    db: RequestSession,
) -> dict:
    url = service.issue_password_reset(db, actor, _user(db, user_id), meta=request_meta(request))
    return {"password_reset_url": url}


@router.post("/users/{user_id}/totp-reset", status_code=204)
def totp_reset(
    user_id: UUID,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_users)],
    db: RequestSession,
) -> Response:
    service.reset_totp(db, actor, _user(db, user_id), meta=request_meta(request), via="admin")
    return Response(status_code=204)


@router.post("/tenants/{tenant_id}/memberships", status_code=201)
def create_membership(
    tenant_id: UUID,
    body: MembershipIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_memberships)],
    db: RequestSession,
) -> dict:
    service.enter_tenant_for_admin(db, actor, tenant_id)
    m = service.create_membership(
        db,
        actor,
        tenant_id=tenant_id,
        user=_user(db, body.user_id),
        role=body.role,
        meta=request_meta(request),
    )
    return {
        "membership_id": str(m.id),
        "tenant_id": str(tenant_id),
        "user_id": str(m.user_id),
        "role": m.role.value,
    }


@router.put("/tenants/{tenant_id}/memberships/{user_id}")
def change_membership_role(
    tenant_id: UUID,
    user_id: UUID,
    body: RoleIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_memberships)],
    db: RequestSession,
) -> dict:
    service.enter_tenant_for_admin(db, actor, tenant_id)
    m = service.change_membership_role(
        db, actor, tenant_id=tenant_id, user_id=user_id, role=body.role, meta=request_meta(request)
    )
    return {
        "membership_id": str(m.id),
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "role": m.role.value,
    }


@router.delete("/tenants/{tenant_id}/memberships/{user_id}", status_code=204)
def remove_membership(
    tenant_id: UUID,
    user_id: UUID,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_memberships)],
    db: RequestSession,
) -> Response:
    service.enter_tenant_for_admin(db, actor, tenant_id)
    service.remove_membership(
        db, actor, tenant_id=tenant_id, user_id=user_id, meta=request_meta(request)
    )
    return Response(status_code=204)
