"""F06.1: labor burden on estimates (D-05, D-34). Burden is computed when an estimate
is read, per kept, priced work area and division, at the rate in force on the
estimate date (or the version's received date in the tenant's time zone), and is
never written. The pure module first, then the API over the Rye Beach fixtures and
test copies, with the rates as Rye Beach has them on production."""

import re
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text

from app.audit.models import AuditLog
from app.core.db import tenant_session
from app.domain.config.audit import Actor
from app.domain.config.burden import (
    active_burden_rates,
    add_burden_rate,
    burden_rate_on,
    deactivate_burden_rate,
    pick_rate,
)
from app.domain.config.models import CostCategory, Division, GlAccount
from app.domain.config.policy import set_policy
from app.domain.estimates.burden import compute_burden
from app.domain.estimates.models import EstimateCost
from app.domain.estimates.totals import CostLineIn, WorkAreaIn, compute, with_burden
from app.tenancy.models import Membership, Role
from tests.config_helpers import run_until_quiet
from tests.conftest import CSRF, Seed
from tests.estimate_helpers import (
    D04_BASIS,
    ELM,
    TURLEY,
    build_workbook,
    configure_tenant,
    fixture_rows,
    upload_template,
)

D = Decimal
EID = "EST6115758"
TID = "EST6120638"
D05_BASIS = [*D04_BASIS, "20"]  # D-05: Labor Burden joins Rye Beach's basis
RATES = {"LS": "0.2136", "EX": "0.1959", "GC": "0.2207", "SNOW": "0.2061"}  # D-34
JAN1 = date(2026, 1, 1)
BURDEN_MODULE = Path(__file__).resolve().parents[1] / "app/domain/estimates/burden.py"


def q(amount: Decimal) -> Decimal:
    return amount.quantize(D("0.01"), rounding=ROUND_HALF_UP)


# --- the pure module ------------------------------------------------------------------------------
class Row:
    """A burden_rate row as the pure functions see it."""

    def __init__(self, division_id, start, end, rate, note=None):
        self.division_id, self.effective_from, self.effective_to = division_id, start, end
        self.rate, self.basis_note = D(rate), note


EX_ID, LS_ID = uuid.uuid4(), uuid.uuid4()


def line(code: str, amount: str, division=EX_ID, division_code="EX") -> CostLineIn:
    slot = code[1:]
    return CostLineIn(code, slot, None, D(amount), division, division_code)


def area(order: int, *lines: CostLineIn, kept=True, price="100.00") -> WorkAreaIn:
    return WorkAreaIn(order, f"Area {order}", kept, D(price), False, tuple(lines))


def test_pick_rate_prefers_the_division_row_and_reads_periods_half_open() -> None:
    ex_old = Row(EX_ID, JAN1, date(2026, 7, 1), "0.1959")
    ex_new = Row(EX_ID, date(2026, 7, 1), None, "0.2100")
    company = Row(None, JAN1, None, "0.3000")
    rows = [company, ex_old, ex_new]
    assert pick_rate(rows, date(2026, 6, 30), EX_ID) is ex_old
    assert pick_rate(rows, date(2026, 7, 1), EX_ID) is ex_new
    assert pick_rate(rows, date(2026, 6, 30), LS_ID) is company  # no LS row covers the day
    assert pick_rate([ex_old], date(2026, 7, 1), EX_ID) is None  # [from, to): the end is out
    assert pick_rate(rows, date(2025, 12, 31), EX_ID) is None
    assert pick_rate(rows, date(2026, 3, 1), None) is company


def test_burden_is_quantized_per_work_area_and_division_then_summed() -> None:
    # 0.005 of rounding in each work area: per-work-area 0.02 + 0.02, whole-estimate 0.03.
    was = [area(1, line("210", "0.10")), area(2, line("210", "0.10"))]
    r = compute_burden(was, [Row(EX_ID, JAN1, None, "0.1500")], date(2026, 3, 1))
    assert [w.burden for w in r.work_areas] == [D("0.02"), D("0.02")]
    assert r.total == D("0.04") and q(D("0.20") * D("0.15")) == D("0.03")
    (ex,) = r.by_division
    assert (ex.division_code, ex.labor, ex.burden, ex.rate.rate) == (
        "EX",
        D("0.20"),
        D("0.04"),
        D("0.1500"),
    )


