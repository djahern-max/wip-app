"""Role matrix (F02 acceptance; F02.1 routes added): 5 roles × every protected route,
cell by cell, plus 401 for an unauthenticated request on every one. Firm roles are
read from ``firm_membership``; the firm users' rows in tenants are entry rows."""

import hashlib
import io
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select

from app.core.authz import CAPABILITIES
from app.core.db import tenant_session, untenanted_session
from app.core.storage import LocalObjectStore
from app.ingest.models import ImportBatch
from app.tenancy.models import FirmMembership, Membership, Role
from tests._env import OBJECT_STORE_DIR
from tests.conftest import CSRF, Seed, full_login, make_client

ROLES = ["firm_admin", "firm_staff", "client_admin", "client_pm", "client_viewer"]
FA, FS, CA, PM, CV = ROLES
ALL = frozenset(ROLES)
OK = {200, 201, 204}


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # may contain {B} (tenant B) and {scratch} (scratch user id)
    allowed: frozenset[str]
    body: Callable[[], dict] | None = None
    # scratch user's state before each cell: "member" (client_viewer in B),
    # "no_member" (nothing anywhere), "firm_member" (firm_staff, no client rows)
    prepare: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    # F03: a multipart upload; (form fields, files) built fresh per cell.
    multipart: Callable[[], tuple[dict, dict]] | None = None


def _routes() -> list[Route]:
    routes = [
        Route("GET", "/api/_probe/rows", ALL),
        Route("GET", "/api/_probe/pay-rates", frozenset({FA, FS, CA}), tags=("pay_rates",)),
        Route("GET", "/api/audit", frozenset({FA, FS, CA})),
        Route("GET", "/api/firm-audit", frozenset({FA, FS})),
        Route("GET", "/api/session/tenants", ALL),
        # F03: imports (can_manage_imports); {batch} is a batch seeded in tenant A.
        Route("GET", "/api/imports", frozenset({FA, FS, CA})),
        Route("GET", "/api/imports/source-kinds", frozenset({FA, FS, CA})),
        Route("GET", "/api/imports/{batch}", frozenset({FA, FS, CA})),
        Route("GET", "/api/imports/{batch}/download", frozenset({FA, FS, CA})),
        Route(
            "POST",
            "/api/imports",
            frozenset({FA, FS, CA}),
            multipart=lambda: (
                {"source_kind": "unparsed_file"},
                {"file": ("matrix.bin", uuid.uuid4().bytes, "application/octet-stream")},
            ),
        ),
        Route("GET", "/api/admin/users", frozenset({FA})),
        Route(
            "POST",
            "/api/admin/users",
            frozenset({FA}),
            body=lambda: {
                "email": f"matrix-{uuid.uuid4().hex[:8]}@example.test",
                "display_name": "Matrix",
            },
        ),
        Route("POST", "/api/admin/users/{scratch}/activation-link", frozenset({FA})),
        Route("POST", "/api/admin/users/{scratch}/totp-reset", frozenset({FA})),
        Route(
            "POST",
            "/api/admin/tenants",
            frozenset({FA}),
            body=lambda: {"name": "Matrix Co", "slug": f"matrix-{uuid.uuid4().hex[:8]}"},
        ),
        Route(
            "POST",
            "/api/admin/tenants/{B}/memberships",
            frozenset({FA}),
            body=lambda: {"user_id": "{scratch}", "role": "client_viewer"},
            prepare="no_member",
        ),
        Route(
            "PUT",
            "/api/admin/tenants/{B}/memberships/{scratch}",
            frozenset({FA}),
            body=lambda: {"role": "client_pm"},
            prepare="member",
        ),
        Route(
            "DELETE",
            "/api/admin/tenants/{B}/memberships/{scratch}",
            frozenset({FA}),
            prepare="member",
        ),
        Route("GET", "/api/admin/firm-memberships", frozenset({FA})),
        Route(
            "POST",
            "/api/admin/firm-memberships",
            frozenset({FA}),
            body=lambda: {"user_id": "{scratch}", "role": "firm_staff"},
            prepare="no_member",
        ),
        Route(
            "PUT",
            "/api/admin/firm-memberships/{scratch}",
            frozenset({FA}),
            body=lambda: {"role": "firm_admin"},
            prepare="firm_member",
        ),
        Route(
            "DELETE",
            "/api/admin/firm-memberships/{scratch}",
            frozenset({FA}),
            prepare="firm_member",
        ),
    ]
    for name, (_guard, roles, _scope) in CAPABILITIES.items():
        routes.append(Route("GET", f"/api/_probe/cap/{name}", frozenset(r.value for r in roles)))
    return routes


