"""Accounting-policy keys (F04): no default anywhere, "not decided" until a firm_admin
sets a key with a decision reference, audited before and after."""

import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from app.core.db import tenant_session
from app.domain.config import policy
from app.domain.config.audit import Actor
from app.domain.config.categories import ensure_cost_categories
from app.domain.config.models import TenantPolicy
from tests.conftest import CSRF, Seed

BACKEND = Path(__file__).resolve().parents[1]


def test_unset_key_is_not_decided_and_require_policy_names_it(
    seed: Seed, rw_engine: Engine
) -> None:
    with tenant_session(rw_engine, seed.tenant_new) as s:
        for key in policy.POLICY_KEYS:
            assert policy.get_policy(s, key) is None
        with pytest.raises(
            policy.PolicyNotDecided, match="'wip_basis' has not been decided"
        ) as exc:
            policy.require_policy(s, "wip_basis")
        assert exc.value.key == "wip_basis"
        with pytest.raises(policy.PolicyValueError, match="unknown policy key"):
            policy.require_policy(s, "nope")


def test_no_policy_key_has_a_default_anywhere() -> None:
    """Static: the registry has no ``default`` field; a key name appears as a string
    literal only in the registry (``app/domain/config/policy.py``); no module under
    ``app/`` or ``alembic/`` assigns a key a value; the registry file has no
    ``default=`` and no ``DEFAULT`` constant."""
    assert "default" not in policy.PolicyKey.__dataclass_fields__
    files = sorted((BACKEND / "app").rglob("*.py")) + sorted((BACKEND / "alembic").rglob("*.py"))
    for key in policy.POLICY_KEYS:
        literal = re.compile(rf"""["']{re.escape(key)}["']""")
        for p in files:
            src = p.read_text()
            if not literal.search(src):
                continue
            rel = str(p.relative_to(BACKEND))
            assert rel == "app/domain/config/policy.py", f"{key} named in {rel}"
    registry = (BACKEND / "app" / "domain" / "config" / "policy.py").read_text()
    assert "default=" not in registry and "DEFAULT" not in registry


def test_fresh_tenant_has_zero_policy_rows(fresh_tenant, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, fresh_tenant) as s:
        assert s.execute(select(TenantPolicy)).first() is None


def test_setting_a_key_records_who_when_reference_and_audits_before_after(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    admin = login_as("rotate_me", tenant=fresh_tenant)
    r = admin.get("/api/config/policy")
    rows = {p["key"]: p for p in r.json()}
    assert set(rows) == set(policy.POLICY_KEYS)
    assert all(p["decided"] is False and p["value"] is None for p in rows.values())
    # firm_staff may read, not set.
    staff = login_as("recover_me", tenant=fresh_tenant)
    r = staff.put(
        "/api/config/policy/fiscal_year_start_month",
        json={"value": 1, "decision_ref": "engagement letter §3"},
        headers=CSRF,
    )
    assert r.status_code == 403
    r = admin.put(
        "/api/config/policy/fiscal_year_start_month",
        json={"value": 1, "decision_ref": "engagement letter §3"},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["decided"], body["value"], body["decision_ref"]) == (
        True,
        1,
        "engagement letter §3",
    )
    assert body["decided_by_email"] == seed.users["rotate_me"].email and body["decided_at"]
    # Money as a string in and out, Decimal in the middle.
    r = admin.put(
        "/api/config/policy/small_job_threshold",
        json={"value": "25000.5", "decision_ref": "owner, 2026-09-18"},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "25000.50"
    with tenant_session(rw_engine, fresh_tenant) as s:
        v = policy.require_policy(s, "small_job_threshold")
        assert isinstance(v, Decimal) and v == Decimal("25000.50")
        typ = s.execute(
            text("SELECT jsonb_typeof(value) FROM tenant_policy WHERE key = 'small_job_threshold'")
        ).scalar_one()
        assert typ == "number"
    # Change it: the audit row carries before and after.
    r = admin.put(
        "/api/config/policy/small_job_threshold",
        json={"value": "30000", "decision_ref": "owner, 2026-09-19"},
        headers=CSRF,
    )
    assert r.status_code == 200
    with tenant_session(rw_engine, fresh_tenant) as s:
        detail = s.execute(
            text(
                "SELECT detail FROM audit_log WHERE action = 'policy_set' AND entity_id = "
                "'small_job_threshold' ORDER BY occurred_at DESC LIMIT 1"
            )
        ).scalar_one()
    assert detail["before"] == {"value": "25000.50", "decision_ref": "owner, 2026-09-18"}
    assert detail["after"] == {"value": "30000.00", "decision_ref": "owner, 2026-09-19"}
    # Validation, in words.
    r = admin.put(
        "/api/config/policy/timezone",
        json={"value": "Mars/Olympus", "decision_ref": "x"},
        headers=CSRF,
    )
    assert r.status_code == 422 and "time zone" in r.json()["detail"]
    r = admin.put(
        "/api/config/policy/wip_basis",
        json={"value": ["10", "99"], "decision_ref": "x"},
        headers=CSRF,
    )
    assert r.status_code == 422 and "99" in r.json()["detail"]
    r = admin.put(
        "/api/config/policy/wip_basis",
        json={"value": ["20", "10"], "decision_ref": "D-04 draft"},
        headers=CSRF,
    )
    assert r.status_code == 200 and r.json()["value"] == ["10", "20"]
    r = admin.put("/api/config/policy/nope", json={"value": 1, "decision_ref": "x"}, headers=CSRF)
    assert r.status_code == 404
    r = admin.put(
        "/api/config/policy/timezone",
        json={"value": "America/New_York", "decision_ref": " "},
        headers=CSRF,
    )
    assert r.status_code == 422 and "decision reference" in r.json()["detail"]


def test_policy_rows_are_tenant_scoped(seed: Seed, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, seed.tenant_a) as s:
        ensure_cost_categories(s, seed.tenant_a)
        policy.set_policy(
            s, seed.tenant_a, "timezone", "America/New_York", decision_ref="t", actor=Actor()
        )
    with tenant_session(rw_engine, seed.tenant_b) as s:
        assert policy.get_policy(s, "timezone") is None