def test_two_divisions_in_one_work_area_are_quantized_separately() -> None:
    was = [area(1, line("210", "0.10"), line("110", "0.10", LS_ID, "LS"))]
    rows = [Row(EX_ID, JAN1, None, "0.1500"), Row(LS_ID, JAN1, None, "0.1500")]
    r = compute_burden(was, rows, date(2026, 3, 1))
    assert r.work_areas[0].burden == D("0.04")
    assert [d.division_code for d in r.by_division] == ["EX", "LS"]
    assert sum(d.burden for d in r.by_division) == r.total == D("0.04")


def test_only_labor_under_counting_work_areas_carries_burden() -> None:
    was = [
        area(1, line("210", "100.00"), line("230", "50.00")),  # materials: never burden
        area(2, line("210", "100.00"), kept=False),  # omitted
        area(3, line("210", "100.00"), price="0.00"),  # kept, priced 0.00
        area(4, CostLineIn("999", None, None, D("100.00"))),  # not on the grid
    ]
    r = compute_burden(was, [Row(EX_ID, JAN1, None, "0.2000")], date(2026, 3, 1))
    assert r.total == D("20.00") and [w.order_no for w in r.work_areas] == [1]
    assert r.burden_for(3) == D("0.00") and r.burden_for(2) == D("0.00")


def test_no_labor_needs_no_rate_and_no_date() -> None:
    r = compute_burden([area(1, line("290", "10.00"))], [], None)
    assert (r.total, r.missing, r.by_division) == (D("0.00"), (), ())


def test_a_missing_rate_or_date_leaves_burden_not_computed() -> None:
    was = [area(1, line("210", "100.00")), area(2, line("110", "100.00", LS_ID, "LS"))]
    r = compute_burden(was, [Row(EX_ID, JAN1, None, "0.2000")], date(2026, 3, 1))
    assert r.missing == ("LS",) and r.total is None
    assert r.burden_for(1) == D("20.00") and r.burden_for(2) is None
    undated = compute_burden(was, [Row(EX_ID, JAN1, None, "0.2000")], None)
    assert undated.missing == ("EX", "LS") and undated.total is None


def test_slot_20_lines_are_reported_on_counting_work_areas_only() -> None:
    was = [
        area(1, line("210", "100.00"), line("220", "1000.00")),
        area(2, line("220", "5.00"), kept=False),
        area(3, line("220", "5.00"), price="0.00"),
    ]
    r = compute_burden(was, [Row(EX_ID, JAN1, None, "0.2000")], date(2026, 3, 1))
    assert r.burden_line_orders == (1,) and r.total == D("20.00")


def test_with_burden_replaces_slot_20_and_recomputes_eac_on_the_same_slots() -> None:
    cats = [("10", "Labor"), ("20", "Labor Burden"), ("30", "Materials")]
    was = [area(1, line("210", "100.00"), line("220", "7.00"), line("230", "50.00"))]
    as_est = compute(was, cats, frozenset({"10", "20", "30"}))
    assert (as_est.kept_cost, as_est.eac_in_basis) == (D("157.00"), D("157.00"))
    wb = with_burden(as_est, frozenset({"10", "20", "30"}), D("20.00"))
    assert (wb.labor_burden, wb.kept_cost, wb.eac_in_basis, wb.eac_not_computed) == (
        D("20.00"),
        D("170.00"),
        D("170.00"),
        False,
    )
    missing = with_burden(as_est, frozenset({"10", "20", "30"}), None)
    assert (missing.labor_burden, missing.kept_cost, missing.eac_in_basis) == (None, None, None)
    assert missing.eac_not_computed
    out_of_basis = with_burden(
        compute(was, cats, frozenset({"10", "30"})), frozenset({"10", "30"}), None
    )
    assert (out_of_basis.eac_in_basis, out_of_basis.eac_not_computed) == (D("150.00"), False)
    undecided = with_burden(compute(was, cats, None), None, D("20.00"))
    assert (undecided.eac_in_basis, undecided.eac_not_computed, undecided.kept_cost) == (
        None,
        False,
        D("170.00"),
    )