ROUTES = _routes()


@pytest.fixture(scope="module")
def role_clients(seed: Seed, owner_engine: Engine) -> Iterator[dict[str, TestClient]]:
    """One logged-in client per role, tenant A active, shared by the module."""
    clients: dict[str, TestClient] = {}
    for key in ROLES:
        c = make_client()
        c.__enter__()
        full_login(c, seed, key, owner_engine, seed.tenant_a)
        clients[key] = c
    yield clients
    for c in clients.values():
        c.__exit__(None, None, None)


def _prepare(seed: Seed, owner_engine: Engine, state: str | None) -> None:
    scratch = seed.users["scratch"].id
    for tid in (seed.tenant_a, seed.tenant_b, seed.tenant_new):
        with tenant_session(owner_engine, tid) as s:
            s.execute(delete(Membership).where(Membership.user_id == scratch))
    with untenanted_session(owner_engine) as s:
        s.execute(delete(FirmMembership).where(FirmMembership.user_id == scratch))
        if state == "firm_member":
            s.add(FirmMembership(firm_id=seed.firm_id, user_id=scratch, role=Role.firm_staff))
    if state == "member":
        with tenant_session(owner_engine, seed.tenant_b) as s:
            s.add(Membership(tenant_id=seed.tenant_b, user_id=scratch, role=Role.client_viewer))


_BATCH_ID: dict[str, str] = {}


def _seed_batch(seed: Seed, owner_engine: Engine) -> str:
    """One import batch in tenant A with a real object, for the {batch} routes."""
    if "id" not in _BATCH_ID:
        content = b"role-matrix"
        sha = hashlib.sha256(content).hexdigest()
        LocalObjectStore(OBJECT_STORE_DIR).put(
            seed.tenant_a, f"imports/{sha}.bin", io.BytesIO(content)
        )
        with tenant_session(owner_engine, seed.tenant_a) as s:
            batch = ImportBatch(
                tenant_id=seed.tenant_a,
                source_kind="unparsed_file",
                sha256=sha,
                byte_size=len(content),
                original_filename="matrix.bin",
                object_key=f"tenant/{seed.tenant_a}/imports/{sha}.bin",
            )
            s.add(batch)
            s.flush()
            _BATCH_ID["id"] = str(batch.id)
    return _BATCH_ID["id"]


def _fill(route: Route, seed: Seed, owner_engine: Engine | None = None) -> tuple[str, dict | None]:
    scratch = str(seed.users["scratch"].id)
    path = route.path.replace("{B}", str(seed.tenant_b)).replace("{scratch}", scratch)
    if "{batch}" in path:
        batch = _seed_batch(seed, owner_engine) if owner_engine is not None else str(uuid.uuid4())
        path = path.replace("{batch}", batch)
    body = None
    if route.body is not None:
        body = {
            k: (v.replace("{scratch}", scratch) if isinstance(v, str) else v)
            for k, v in route.body().items()
        }
    return path, body


