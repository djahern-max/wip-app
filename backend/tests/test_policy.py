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
WAITING_KEYS = ("small_job_threshold",)  # F08 gave the two item keys their controls
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
    """F04.1: the key whose feature has not arrived cannot be set on the screen; a value
    already stored on one is still shown with who and when. F08 removed the sentence
    from the two item keys; ``small_job_threshold`` keeps its (D-08 open)."""
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
    values = {"small_job_threshold": "25000.00"}
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
            "small_job_threshold",
            "25000.00",
            decision_ref="D-08",
            actor=Actor(user_id=seed.users["rotate_me"].id),
        )
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    small = rows["small_job_threshold"]
    assert small["decided"] is True and small["value"] == "25000.00"
    assert small["decided_by_email"] == seed.users["rotate_me"].email and small["decided_at"]
    assert small["decision_ref"] == "D-08" and small["waiting"]
    with tenant_session(rw_engine, fresh_tenant) as s:
        assert policy.require_policy(s, "small_job_threshold") == Decimal("25000.00")
    r = admin.put("/api/config/policy/small_job_threshold", json={"value": "1.00"}, headers=CSRF)
    assert r.status_code == 409
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    assert rows["small_job_threshold"]["value"] == "25000.00"


# --- F08: the two item keys (D-02, D-39) ---------------------------------------------------------


def _put(admin: TestClient, key: str, value, ref: str = "") -> tuple[int, dict]:
    r = admin.put(
        f"/api/config/policy/{key}", json={"value": value, "decision_ref": ref}, headers=CSRF
    )
    return r.status_code, r.json()