def test_the_burden_module_has_no_float_and_rounds_half_up_only() -> None:
    src = BURDEN_MODULE.read_text()
    assert not re.search(r"\bfloat\b", src)
    assert set(re.findall(r"ROUND_[A-Z_]+", src)) == {"ROUND_HALF_UP"}
    assert not re.search(r"\b(select|Session|datetime\.now|date\.today)\b", src)


# --- through the API ------------------------------------------------------------------------------
def _actor(seed: Seed) -> Actor:
    return Actor(user_id=seed.users["rotate_me"].id)


def _policy(engine: Engine, seed: Seed, tenant_id: uuid.UUID, key: str, value) -> None:
    with tenant_session(engine, tenant_id) as s:
        set_policy(s, tenant_id, key, value, decision_ref="D-05 (test)", actor=_actor(seed))


def _divisions(engine: Engine, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    with tenant_session(engine, tenant_id) as s:
        return {d.code: d.id for d in s.execute(select(Division)).scalars()}


def _add_rate(engine, seed, tenant_id, code, rate, start=JAN1, end=None) -> uuid.UUID:
    with tenant_session(engine, tenant_id) as s:
        division_id = None if code is None else _divisions(engine, tenant_id)[code]
        row = add_burden_rate(
            s,
            tenant_id,
            division_id=division_id,
            effective_from=start,
            effective_to=end,
            rate=D(rate),
            basis_note="worksheet 2026-09-27 (test)",
            actor=_actor(seed),
        )
        return row.id


def _seed_production_rates(engine: Engine, seed: Seed, tenant_id: uuid.UUID) -> None:
    """D-34: the 25-26 audited EX row deactivated, the four worksheet rows active."""
    old = _add_rate(engine, seed, tenant_id, "EX", "0.1945")
    with tenant_session(engine, tenant_id) as s:
        deactivate_burden_rate(s, tenant_id, old, _actor(seed))
    for code, rate in RATES.items():
        _add_rate(engine, seed, tenant_id, code, rate)


@pytest.fixture
def bare(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> dict:
    """Rye Beach's grid, the D-05 basis and the time zone; no burden rates."""
    configure_tenant(rw_engine, seed, fresh_tenant, basis=False)
    _policy(rw_engine, seed, fresh_tenant, "wip_basis", D05_BASIS)
    _policy(rw_engine, seed, fresh_tenant, "timezone", "America/New_York")
    return {"client": login_as("rotate_me", tenant=fresh_tenant), "id": fresh_tenant}


@pytest.fixture
def tenant(bare: dict, seed: Seed, rw_engine: Engine) -> dict:
    _seed_production_rates(rw_engine, seed, bare["id"])
    return bare


def _load(t: dict, engine: Engine, content: bytes, filename: str = "estimates.xlsx") -> None:
    upload_template(t["client"], content, filename)
    run_until_quiet(engine)


def _detail(t: dict, external_id: str) -> dict:
    rows = t["client"].get("/api/estimates").json()["estimates"]
    row = next(r for r in rows if r["external_id"] == external_id)
    r = t["client"].get(f"/api/estimates/{row['id']}")
    assert r.status_code == 200, r.text
    return r.json()


def _codes(d: dict) -> list[str]:
    return [i["code"] for i in d["attention"]]


def _issue(d: dict, code: str) -> dict:
    return next(i for i in d["attention"] if i["code"] == code)


def _category(d: dict, slot: str) -> dict:
    return next(c for c in d["totals"]["by_category"] if c["slot"] == slot)


def _elm_copy(*, estimate_date=None, recode=None, extra_lines=()) -> bytes:
    rows = fixture_rows(ELM)
    if estimate_date is not None:
        rows["Estimates"][0]["estimate_date"] = estimate_date
    labor = [c for c in rows["Estimate costs"] if str(c["cost_code"]).strip() == "210"]
    for n, c in enumerate(labor):
        if recode is not None:
            c["cost_code"] = recode(n)
    rows["Estimate costs"].extend(
        {"estimate_id": EID, "order": order, "cost_code": code, "hours": None, "amount": amount}
        for order, code, amount in extra_lines
    )
    return build_workbook(sheets=rows)


def _expected_burden(d: dict, rates: dict[str, str]) -> tuple[Decimal, dict[int, Decimal]]:
    """Burden recomputed from the detail's own lines: per counting work area and
    division, quantized once; the estimate's burden is the sum."""
    per_area: dict[int, Decimal] = {}
    for w in d["work_areas"]:
        if not (w["kept"] and D(w["price"]) > 0):
            continue
        labor: dict[str, Decimal] = {}
        for ln in w["lines"]:
            if ln["category_name"] == "Labor":
                labor[ln["division_code"]] = labor.get(ln["division_code"], D("0")) + D(
                    ln["amount"]
                )
        if labor:
            per_area[w["order_no"]] = sum((q(a * D(rates[c])) for c, a in labor.items()), D("0.00"))
    return sum(per_area.values(), D("0.00")), per_area


def test_67_elm_carries_ex_burden_6383_50_on_the_received_date(
    tenant: dict, rw_engine: Engine
) -> None:
    _load(tenant, rw_engine, ELM.read_bytes(), "estimate_upload_EST6115758.xlsx")
    d = _detail(tenant, EID)
    t = d["totals"]
    assert d["estimate_date"] is None
    assert (t["burden_date_source"], t["burden_computed"]) == ("received", True)
    assert t["burden_date"] == datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    assert t["burden_total"] == "6383.50" and q(D("32585.55") * D("0.1959")) == D("6383.51")
    assert (t["cost_total_as_estimated"], t["kept_cost"]) == ("315832.69", "315832.69")
    assert t["cost_total_with_burden"] == "322216.19"
    assert (t["eac_in_basis"], t["eac_in_basis_as_estimated"], t["eac_not_computed"]) == (
        "293017.70",
        "286634.20",
        False,
    )
    assert t["burden_by_division"] == [
        {
            "division_code": "EX",
            "labor_amount": "32585.55",
            "rate": "0.1959",
            "rate_percent": "19.59",
            "rate_effective_from": "2026-01-01",
            "basis_note": "worksheet 2026-09-27 (test)",
            "burden": "6383.50",
        }
    ]
    burden_row = _category(d, "20")
    assert (burden_row["amount"], burden_row["amount_with_burden"], burden_row["in_basis"]) == (
        "0.00",
        "6383.50",
        "yes",
    )
    assert _category(d, "10")["amount"] == _category(d, "10")["amount_with_burden"] == "32585.55"
    total, per_area = _expected_burden(d, RATES)
    assert total == D("6383.50") and len(per_area) == 13
    for w in d["work_areas"]:
        assert w["burden"] == str(per_area.get(w["order_no"], D("0.00"))), w["order_no"]
        assert w["cost"] == str(sum((D(ln["amount"]) for ln in w["lines"]), D("0.00")))
    assert _codes(d) == ["EST_UNIT_PRICED"]


def test_turley_has_no_labor_and_so_no_burden(tenant: dict, rw_engine: Engine) -> None:
    _load(tenant, rw_engine, TURLEY.read_bytes(), "turley.xlsx")
    t = _detail(tenant, TID)["totals"]
    assert (t["burden_total"], t["burden_by_division"], t["burden_computed"]) == ("0.00", [], True)
    assert t["eac_in_basis"] == t["eac_in_basis_as_estimated"] is not None
    assert t["cost_total_with_burden"] == t["cost_total_as_estimated"]
    assert "EST_NO_BURDEN_RATE" not in _codes(_detail(tenant, TID))


def test_a_date_before_any_rate_leaves_eac_not_computed(tenant: dict, rw_engine: Engine) -> None:
    _load(tenant, rw_engine, _elm_copy(estimate_date=date(2025, 12, 31)))
    d = _detail(tenant, EID)
    t = d["totals"]
    assert (t["burden_date"], t["burden_date_source"], t["burden_computed"]) == (
        "2025-12-31",
        "estimate_date",
        False,
    )
    assert (
        t["burden_total"],
        t["cost_total_with_burden"],
        _category(d, "20")["amount_with_burden"],
    ) == (
        None,
        None,
        None,
    )
    assert (t["eac_in_basis"], t["eac_not_computed"], t["eac_in_basis_as_estimated"]) == (
        None,
        True,
        "286634.20",
    )
    assert (
        t["burden_by_division"][0]["rate"] is None and t["burden_by_division"][0]["burden"] is None
    )
    issue = _issue(d, "EST_NO_BURDEN_RATE")
    assert "EX" in issue["message"] and "2025-12-31" in issue["message"]
    assert all(w["burden"] is None for w in d["work_areas"] if w["order_no"] in (1, 2))


def test_no_time_zone_and_no_estimate_date_means_no_pricing_day(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    configure_tenant(rw_engine, seed, fresh_tenant, basis=False)
    _policy(rw_engine, seed, fresh_tenant, "wip_basis", D05_BASIS)
    t = {"client": login_as("rotate_me", tenant=fresh_tenant), "id": fresh_tenant}
    _seed_production_rates(rw_engine, seed, fresh_tenant)
    _load(t, rw_engine, ELM.read_bytes())
    d = _detail(t, EID)
    assert (d["totals"]["burden_date"], d["totals"]["burden_date_source"]) == (None, "none")
    assert (d["totals"]["burden_computed"], d["totals"]["eac_in_basis"]) == (False, None)
    assert "time zone" in _issue(d, "EST_NO_BURDEN_DATE")["message"]
    assert "EST_NO_BURDEN_RATE" not in _codes(d)


def test_all_labor_on_ls_and_half_and_half(tenant: dict, rw_engine: Engine) -> None:
    _load(tenant, rw_engine, _elm_copy(recode=lambda n: "110"))
    t = _detail(tenant, EID)["totals"]
    assert (t["burden_total"], t["eac_in_basis"]) == (
        "6960.29",
        "293594.49",
    )  # not 6,960.27 (owner, Q4)
    assert q(D("32585.55") * D("0.2136")) == D("6960.27")
    assert [r["division_code"] for r in t["burden_by_division"]] == ["LS"]
    _load(tenant, rw_engine, _elm_copy(recode=lambda n: "110" if n % 2 == 0 else "210"))
    d = _detail(tenant, EID)
    rows = d["totals"]["burden_by_division"]
    assert [r["division_code"] for r in rows] == ["EX", "LS"]
    assert sum(D(r["burden"]) for r in rows) == D(d["totals"]["burden_total"])
    assert sum(D(r["labor_amount"]) for r in rows) == D("32585.55")
    total, per_area = _expected_burden(d, RATES)
    assert D(d["totals"]["burden_total"]) == total
    for w in d["work_areas"]:
        assert w["burden"] == str(per_area.get(w["order_no"], D("0.00")))


def test_a_slot_20_line_loads_is_shown_as_estimated_and_is_left_out_of_burden(
    tenant: dict, rw_engine: Engine
) -> None:
    _load(tenant, rw_engine, _elm_copy(extra_lines=[(1, "220", "1000.00"), (17, "220", "40.00")]))
    d = _detail(tenant, EID)
    t = d["totals"]
    row = _category(d, "20")
    assert (row["amount"], row["amount_with_burden"]) == ("1000.00", "6383.50")
    assert (t["eac_in_basis_as_estimated"], t["eac_in_basis"]) == ("287634.20", "293017.70")
    assert (t["cost_total_as_estimated"], t["cost_total_with_burden"]) == ("316832.69", "322216.19")
    burden_lines = [i for i in d["attention"] if i["code"] == "EST_BURDEN_LINE"]
    assert len(burden_lines) == 1 and "#1" in burden_lines[0]["message"]
    assert "#17" not in burden_lines[0]["message"]
    omitted = [i["message"] for i in d["attention"] if i["code"] == "EST_COST_LINE_ON_OMITTED"]
    assert len(omitted) == 1 and omitted[0].startswith("Work area #17 is omitted")
    with tenant_session(rw_engine, tenant["id"]) as s:
        codes = s.execute(select(EstimateCost.cost_code, EstimateCost.amount)).all()
    assert ("220", D("1000.00")) in codes and ("220", D("40.00")) in codes  # stored as loaded


def test_a_kept_work_area_priced_0_00_with_labor_carries_no_burden(
    tenant: dict, rw_engine: Engine
) -> None:
    """Owner's addition (2026-09-29): work area #20 is kept at 0.00."""
    _load(tenant, rw_engine, _elm_copy(extra_lines=[(20, "210", "500.00")]))
    d = _detail(tenant, EID)
    w20 = next(w for w in d["work_areas"] if w["order_no"] == 20)
    assert (w20["kept"], w20["price"], w20["cost"], w20["burden"]) == (
        True,
        "0.00",
        "500.00",
        "0.00",
    )
    assert d["totals"]["burden_total"] == "6383.50"
    assert d["totals"]["burden_by_division"][0]["labor_amount"] == "32585.55"
    assert any(
        i["code"] == "EST_COST_LINE_ON_OMITTED"
        and i["message"].startswith("Work area #20 is priced 0.00")
        for i in d["attention"]
    )


def test_the_rate_in_force_follows_the_estimate_date(
    bare: dict, seed: Seed, rw_engine: Engine
) -> None:
    t = bare
    _add_rate(rw_engine, seed, t["id"], "EX", "0.1959", JAN1, date(2026, 7, 1))
    _add_rate(rw_engine, seed, t["id"], "EX", "0.2100", date(2026, 7, 1))
    _load(t, rw_engine, _elm_copy(estimate_date=date(2026, 6, 30)))
    assert _detail(t, EID)["totals"]["burden_total"] == "6383.50"
    _load(t, rw_engine, _elm_copy(estimate_date=date(2026, 7, 1)))
    rows = _detail(t, EID)["totals"]
    assert rows["burden_total"] == "6842.96"  # not 6,842.97 (owner, Q4)
    assert rows["burden_by_division"][0]["rate_effective_from"] == "2026-07-01"


def test_the_company_rate_applies_only_where_no_division_rate_covers(
    bare: dict, seed: Seed, rw_engine: Engine
) -> None:
    t = bare
    _add_rate(rw_engine, seed, t["id"], None, "0.3000")
    _load(t, rw_engine, ELM.read_bytes())
    d = _detail(t, EID)
    total, _ = _expected_burden(d, {"EX": "0.3000"})
    assert D(d["totals"]["burden_total"]) == total
    assert d["totals"]["burden_by_division"][0]["rate"] == "0.3000"
    _add_rate(rw_engine, seed, t["id"], "EX", "0.1959")
    assert _detail(t, EID)["totals"]["burden_total"] == "6383.50"


def test_inactive_rates_are_never_used(bare: dict, seed: Seed, rw_engine: Engine) -> None:
    t = bare
    old = _add_rate(rw_engine, seed, t["id"], "EX", "0.1945")
    with tenant_session(rw_engine, t["id"]) as s:
        deactivate_burden_rate(s, t["id"], old, _actor(seed))
        assert active_burden_rates(s) == []
        assert burden_rate_on(s, date(2026, 3, 1), _divisions(rw_engine, t["id"])["EX"]) is None
    _load(t, rw_engine, ELM.read_bytes())
    d = _detail(t, EID)
    assert d["totals"]["burden_computed"] is False and "EST_NO_BURDEN_RATE" in _codes(d)


def test_no_burden_warning_when_slot_20_is_outside_the_basis(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    """Owner's answer 5: a tenant that has not put Labor Burden in its basis sees the
    with-burden column, no warning, and an EAC that burden does not touch."""
    configure_tenant(rw_engine, seed, fresh_tenant)  # the D-04 basis, without slot 20
    t = {"client": login_as("rotate_me", tenant=fresh_tenant), "id": fresh_tenant}
    _load(t, rw_engine, ELM.read_bytes())
    d = _detail(t, EID)
    assert "EST_NO_BURDEN_RATE" not in _codes(d) and "EST_NO_BURDEN_DATE" not in _codes(d)
    assert d["totals"]["eac_in_basis"] == d["totals"]["eac_in_basis_as_estimated"] == "286634.20"
    assert d["totals"]["eac_not_computed"] is False


def test_reading_writes_nothing(tenant: dict, rw_engine: Engine, owner_engine: Engine) -> None:
    _load(tenant, rw_engine, ELM.read_bytes())

    def snapshot():
        with tenant_session(rw_engine, tenant["id"]) as s:
            lines = s.execute(
                select(
                    EstimateCost.id,
                    EstimateCost.cost_code,
                    EstimateCost.hours,
                    EstimateCost.amount,
                    EstimateCost.division_id,
                    EstimateCost.cost_category_id,
                ).order_by(EstimateCost.id)
            ).all()
            audits = s.execute(select(func.count()).select_from(AuditLog)).scalar_one()
        with owner_engine.connect() as c:
            head = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        return lines, audits, head

    before = snapshot()
    for _ in range(2):
        _detail(tenant, EID)
    assert snapshot() == before and len(before[0]) == 59


def test_every_role_sees_burden_and_nobody_writes_it(
    tenant: dict,
    rw_engine: Engine,
    seed: Seed,
    login_as: Callable[..., TestClient],
    owner_engine: Engine,
) -> None:
    _load(tenant, rw_engine, ELM.read_bytes())
    with tenant_session(owner_engine, tenant["id"]) as s:
        s.add(
            Membership(
                tenant_id=tenant["id"],
                user_id=seed.users["client_viewer"].id,
                role=Role.client_viewer,
            )
        )
    viewer = login_as("client_viewer", tenant=tenant["id"])
    d = _detail({"client": viewer}, EID)
    assert d["totals"]["burden_total"] == "6383.50" and d["totals"]["eac_in_basis"] == "293017.70"
    for c in (viewer, tenant["client"]):
        for method in ("post", "put", "patch", "delete"):
            r = getattr(c, method)(f"/api/estimates/{d['id']}", headers=CSRF)
            assert r.status_code == 405, (method, r.status_code)


# --- configuration lists: the API still returns inactive rows (hiding is on screen) ---------------
def test_config_lists_return_inactive_rows(tenant: dict, seed: Seed, rw_engine: Engine) -> None:
    c = tenant["client"]
    rates = c.get("/api/config/burden-rates").json()
    assert (len(rates), sum(not r["active"] for r in rates)) == (5, 1)
    with tenant_session(rw_engine, tenant["id"]) as s:
        s.add(
            GlAccount(tenant_id=tenant["id"], account_no="5999", name="Old job cost", active=False)
        )
        s.add(GlAccount(tenant_id=tenant["id"], account_no="5998", name="Job cost", active=True))
        cat = s.execute(select(CostCategory).where(CostCategory.slot == "80")).scalar_one()
        cat_id = cat.id
    div = c.post(
        "/api/config/divisions",
        json={"code": "OLD", "name": "Old line", "code_digit": "8"},
        headers=CSRF,
    ).json()
    assert c.post(f"/api/config/divisions/{div['id']}/deactivate", headers=CSRF).status_code == 200
    assert (
        c.post(f"/api/config/cost-categories/{cat_id}/deactivate", headers=CSRF).status_code == 200
    )
    divisions = c.get("/api/config/divisions").json()
    assert any(d["code"] == "OLD" and not d["active"] for d in divisions)
    categories = c.get("/api/config/cost-categories").json()
    assert any(x["slot"] == "80" and not x["active"] for x in categories)
    default = c.get("/api/config/accounts").json()
    assert [a["account_no"] for a in default["accounts"]] == ["5998"]
    assert (default["total_active"], default["inactive_count"]) == (1, 1)
    everything = c.get("/api/config/accounts?include_inactive=true").json()
    assert [(a["account_no"], a["active"]) for a in everything["accounts"]] == [
        ("5998", True),
        ("5999", False),
    ]
    assert (everything["total_active"], everything["unmapped_count"]) == (1, 1)
