"""Test-only routes, mounted by ``create_app`` only when running under pytest.

One GET per named capability (``app.core.authz.CAPABILITIES``) plus a pay-rate
probe and a tenant-scoped row probe, so the role matrix and the "tenant comes
from the session" rule can be proven before the real endpoints exist.
"""

from collections.abc import Callable

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.auth import TenantSession
from app.core.authz import CAPABILITIES, can_view_pay_rates
from app.tenancy.models import RlsProbe


def _ok(name: str) -> Callable[[], dict]:
    def probe() -> dict:
        return {"capability": name, "ok": True}

    probe.__name__ = f"probe_{name}"
    return probe


def build_probe_router() -> APIRouter:
    router = APIRouter(prefix="/_probe", tags=["_probe"])
    for name, (guard, _roles, _scope) in CAPABILITIES.items():
        router.add_api_route(
            f"/cap/{name}", _ok(name), methods=["GET"], dependencies=[Depends(guard)]
        )

    @router.get("/pay-rates", dependencies=[Depends(can_view_pay_rates)])
    def pay_rates() -> dict:
        """Stand-in for the F11 pay-rate endpoints: no real rates exist yet."""
        return {"pay_rates": []}

    @router.get("/rows")
    def rows(session: TenantSession) -> list[str]:
        """Reads the probe table through the session-derived tenant context."""
        return list(session.execute(select(RlsProbe.label).order_by(RlsProbe.label)).scalars())

    return router
