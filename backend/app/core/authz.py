"""Role checks as dependencies (F02). Routers name a capability from this module;
they never list roles inline (CLAUDE.md Security).

Two scopes:
- ``require_roles(*roles)``: the effective role in the session's **active tenant**
  (``app.core.auth.effective_role``). Needs a verified principal and an active
  tenant (400 without one).
- ``require_firm_role(*roles)``: the firm role held in ``firm_membership`` (D-15).
  Needs no active tenant; used by firm administration and the firm audit route.

Both return 401 for no session and 403 for the wrong role or an unmet TOTP
requirement (the 403 ``detail`` distinguishes them).
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException

from app.core.auth import Principal, get_verified_principal
from app.tenancy.models import Role

ALL_ROLES: tuple[Role, ...] = tuple(Role)
FIRM: tuple[Role, ...] = (Role.firm_admin, Role.firm_staff)
FIRM_AND_CLIENT_ADMIN: tuple[Role, ...] = (*FIRM, Role.client_admin)

Guard = Callable[..., Principal]


def require_roles(*roles: Role) -> Guard:
    allowed = frozenset(roles)

    def dependency(
        principal: Annotated[Principal, Depends(get_verified_principal)],
    ) -> Principal:
        if principal.active_tenant_id is None:
            raise HTTPException(status_code=400, detail="tenant context required")
        if principal.role not in allowed:
            raise HTTPException(status_code=403, detail="forbidden")
        return principal

    dependency.__name__ = f"require_roles[{','.join(r.value for r in roles)}]"
    return dependency


def require_firm_role(*roles: Role) -> Guard:
    allowed = frozenset(roles)

    def dependency(
        principal: Annotated[Principal, Depends(get_verified_principal)],
    ) -> Principal:
        if principal.firm_role not in allowed:
            raise HTTPException(status_code=403, detail="forbidden")
        return principal

    dependency.__name__ = f"require_firm_role[{','.join(r.value for r in roles)}]"
    return dependency


# --- named capabilities ---------------------------------------------------------------------------
# Tenant-scoped (effective role in the active tenant)
can_view_reports = require_roles(*ALL_ROLES)
can_view_pay_rates = require_roles(*FIRM_AND_CLIENT_ADMIN)  # CLAUDE.md Security
can_propose_eac = require_roles(*FIRM_AND_CLIENT_ADMIN, Role.client_pm)  # §11
can_approve_eac = require_roles(*FIRM_AND_CLIENT_ADMIN)  # §11: client_admin approves
can_reopen_period = require_roles(Role.firm_admin)  # CLAUDE.md: firm_admin action
can_read_tenant_audit = require_roles(*FIRM_AND_CLIENT_ADMIN)  # D-12: who accessed my company
can_list_tenant_users = require_roles(Role.firm_admin)
can_manage_imports = require_roles(*FIRM_AND_CLIENT_ADMIN)  # F03: upload, list, download files
# Firm-level (no active tenant needed)
can_manage_users = require_firm_role(Role.firm_admin)
can_manage_memberships = require_firm_role(Role.firm_admin)
can_manage_firm_memberships = require_firm_role(Role.firm_admin)  # D-15
can_manage_tenants = require_firm_role(Role.firm_admin)  # owner answer C
can_read_firm_audit = require_firm_role(*FIRM)

# Name → guard → allowed roles. The test-only probe router (tests/probes.py)
# mounts one route per entry; tests/test_roles.py asserts the matrix cell by cell.
CAPABILITIES: dict[str, tuple[Guard, frozenset[Role], str]] = {
    "view_reports": (can_view_reports, frozenset(ALL_ROLES), "tenant"),
    "view_pay_rates": (can_view_pay_rates, frozenset(FIRM_AND_CLIENT_ADMIN), "tenant"),
    "propose_eac": (
        can_propose_eac,
        frozenset((*FIRM_AND_CLIENT_ADMIN, Role.client_pm)),
        "tenant",
    ),
    "approve_eac": (can_approve_eac, frozenset(FIRM_AND_CLIENT_ADMIN), "tenant"),
    "reopen_period": (can_reopen_period, frozenset({Role.firm_admin}), "tenant"),
    "read_tenant_audit": (can_read_tenant_audit, frozenset(FIRM_AND_CLIENT_ADMIN), "tenant"),
    "list_tenant_users": (can_list_tenant_users, frozenset({Role.firm_admin}), "tenant"),
    "manage_imports": (can_manage_imports, frozenset(FIRM_AND_CLIENT_ADMIN), "tenant"),
    "manage_users": (can_manage_users, frozenset({Role.firm_admin}), "firm"),
    "manage_memberships": (can_manage_memberships, frozenset({Role.firm_admin}), "firm"),
    "manage_firm_memberships": (
        can_manage_firm_memberships,
        frozenset({Role.firm_admin}),
        "firm",
    ),
    "manage_tenants": (can_manage_tenants, frozenset({Role.firm_admin}), "firm"),
    "read_firm_audit": (can_read_firm_audit, frozenset(FIRM), "firm"),
}
