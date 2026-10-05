"""Accounting-policy keys (F04): no default anywhere, "not decided" until a firm_admin
sets a key, audited before and after. F04.1: the reference is optional (blank stored as
""), a key whose feature has not arrived is refused by the route with its waiting
sentence, and the time zone is offered as a list the API labels."""

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
WAITING_KEYS = ("small_job_threshold", "deposit_identification", "fuel_surcharge_treatment")
US_ZONES = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
)


def _audit_rows(s, key: str) -> list[dict]:
    return list(
        s.execute(
            text(
                "SELECT detail FROM audit_log WHERE action = 'policy_set' AND entity_id = :k "
                "ORDER BY occurred_at"
            ),
            {"k": key},
        ).scalars()
    )


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
    # Money as a string in and out, Decimal in the middle. The key waits for its feature
    # (F04.1), so the value is stored through the service, as a later feature or a
    # seed would, and read back through the API.
    actor = Actor(user_id=seed.users["rotate_me"].id)
    with tenant_session(rw_engine, fresh_tenant) as s:
        policy.set_policy(
            s,
            fresh_tenant,
            "small_job_threshold",
            "25000.5",
            decision_ref="owner, 2026-09-18",
            actor=actor,
        )
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    money = rows["small_job_threshold"]
    assert money["decided"] is True and money["value"] == "25000.50"
    assert money["decided_by_email"] == seed.users["rotate_me"].email and money["decided_at"]
    assert money["decision_ref"] == "owner, 2026-09-18"
    with tenant_session(rw_engine, fresh_tenant) as s:
        v = policy.require_policy(s, "small_job_threshold")
        assert isinstance(v, Decimal) and v == Decimal("25000.50")
        typ = s.execute(
            text("SELECT jsonb_typeof(value) FROM tenant_policy WHERE key = 'small_job_threshold'")
        ).scalar_one()
        assert typ == "number"
        # Change it: the audit row carries before and after.
        policy.set_policy(
            s,
            fresh_tenant,
            "small_job_threshold",
            "30000",
            decision_ref="owner, 2026-09-19",
            actor=actor,
        )
    assert rows["small_job_threshold"]["value"] == "25000.50"
    with tenant_session(rw_engine, fresh_tenant) as s:
        detail = _audit_rows(s, "small_job_threshold")[-1]
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
    # F04.1: the reference is optional. Missing or blank, the row still records who and
    # when, the API reads no reference, and one audit row carries before and after.
    r = admin.put("/api/config/policy/timezone", json={"value": "America/New_York"}, headers=CSRF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decided"] is True and body["value"] == "America/New_York"
    assert body["decision_ref"] is None
    assert body["decided_by_email"] == seed.users["rotate_me"].email and body["decided_at"]
    with tenant_session(rw_engine, fresh_tenant) as s:
        row = policy.get_policy(s, "timezone")
        assert row.decided_by == seed.users["rotate_me"].id and row.decided_at
        assert row.decision_ref == ""
        audits = _audit_rows(s, "timezone")
    assert len(audits) == 1
    assert "before" not in audits[0]  # a first set has no before, as today
    assert audits[0]["after"] == {"value": "America/New_York", "decision_ref": ""}
    r = admin.put(
        "/api/config/policy/timezone",
        json={"value": "America/Chicago", "decision_ref": " "},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text
    assert r.json()["decision_ref"] is None
    with tenant_session(rw_engine, fresh_tenant) as s:
        assert policy.get_policy(s, "timezone").decision_ref == ""
        audits = _audit_rows(s, "timezone")
    assert len(audits) == 2
    assert audits[1]["before"] == {"value": "America/New_York", "decision_ref": ""}
    assert audits[1]["after"] == {"value": "America/Chicago", "decision_ref": ""}


def test_a_waiting_key_is_refused_by_the_route_and_still_read_when_stored(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    """F04.1: the three keys whose features have not arrived cannot be set on the screen;
    a value already stored on one is still shown with who and when."""
    admin = login_as("rotate_me", tenant=fresh_tenant)
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    for key in policy.POLICY_KEYS:
        if key in WAITING_KEYS:
            assert rows[key]["waiting"] == policy.POLICY_KEYS[key].waiting
            assert rows[key]["waiting"]
        else:
            assert rows[key]["waiting"] is None
    assert rows["small_job_threshold"]["waiting"] == (
        "Not decided (D-08). Set when the WIP schedule arrives."
    )
    assert rows["deposit_identification"]["waiting"] == "Set with the billing reports (F08)."
    assert rows["fuel_surcharge_treatment"]["waiting"] == "Set with the billing reports (F08)."
    values = {
        "small_job_threshold": "25000.00",
        "deposit_identification": "Customer deposit item",
        "fuel_surcharge_treatment": "income",
    }
    for key, value in values.items():
        r = admin.put(
            f"/api/config/policy/{key}", json={"value": value, "decision_ref": "x"}, headers=CSRF
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == policy.POLICY_KEYS[key].waiting
    with tenant_session(rw_engine, fresh_tenant) as s:
        assert s.execute(select(TenantPolicy)).first() is None
        assert (
            s.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'policy_set'")
            ).scalar_one()
            == 0
        )
        # A value already stored (by a later feature, a seed, or set earlier) is kept.
        policy.set_policy(
            s,
            fresh_tenant,
            "deposit_identification",
            "Customer deposit item",
            decision_ref="D-02",
            actor=Actor(user_id=seed.users["rotate_me"].id),
        )
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    dep = rows["deposit_identification"]
    assert dep["decided"] is True and dep["value"] == "Customer deposit item"
    assert dep["decided_by_email"] == seed.users["rotate_me"].email and dep["decided_at"]
    assert dep["decision_ref"] == "D-02" and dep["waiting"]
    with tenant_session(rw_engine, fresh_tenant) as s:
        assert policy.require_policy(s, "deposit_identification") == "Customer deposit item"
    r = admin.put(
        "/api/config/policy/deposit_identification", json={"value": "other"}, headers=CSRF
    )
    assert r.status_code == 409
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    assert rows["deposit_identification"]["value"] == "Customer deposit item"


def test_time_zone_is_offered_as_a_labelled_list_and_any_valid_zone_is_kept(
    seed: Seed, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    """F04.1: the API returns the seven United States zones with plain-words labels and
    the decided value's label; a valid zone off the list is accepted, shown and offered
    so saving never loses it; the other keys carry no options."""
    admin = login_as("rotate_me", tenant=fresh_tenant)
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    tz = rows["timezone"]
    assert tz["value_label"] is None
    assert [o["value"] for o in tz["options"]] == list(US_ZONES)
    assert [o["label"] for o in tz["options"]] == [
        "Eastern (America/New_York)",
        "Central (America/Chicago)",
        "Mountain (America/Denver)",
        "Arizona (America/Phoenix)",
        "Pacific (America/Los_Angeles)",
        "Alaska (America/Anchorage)",
        "Hawaii (Pacific/Honolulu)",
    ]
    for key in policy.POLICY_KEYS:
        if key != "timezone":
            assert rows[key]["options"] is None and rows[key]["value_label"] is None
    r = admin.put("/api/config/policy/timezone", json={"value": "America/New_York"}, headers=CSRF)
    assert r.status_code == 200 and r.json()["value_label"] == "Eastern (America/New_York)"
    assert len(r.json()["options"]) == 7
    r = admin.put("/api/config/policy/timezone", json={"value": "Europe/London"}, headers=CSRF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] == "Europe/London" and body["value_label"] == "Europe/London"
    assert [o["value"] for o in body["options"]] == [*US_ZONES, "Europe/London"]
    assert body["options"][-1]["label"] == "Europe/London"
    r = admin.put("/api/config/policy/timezone", json={"value": "Mars/Olympus"}, headers=CSRF)
    assert r.status_code == 422 and "time zone" in r.json()["detail"]
    assert policy.time_zone_label("America/Phoenix") == "Arizona (America/Phoenix)"


def test_policy_rows_are_tenant_scoped(seed: Seed, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, seed.tenant_a) as s:
        ensure_cost_categories(s, seed.tenant_a)
        policy.set_policy(
            s, seed.tenant_a, "timezone", "America/New_York", decision_ref="t", actor=Actor()
        )
    with tenant_session(rw_engine, seed.tenant_b) as s:
        assert policy.get_policy(s, "timezone") is None