def test_the_two_item_keys_take_the_tenants_items_and_refuse_what_it_does_not_have(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    from tests.billing_helpers import (
        DEPOSIT_ITEM,
        FUEL_ITEM,
        WORK_ITEM,
        apply_payloads,
        item_payload,
        seed_items,
    )

    admin = login_as("rotate_me", tenant=fresh_tenant)
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    for key in ("deposit_identification", "fuel_surcharge_treatment"):
        assert rows[key]["waiting"] is None and rows[key]["options"] == []  # no items held yet
    assert rows["deposit_identification"]["kind"] == "item_ids"
    assert rows["fuel_surcharge_treatment"]["kind"] == "surcharge"
    assert (
        rows["deposit_identification"]["description"]
        == "The QuickBooks items a deposit invoice uses (D-02)."
    )
    assert rows["fuel_surcharge_treatment"]["description"] == (
        "The QuickBooks items a fuel surcharge line uses, and the rate (D-39)."
    )
    # Nothing held: an id is refused in words; an empty deposit list is refused in words.
    status, body = _put(admin, "deposit_identification", {"item_ids": [DEPOSIT_ITEM]})
    assert (
        status == 422
        and "does not have in QuickBooks" in body["detail"]
        and DEPOSIT_ITEM in body["detail"]
    )
    status, body = _put(admin, "deposit_identification", {"item_ids": []})
    assert (
        status == 422
        and body["detail"]
        == "Deposit identification needs at least one QuickBooks item: a deposit invoice is on one."
    )

    seed_items(rw_engine, fresh_tenant)
    apply_payloads(
        rw_engine, fresh_tenant, [("Item", item_payload("904", "Old deposit item", active=False))]
    )
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    options = rows["deposit_identification"]["options"]
    assert [o["value"] for o in options] == [DEPOSIT_ITEM, FUEL_ITEM, "904", WORK_ITEM]  # by name
    assert [o["label"] for o in options] == [
        "Customer deposit",
        "Fuel surcharge (EX)",
        "Old deposit item (inactive)",
        "Site work",
    ]
    assert rows["fuel_surcharge_treatment"]["options"] == options

    with tenant_session(rw_engine, fresh_tenant) as s:
        before = len(_audit_rows(s, "deposit_identification"))
    status, body = _put(
        admin,
        "deposit_identification",
        {"item_ids": [WORK_ITEM, DEPOSIT_ITEM, DEPOSIT_ITEM, "904"]},
        "D-02",
    )
    assert status == 200, body
    assert body["decided"] and body["value"] == {
        "item_ids": [DEPOSIT_ITEM, WORK_ITEM, "904"]
    }  # de-duplicated, sorted as strings
    assert (
        body["decision_ref"] == "D-02" and body["decided_by_email"] == seed.users["rotate_me"].email
    )
    status, body = _put(admin, "deposit_identification", {"item_ids": [DEPOSIT_ITEM]})
    assert status == 200 and body["value"] == {"item_ids": [DEPOSIT_ITEM]}
    with tenant_session(rw_engine, fresh_tenant) as s:
        rows_ = _audit_rows(s, "deposit_identification")
        assert len(rows_) == before + 2
        assert rows_[-2].get("before") is None and rows_[-2]["after"]["value"] == {
            "item_ids": [DEPOSIT_ITEM, WORK_ITEM, "904"]
        }
        assert rows_[-1]["before"]["value"] == {"item_ids": [DEPOSIT_ITEM, WORK_ITEM, "904"]}
        assert rows_[-1]["after"] == {"value": {"item_ids": [DEPOSIT_ITEM]}, "decision_ref": ""}
        assert policy.deposit_items(s) == frozenset({DEPOSIT_ITEM})
    for bad in ("12", ["12"], {"item_ids": "12"}, {"item_ids": [" "]}, {"item_ids": ["999"]}):
        status, body = _put(admin, "deposit_identification", bad)
        assert status == 422, bad
        assert body["detail"].startswith("Deposit identification needs ")


def test_fuel_surcharge_takes_items_and_a_rate_or_records_no_surcharge(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    from tests.billing_helpers import FUEL_ITEM, seed_items

    admin = login_as("rotate_me", tenant=fresh_tenant)
    seed_items(rw_engine, fresh_tenant)
    key = "fuel_surcharge_treatment"
    # Rye Beach: one item per division income account and 5.00%.
    status, body = _put(admin, key, {"item_ids": [FUEL_ITEM], "rate": "0.05"}, "D-39")
    assert status == 200 and body["value"] == {"item_ids": [FUEL_ITEM], "rate": "0.0500"}
    with tenant_session(rw_engine, fresh_tenant) as s:
        got = policy.surcharge_treatment(s)
        assert (
            got is not None
            and got.item_ids == frozenset({FUEL_ITEM})
            and got.rate == Decimal("0.0500")
        )
    # The rate may wait (F08 does not read it; F08.1 requires it).
    status, body = _put(admin, key, {"item_ids": [FUEL_ITEM], "rate": None})
    assert status == 200 and body["value"] == {"item_ids": [FUEL_ITEM], "rate": None}
    status, body = _put(admin, key, {"item_ids": [FUEL_ITEM]})
    assert status == 200 and body["value"] == {"item_ids": [FUEL_ITEM], "rate": None}
    # "This company charges no fuel surcharge" is a decision, read back as decided.
    status, body = _put(admin, key, {"item_ids": [], "rate": None}, "owner, 2026-10-06")
    assert (
        status == 200
        and body["decided"] is True
        and body["value"] == {"item_ids": [], "rate": None}
    )
    with tenant_session(rw_engine, fresh_tenant) as s:
        got = policy.surcharge_treatment(s)
        assert got is not None and got.item_ids == frozenset() and got.rate is None
        assert len(_audit_rows(s, key)) == 4
    # Refused in words: a rate with no item, a rate out of range or as a number, an unknown item.
    for bad, needs in (
        ({"item_ids": [], "rate": "0.05"}, "the fuel surcharge items with the rate"),
        ({"item_ids": [FUEL_ITEM], "rate": 0.05}, "a rate as a fraction in a string"),
        ({"item_ids": [FUEL_ITEM], "rate": "5"}, "a rate above 0 and below 1"),
        ({"item_ids": [FUEL_ITEM], "rate": "abc"}, "a rate as a fraction in a string"),
        ({"item_ids": ["999"], "rate": "0.05"}, "item id(s) this company does not have"),
        ("income", "an object"),
    ):
        status, body = _put(admin, key, bad)
        assert status == 422 and needs in body["detail"], (bad, body)
    rows = {p["key"]: p for p in admin.get("/api/config/policy").json()}
    assert rows[key]["value"] == {"item_ids": [], "rate": None}  # the refusals changed nothing


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
        if key != "timezone" and policy.POLICY_KEYS[key].kind not in policy.ITEM_KINDS:
            assert rows[key]["options"] is None and rows[key]["value_label"] is None
        elif key != "timezone":  # F08: the item keys carry the item picker
            assert rows[key]["options"] == [] and rows[key]["value_label"] is None
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
