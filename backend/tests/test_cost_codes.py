"""Cost categories and cost codes (D-23): the seeded list, per-tenant seeding by the
audited service, the cost-code function, and the grid's "no account" cells."""

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select, text

from app.core.db import tenant_session
from app.domain.config.audit import Actor
from app.domain.config.categories import D23_COST_CATEGORIES, cost_code, ensure_cost_categories
from app.domain.config.models import CostCategory, Division
from app.domain.config.service import create_division
from tests.config_helpers import CHART_CSV, load_rye_beach_rules, run_until_quiet, upload_chart
from tests.conftest import Seed

D23 = [
    ("10", "Labor"),
    ("20", "Labor Burden"),
    ("30", "Materials"),
    ("35", "Supplies"),
    ("40", "Subcontractors"),
    ("45", "Equipment (owned)"),
    ("47", "Vehicles (owned)"),
    ("50", "Equipment Rental"),
    ("55", "Equipment Maintenance"),
    ("60", "Disposal"),
    ("65", "Fuel"),
    ("70", "Permits & Bonds"),
    ("80", "Warranty"),
    ("90", "Other"),
]


def test_seed_is_exactly_the_d23_list_per_tenant_and_audited_once(
    seed: Seed, rw_engine: Engine, fresh_tenant
) -> None:
    assert list(D23_COST_CATEGORIES) == D23
    statements: list[str] = []

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO cost_category" in statement:
            statements.append(statement)

    event.listen(rw_engine, "before_cursor_execute", on_execute)
    try:
        with tenant_session(rw_engine, fresh_tenant) as s:
            assert ensure_cost_categories(s, fresh_tenant, Actor()) == 14
            assert ensure_cost_categories(s, fresh_tenant, Actor()) == 0
    finally:
        event.remove(rw_engine, "before_cursor_execute", on_execute)
    assert statements and all("tenant_id" in st for st in statements)
    with tenant_session(rw_engine, fresh_tenant) as s:
        rows = s.execute(select(CostCategory).order_by(CostCategory.sort_order)).scalars().all()
        assert [(r.slot, r.name) for r in rows] == D23
        assert all(r.tenant_id == fresh_tenant and r.active for r in rows)
        n = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'cost_categories_seeded'")
        ).scalar_one()
        assert n == 1
    with tenant_session(rw_engine, seed.tenant_a) as s:  # another tenant is untouched
        c_rows = s.execute(select(CostCategory).where(CostCategory.tenant_id == fresh_tenant)).all()
        assert c_rows == []


def test_cost_code_function(seed: Seed, rw_engine: Engine, fresh_tenant) -> None:
    with tenant_session(rw_engine, fresh_tenant) as s:
        ensure_cost_categories(s, fresh_tenant)
        labor = s.execute(select(CostCategory).where(CostCategory.slot == "10")).scalar_one()
        snow = create_division(
            s,
            fresh_tenant,
            code="SNOWX",
            name="Snow",
            code_digit="4",
            sort_order=0,
            actor=Actor(),
        )
        design = create_division(
            s,
            fresh_tenant,
            code="DESX",
            name="Design",
            code_digit=None,
            sort_order=1,
            actor=Actor(),
        )
        assert cost_code(snow, labor) == "410"
        assert cost_code(design, labor) is None
        assert cost_code(None, labor) is None and cost_code(snow, None) is None


def test_cost_codes_grid_shows_no_account_cells(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    load_rye_beach_rules(rw_engine, fresh_tenant)
    client = login_as("rotate_me", tenant=fresh_tenant)
    upload_chart(client, CHART_CSV.read_bytes())
    run_until_quiet(rw_engine)
    grid = client.get("/api/config/cost-codes").json()
    assert [c["slot"] for c in grid["categories"]] == [s for s, _ in D23]
    rows = {r["code"]: r for r in grid["rows"]}
    snow = {c["slot"]: c for c in rows["SNOW"]["cells"]}
    assert snow["10"]["code"] == "410"
    assert [a["account_no"] for a in snow["10"]["accounts"]] == ["5410"]
    assert snow["40"]["accounts"] == []  # no account: SNOW + Subcontractors
    for r in rows.values():
        for cell in r["cells"]:
            if cell["slot"] in ("45", "47", "65"):
                assert cell["accounts"] == [], (r["code"], cell["slot"])
    assert rows["MS"]["code_digit"] is None and all(c["code"] is None for c in rows["MS"]["cells"])
    with tenant_session(rw_engine, fresh_tenant) as s:
        assert {d.code for d in s.execute(select(Division)).scalars()} >= {
            "LS",
            "EX",
            "GC",
            "SNOW",
            "MS",
            "DES",
        }


def test_confirm_all_writes_one_audit_row_per_account(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    load_rye_beach_rules(rw_engine, fresh_tenant)
    client = login_as("recover_me", tenant=fresh_tenant)
    upload_chart(client, CHART_CSV.read_bytes())
    run_until_quiet(rw_engine)
    with tenant_session(rw_engine, fresh_tenant) as s:
        before = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'account_map_confirmed'")
        ).scalar_one()
    from tests.conftest import CSRF

    r = client.post("/api/config/accounts/confirm-all", headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json() == {"confirmed": 159, "unmapped_count": 0}
    with tenant_session(rw_engine, fresh_tenant) as s:
        after = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'account_map_confirmed'")
        ).scalar_one()
        summary = s.execute(
            text(
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'account_map_confirmed' AND entity_id IS NULL"
            )
        ).scalar_one()
    assert after - before == 159 and summary == 0
    body = client.get("/api/config/accounts").json()
    assert (body["unmapped_count"], body["confirmed_count"]) == (0, 159)
    assert client.post("/api/config/accounts/confirm-all", headers=CSRF).json()["confirmed"] == 0
