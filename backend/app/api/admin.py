"""firm_admin administration (F02, F02.1): users and activation links, tenants,
firm memberships, memberships and entry rows, TOTP resets.

Firm-level routes need no active tenant. Routes that write to a tenant name it in
the path; the handler enters that tenant for the write (``enter_tenant_for_admin``)
so the membership row and its audit row land in one tenant, in one transaction.
The firm is always the actor's own (D-17); it never comes from the request.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.api.schemas import (
    ActivationLinkOut,
    FirmMembershipOut,
    MembershipOut,
    TenantCreatedOut,
    TenantUserOut,
    UserCreatedOut,
)
from app.auth import admin
from app.auth.service import AuthError
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession
from app.core.authz import (
    can_list_tenant_users,
    can_manage_firm_memberships,
    can_manage_memberships,
    can_manage_tenants,
    can_manage_users,
)
from app.core.db import AppEngine, RequestSession
from app.tenancy.models import Role, Tenant

router = APIRouter(prefix="/admin", tags=["admin"])


class UserIn(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=320)]
    display_name: Annotated[str, Field(min_length=1, max_length=200)]


class TenantIn(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    slug: Annotated[str, Field(min_length=1, max_length=80)]


class MembershipIn(BaseModel):
    user_id: UUID
    role: Role | None = None  # None: entry row for a firm user (D-15)


class RoleIn(BaseModel):
    role: Role


class FirmMembershipIn(BaseModel):
    user_id: UUID
    role: Role


def _membership_out(m, tenant_id: UUID) -> MembershipOut:
    return MembershipOut(
        membership_id=str(m.id),
        tenant_id=str(tenant_id),
        user_id=str(m.user_id),
        role=None,  # filled by the caller from the request, never from the column
    )


def _firm_membership_out(fm, user) -> FirmMembershipOut:
    return FirmMembershipOut(
        firm_membership_id=str(fm.id),
        firm_id=str(fm.firm_id),
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=fm.role.value,
        totp_enrolled=user.totp_enrolled_at is not None,
    )


# --- users and links ------------------------------------------------------------------------------


@router.post("/users", status_code=201, response_model=UserCreatedOut)
def create_user(
    body: UserIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_users)],
    db: RequestSession,
):
    """Create a user with no password and return the one-time activation link for
    the admin to hand over (D-16; no e-mail)."""
    user, url = admin.create_user_with_link(
        db,
        email=body.email,
        display_name=body.display_name,
        actor=actor,
        firm_id=actor.firm_id,
        meta=request_meta(request),
    )
    return UserCreatedOut(
        id=str(user.id), email=user.email, display_name=user.display_name, activation_url=url
    )


@router.post("/users/{user_id}/activation-link", response_model=ActivationLinkOut)
def activation_link(
    user_id: UUID,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_users)],
    db: RequestSession,
):
    """Admin password reset (D-16): a new link, which invalidates any earlier one
    and ends the user's sessions."""
    target = admin._user(db, user_id)
    url = admin.issue_activation_link(
        db,
        actor,
        target,
        firm_id=admin.firm_id_for_target(db, actor, target),
        meta=request_meta(request),
    )
    return ActivationLinkOut(activation_url=url)


@router.post("/users/{user_id}/totp-reset", response_model=ActivationLinkOut)
def totp_reset(
    user_id: UUID,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_users)],
    db: RequestSession,
):
    url = admin.reset_totp_and_issue_link(
        db, actor, admin._user(db, user_id), meta=request_meta(request), via="admin"
    )
    return ActivationLinkOut(activation_url=url)


@router.get("/users", response_model=list[TenantUserOut])
def list_tenant_users(
    actor: Annotated[Principal, Depends(can_list_tenant_users)], db: TenantSession
):
    """Users of the active tenant (RLS scopes the membership rows)."""
    tenant = db.get(Tenant, actor.active_tenant_id)
    return [
        TenantUserOut(
            user_id=str(u.id),
            email=u.email,
            display_name=u.display_name,
            role=role.value if role else None,
            totp_enrolled=u.totp_enrolled_at is not None,
        )
        for _m, u, role in admin.list_tenant_users(db, tenant)
    ]


