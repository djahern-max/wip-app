"""Burden rates (F04): no overlapping periods for the same division, the rate in
force on a date at the boundaries, Decimal end to end."""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.db import tenant_session
from app.domain.config import burden
from app.domain.config.audit import Actor
from app.domain.config.service import create_division
from tests.conftest import CSRF, Seed


def test_overlaps_refused_and_rate_on_date_at_boundaries(
    seed: Seed, rw_engine: Engine, fresh_tenant
) -> None:
    actor = Actor(user_id=seed.users["firm_admin"].id)
    with tenant_session(rw_engine, fresh_tenant) as s:
        snow = create_division(
            s, fresh_tenant, code="SNOW", name="Snow", code_digit="4", sort_order=0, actor=actor
        )
        burden.add_burden_rate(
            s,
            fresh_tenant,
            division_id=None,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 7, 1),
            rate=Decimal("0.3250"),
            basis_note="2025 actuals",
            actor=actor,
        )
        burden.add_burden_rate(
            s,
            fresh_tenant,
            division_id=None,
            effective_from=date(2026, 7, 1),
            effective_to=None,
            rate=Decimal("0.3400"),
            basis_note=None,
            actor=actor,
        )
        burden.add_burden_rate(
            s,
            fresh_tenant,
            division_id=snow.id,
            effective_from=date(2026, 11, 1),
            effective_to=date(2027, 4, 1),
            rate=Decimal("0.4100"),
            basis_note="winter",
            actor=actor,
        )
        snow_id = snow.id
        with pytest.raises(
            burden.BurdenRateError, match="overlaps the rate in force from 2026-01-01"
        ):
            burden.add_burden_rate(
                s,
                fresh_tenant,
                division_id=None,
                effective_from=date(2026, 6, 30),
                effective_to=date(2026, 8, 1),
                rate=Decimal("0.5"),
                basis_note=None,
                actor=actor,
            )
        with pytest.raises(burden.BurdenRateError, match="overlaps"):
            burden.add_burden_rate(  # open-ended existing period covers 2030
                s,
                fresh_tenant,
                division_id=None,
                effective_from=date(2030, 1, 1),
                effective_to=None,
                rate=Decimal("0.5"),
                basis_note=None,
                actor=actor,
            )
        # A different division may overlap in time.
        burden.add_burden_rate(
            s,
            fresh_tenant,
            division_id=snow_id,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 11, 1),
            rate=Decimal("0.3000"),
            basis_note=None,
            actor=actor,
        )
        with pytest.raises(burden.BurdenRateError, match="end date must be after"):
            burden.add_burden_rate(
                s,
                fresh_tenant,
                division_id=None,
                effective_from=date(2031, 1, 1),
                effective_to=date(2031, 1, 1),
                rate=Decimal("0.1"),
                basis_note=None,
                actor=actor,
            )
    with tenant_session(rw_engine, fresh_tenant) as s:
        on = burden.burden_rate_on
        assert on(s, date(2025, 12, 31)) is None
        assert on(s, date(2026, 1, 1)) == Decimal("0.3250")  # inclusive start
        assert on(s, date(2026, 6, 30)) == Decimal("0.3250")
        assert on(s, date(2026, 7, 1)) == Decimal("0.3400")  # exclusive end / next start
        assert on(s, date(2030, 1, 1)) == Decimal("0.3400")  # open-ended
        assert on(s, date(2026, 10, 31), snow_id) == Decimal("0.3000")
        assert on(s, date(2026, 11, 1), snow_id) == Decimal("0.4100")
        assert on(s, date(2027, 4, 1), snow_id) == Decimal("0.3400")  # falls back to tenant-wide
        for value in (on(s, date(2026, 1, 1)), on(s, date(2026, 11, 1), snow_id)):
            assert type(value) is Decimal
        stored = s.execute(
            text("SELECT rate FROM burden_rate ORDER BY effective_from LIMIT 1")
        ).scalar_one()
        assert type(stored) is Decimal and stored == Decimal("0.3250")


def test_burden_rate_api_is_strings_in_and_out(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    c = login_as("recover_me", tenant=fresh_tenant)
    r = c.post(
        "/api/config/burden-rates",
        json={"effective_from": "2026-01-01", "rate": "0.32505", "basis_note": "note"},
        headers=CSRF,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rate"] == "0.3251" and body["division_code"] is None  # quantized half-up
    r = c.post(
        "/api/config/burden-rates",
        json={"effective_from": "2026-03-01", "rate": "0.4"},
        headers=CSRF,
    )
    assert r.status_code == 422 and "overlaps" in r.json()["detail"]
    r = c.post(
        "/api/config/burden-rates",
        json={"effective_from": "2026-03-01", "rate": "abc"},
        headers=CSRF,
    )
    assert r.status_code == 422 and "decimal fraction" in r.json()["detail"]
    r = c.post(f"/api/config/burden-rates/{body['id']}/deactivate", headers=CSRF)
    assert r.status_code == 200 and r.json()["active"] is False
    with tenant_session(rw_engine, fresh_tenant) as s:
        actions = (
            s.execute(
                text(
                    "SELECT action FROM audit_log WHERE entity_type = 'burden_rate' "
                    "AND entity_id = :id ORDER BY occurred_at"
                ),
                {"id": body["id"]},
            )
            .scalars()
            .all()
        )
    assert actions == ["burden_rate_added", "burden_rate_deactivated"]
    listed = c.get("/api/config/burden-rates").json()
    assert any(x["id"] == body["id"] and x["active"] is False for x in listed)
    # A JSON number would be a float on the way in: refused by the schema (rate is a string).
    r = c.post(
        "/api/config/burden-rates", json={"effective_from": "2027-01-01", "rate": 0.3}, headers=CSRF
    )
    assert r.status_code == 422
