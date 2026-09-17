"""Audit rows: right table, right tenant, atomic with the action (D-12, D-13)."""

import re
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from app.audit.models import AuditLog
from app.auth import service
from app.core.audit import RequestMeta, write_tenant_audit
from app.core.db import tenant_session
from app.tenancy.models import Membership, Role
from tests.conftest import CSRF, Seed
from tests.test_auth import firm_events, tenant_events

API_DIR = Path(__file__).resolve().parents[1] / "app" / "api"


def test_tenant_a_cannot_read_tenant_b_audit_rows(
    seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    marker = uuid.uuid4().hex
    with tenant_session(owner_engine, seed.tenant_b) as s:
        write_tenant_audit(
            s,
            tenant_id=seed.tenant_b,
            action="tenant_enter",
            entity_type="probe",
            entity_id=marker,
            actor_user_id=None,
            actor_role=None,
        )
    with tenant_session(rw_engine, seed.tenant_a) as s:
        tenant_ids = {r.tenant_id for r in s.execute(select(AuditLog)).scalars()}
        by_marker = s.execute(select(AuditLog).where(AuditLog.entity_id == marker)).scalars().all()
        n_raw = s.execute(
            text("SELECT count(*) FROM audit_log WHERE tenant_id = :b"), {"b": seed.tenant_b}
        ).scalar_one()
    assert tenant_ids == {seed.tenant_a}
    assert by_marker == []
    assert n_raw == 0


def test_tenant_switch_is_audited_in_the_entered_tenant(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    su = seed.users["firm_admin"]
    c = login_as("firm_admin", tenant=None)
    before_b = len(
        [
            e
            for e in tenant_events(rw_engine, seed.tenant_b, "tenant_enter")
            if e.actor_user_id == su.id
        ]
    )
    r = c.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_b)}, headers=CSRF)
    assert r.status_code == 200
    entered = [
        e
        for e in tenant_events(rw_engine, seed.tenant_b, "tenant_enter")
        if e.actor_user_id == su.id
    ]
    assert len(entered) == before_b + 1
    assert entered[-1].actor_role == Role.firm_admin
    assert entered[-1].detail == {"via": "switcher"}
    assert entered[-1].request_id == r.headers["X-Request-Id"]
    assert entered[-1].ip == "testclient"


def test_membership_role_change_is_audited_and_atomic(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    pm = seed.users["client_pm"]
    admin = login_as("firm_admin")
    url = f"/api/admin/tenants/{seed.tenant_a}/memberships/{pm.id}"
    r = admin.put(url, json={"role": "client_viewer"}, headers=CSRF)
    assert r.status_code == 200
    changed = [
        e
        for e in tenant_events(rw_engine, seed.tenant_a, "membership_role_changed")
        if e.detail.get("user_id") == str(pm.id)
    ]
    assert changed[-1].detail["old_role"] == "client_pm"
    assert changed[-1].detail["new_role"] == "client_viewer"
    assert changed[-1].actor_role == Role.firm_admin
    # Roll back after the service wrote both the change and its audit row:
    # neither survives.
    count_before = len(changed)
    with pytest.raises(RuntimeError, match="simulated"):
        with tenant_session(owner_engine, seed.tenant_a) as s:
            service.change_membership_role(
                s,
                None,
                tenant_id=seed.tenant_a,
                user_id=pm.id,
                role=Role.client_admin,
                meta=RequestMeta(),
            )
            raise RuntimeError("simulated failure after the write")
    with tenant_session(rw_engine, seed.tenant_a) as s:
        role = s.execute(select(Membership.role).where(Membership.user_id == pm.id)).scalar_one()
    assert role == Role.client_viewer
    changed_after = [
        e
        for e in tenant_events(rw_engine, seed.tenant_a, "membership_role_changed")
        if e.detail.get("user_id") == str(pm.id)
    ]
    assert len(changed_after) == count_before
    # restore
    r = admin.put(url, json={"role": "client_pm"}, headers=CSRF)
    assert r.status_code == 200


def test_login_events_land_in_firm_audit_log(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient]
) -> None:
    su = seed.users["firm_staff"]
    login_as("firm_staff")
    success = firm_events(rw_engine, su.id, "login_success")
    assert success[-1].firm_id == seed.firm_id
    assert success[-1].actor_role == Role.firm_staff
    assert success[-1].detail["method"] == "password+totp"


def test_firm_audit_route_shows_firm_events_to_firm_roles_only(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    staff = login_as("firm_staff")
    rows = staff.get("/api/firm-audit?limit=500").json()
    actions = {r["action"] for r in rows}
    assert {"login_success", "login_failure"} <= actions
    assert all(r["firm_id"] in (str(seed.firm_id), None) for r in rows)
    for key in ("client_admin", "client_pm", "client_viewer"):
        assert login_as(key).get("/api/firm-audit").status_code == 403


def test_tenant_audit_route_is_scoped(login_as: Callable[..., TestClient], seed: Seed) -> None:
    ca = login_as("client_admin")
    rows = ca.get("/api/audit").json()
    assert rows and all(r["tenant_id"] == str(seed.tenant_a) for r in rows)
    assert any(r["action"] == "tenant_enter" for r in rows)
    assert login_as("client_pm").get("/api/audit").status_code == 403


def test_firm_audit_log_is_only_read_through_the_guarded_route() -> None:
    """No other router touches the table (belt and braces for the matrix)."""
    users = [p.name for p in API_DIR.glob("*.py") if re.search(r"\bFirmAuditLog\b", p.read_text())]
    assert users == ["audit.py"]
    text = (API_DIR / "audit.py").read_text()
    assert "can_read_firm_audit" in text


def test_audit_detail_refuses_secret_keys(seed: Seed, owner_engine: Engine) -> None:
    with pytest.raises(ValueError, match="must not contain"):
        with tenant_session(owner_engine, seed.tenant_a) as s:
            write_tenant_audit(
                s,
                tenant_id=seed.tenant_a,
                action="tenant_enter",
                entity_type="x",
                entity_id=None,
                actor_user_id=None,
                actor_role=None,
                detail={"password": "x"},
            )