# --- tenants (owner answer C) ---------------------------------------------------------------------


@router.post("/tenants", status_code=201, response_model=TenantCreatedOut)
def create_tenant(
    body: TenantIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_tenants)],
    db: RequestSession,
):
    t = admin.create_tenant(db, actor, name=body.name, slug=body.slug, meta=request_meta(request))
    return TenantCreatedOut(tenant_id=str(t.id), firm_id=str(t.firm_id), name=t.name, slug=t.slug)


# --- firm memberships (D-15) ----------------------------------------------------------------------


@router.get("/firm-memberships", response_model=list[FirmMembershipOut])
def list_firm_memberships(
    actor: Annotated[Principal, Depends(can_manage_firm_memberships)], db: RequestSession
):
    return [_firm_membership_out(fm, u) for fm, u in admin.list_firm_memberships(db, actor.firm_id)]


@router.post("/firm-memberships", status_code=201, response_model=FirmMembershipOut)
def create_firm_membership(
    body: FirmMembershipIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_firm_memberships)],
    db: RequestSession,
):
    user = admin._user(db, body.user_id)
    fm = admin.create_firm_membership(
        db, actor, firm_id=actor.firm_id, user=user, role=body.role, meta=request_meta(request)
    )
    return _firm_membership_out(fm, user)


@router.put("/firm-memberships/{user_id}", response_model=FirmMembershipOut)
def change_firm_membership_role(
    user_id: UUID,
    body: RoleIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_firm_memberships)],
    db: RequestSession,
):
    fm = admin.change_firm_membership_role(
        db,
        actor,
        firm_id=actor.firm_id,
        user_id=user_id,
        role=body.role,
        meta=request_meta(request),
    )
    return _firm_membership_out(fm, admin._user(db, user_id))


@router.delete("/firm-memberships/{user_id}", status_code=204)
def remove_firm_membership(
    user_id: UUID,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_firm_memberships)],
    engine: AppEngine,
) -> Response:
    """Owner answer A: commits the removal first, then cleans entry rows one tenant
    per transaction. Repeating the request finishes an interrupted cleanup."""
    done = admin.remove_firm_membership(
        engine, actor, firm_id=actor.firm_id, user_id=user_id, meta=request_meta(request)
    )
    if not done:
        raise AuthError(404, "firm_membership not found")
    return Response(status_code=204)


# --- memberships and entry rows -------------------------------------------------------------------


@router.post("/tenants/{tenant_id}/memberships", status_code=201, response_model=MembershipOut)
def create_membership(
    tenant_id: UUID,
    body: MembershipIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_memberships)],
    db: RequestSession,
):
    tenant = admin.enter_tenant_for_admin(db, actor, tenant_id)
    m = admin.create_membership(
        db,
        actor,
        tenant=tenant,
        user=admin._user(db, body.user_id),
        role=body.role,
        meta=request_meta(request),
    )
    out = _membership_out(m, tenant_id)
    out.role = body.role.value if body.role else None
    return out


@router.put("/tenants/{tenant_id}/memberships/{user_id}", response_model=MembershipOut)
def change_membership_role(
    tenant_id: UUID,
    user_id: UUID,
    body: RoleIn,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_memberships)],
    db: RequestSession,
):
    tenant = admin.enter_tenant_for_admin(db, actor, tenant_id)
    m = admin.change_membership_role(
        db, actor, tenant=tenant, user_id=user_id, role=body.role, meta=request_meta(request)
    )
    out = _membership_out(m, tenant_id)
    out.role = body.role.value
    return out


@router.delete("/tenants/{tenant_id}/memberships/{user_id}", status_code=204)
def remove_membership(
    tenant_id: UUID,
    user_id: UUID,
    request: Request,
    actor: Annotated[Principal, Depends(can_manage_memberships)],
    db: RequestSession,
) -> Response:
    tenant = admin.enter_tenant_for_admin(db, actor, tenant_id)
    admin.remove_membership(db, actor, tenant=tenant, user_id=user_id, meta=request_meta(request))
    return Response(status_code=204)