@pytest.mark.parametrize("route", ROUTES, ids=[f"{r.method} {r.path}" for r in ROUTES])
def test_role_matrix_cell_by_cell(
    route: Route, role_clients: dict[str, TestClient], seed: Seed, owner_engine: Engine
) -> None:
    failures: list[str] = []
    for role in ROLES:
        _prepare(seed, owner_engine, route.prepare)
        path, body = _fill(route, seed, owner_engine)
        if route.multipart is not None:
            data, files = route.multipart()
            r = role_clients[role].request(route.method, path, data=data, files=files, headers=CSRF)
        else:
            r = role_clients[role].request(route.method, path, json=body, headers=CSRF)
        expected = "2xx" if role in route.allowed else "403"
        got = "2xx" if r.status_code in OK else str(r.status_code)
        if got != expected:
            failures.append(f"{role}: expected {expected}, got {r.status_code} {r.text[:80]}")
    _prepare(seed, owner_engine, None)
    assert not failures, f"{route.method} {route.path}\n" + "\n".join(failures)


@pytest.mark.parametrize("route", ROUTES, ids=[f"{r.method} {r.path}" for r in ROUTES])
def test_unauthenticated_gets_401(route: Route, client: TestClient, seed: Seed) -> None:
    path, body = _fill(route, seed)
    if route.multipart is not None:
        data, files = route.multipart()
        r = client.request(route.method, path, data=data, files=files, headers=CSRF)
    else:
        r = client.request(route.method, path, json=body, headers=CSRF)
    assert r.status_code == 401, r.text


def test_pay_rate_probe(role_clients: dict[str, TestClient]) -> None:
    """CLAUDE.md Security: pay rates only for firm_admin, firm_staff, client_admin."""
    statuses = {role: role_clients[role].get("/api/_probe/pay-rates").status_code for role in ROLES}
    assert statuses == {FA: 200, FS: 200, CA: 200, PM: 403, CV: 403}


def test_every_capability_has_a_probe_and_a_matrix_row() -> None:
    probed = {r.path.rsplit("/", 1)[-1] for r in ROUTES if r.path.startswith("/api/_probe/cap/")}
    assert probed == set(CAPABILITIES)


def test_switching_tenant_without_membership_is_403_and_leaves_session(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    c = login_as("client_pm")
    r = c.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_b)}, headers=CSRF)
    assert (r.status_code, r.json()) == (403, {"detail": "no membership in that tenant"})
    assert c.get("/api/session/me").json()["active_tenant_id"] == str(seed.tenant_a)
    assert c.get("/api/_probe/rows").json() == ["a-row"]
    r = c.post("/api/session/tenant", json={"tenant_id": str(uuid.uuid4())}, headers=CSRF)
    assert r.status_code == 403


def test_firm_user_switches_between_tenants(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    c = login_as("firm_staff", tenant=None)
    tenants = c.get("/api/session/tenants").json()
    assert {t["tenant_id"] for t in tenants} == {str(seed.tenant_a), str(seed.tenant_b)}
    assert c.get("/api/session/me").json()["active_tenant_id"] is None
    assert c.get("/api/_probe/rows").status_code == 400  # no tenant chosen yet
    r = c.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_b)}, headers=CSRF)
    assert r.json() == {"active_tenant_id": str(seed.tenant_b), "role": "firm_staff"}
    assert c.get("/api/_probe/rows").json() == ["b-row"]
    c.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_a)}, headers=CSRF)
    assert c.get("/api/_probe/rows").json() == ["a-row"]


def test_removed_membership_drops_the_active_tenant(
    login_as: Callable[..., TestClient], seed: Seed, owner_engine: Engine
) -> None:
    scratch = seed.users["scratch"]
    with tenant_session(owner_engine, seed.tenant_b) as s:
        s.execute(delete(Membership).where(Membership.user_id == scratch.id))
        s.add(Membership(tenant_id=seed.tenant_b, user_id=scratch.id, role=Role.client_viewer))
    c = login_as("scratch", tenant=seed.tenant_b)
    assert c.get("/api/_probe/rows").json() == ["b-row"]
    with tenant_session(owner_engine, seed.tenant_b) as s:
        s.execute(delete(Membership).where(Membership.user_id == scratch.id))
    assert c.get("/api/session/me").json()["active_tenant_id"] is None
    assert c.get("/api/_probe/rows").status_code == 400
    with tenant_session(owner_engine, seed.tenant_b) as s:
        assert s.execute(select(Membership).where(Membership.user_id == scratch.id)).first() is None
