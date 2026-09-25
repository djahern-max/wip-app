"""F06: the estimate template through the whole pipeline (upload → process →
normalize), on the Rye Beach fixtures and on test copies: idempotence, updates by
id, versions and the D-01 baseline, cost-code resolution, the exceptions, the
three-CSV form in any order, and the audit rows."""

import uuid
from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from app.audit.models import AuditLog
from app.core.db import tenant_session
from app.domain.estimates.models import Estimate, EstimateCost, EstimateVersion
from tests.config_helpers import batch_json, run_until_quiet
from tests.conftest import Seed
from tests.estimate_helpers import (
    EIGHTY,
    ELM,
    TURLEY,
    build_csv,
    build_workbook,
    configure_tenant,
    estimate_rows,
    fixture_rows,
    upload_template,
    versions_of,
    work_areas_of,
)

D = Decimal
EID = "EST6115758"
TID = "EST6120638"


@pytest.fixture
def tenant(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> dict:
    configure_tenant(rw_engine, seed, fresh_tenant)
    return {"client": login_as("rotate_me", tenant=fresh_tenant), "id": fresh_tenant}


def _load(tenant: dict, engine: Engine, content: bytes, filename: str = "estimates.xlsx") -> dict:
    batch = upload_template(tenant["client"], content, filename)["batch"]
    run_until_quiet(engine)
    return batch_json(tenant["client"], batch["id"])


def _audit(engine: Engine, tenant_id: uuid.UUID, external_id: str | None = None) -> list[dict]:
    with tenant_session(engine, tenant_id) as s:
        rows = s.execute(
            select(AuditLog)
            .where(AuditLog.action.like("estimate_%"))
            .order_by(AuditLog.occurred_at, AuditLog.id)
        ).scalars()
        return [
            {"action": r.action, **(r.detail or {})}
            for r in rows
            if external_id is None or (r.detail or {}).get("external_id") == external_id
        ]


def _detail(tenant: dict, external_id: str) -> dict:
    rows = tenant["client"].get("/api/estimates").json()["estimates"]
    row = next(r for r in rows if r["external_id"] == external_id)
    r = tenant["client"].get(f"/api/estimates/{row['id']}")
    assert r.status_code == 200, r.text
    return r.json()


def _codes(detail: dict) -> list[str]:
    return [i["code"] for i in detail["attention"]]


def _wa(detail: dict, order: int) -> dict:
    return next(w for w in detail["work_areas"] if w["order_no"] == order)


# --- 67 Elm Street


def test_67_elm_loads_with_every_number_in_the_brief(tenant: dict, rw_engine: Engine) -> None:
    b = _load(tenant, rw_engine, ELM.read_bytes(), "estimate_upload_EST6115758.xlsx")
    assert (b["status"], b["rows_loaded"], b["rows_rejected"]) == ("loaded", 3, 0)
    assert (b["followup_status"], b["message"]) == ("succeeded", "Loaded. Estimates updated.")
    assert b["issues"] == []
    est = estimate_rows(rw_engine, tenant["id"])[EID]
    assert (est.status, est.status_norm, est.price, est.estimator) == (
        "Sold",
        "sold",
        D("475129.68"),
        None,
    )
    (v1,) = versions_of(rw_engine, tenant["id"], EID)
    assert (v1.version_no, v1.is_baseline, v1.kept_total, v1.status_norm) == (
        1,
        True,
        D("475129.68"),
        "sold",
    )
    was = work_areas_of(rw_engine, tenant["id"], v1.id)
    assert len(was) == 21 and [w["order"] for w in was if not w["kept"]] == [17]
    assert [w["order"] for w in was if w["co"]] == [18, 19, 20, 21]
    assert sum(len(w["lines"]) for w in was) == 59  # 60 rows; the two 250 lines of #1 summed
    assert all(ln["category"] and ln["division"] for w in was for ln in w["lines"])
    assert [ln["code"] for ln in was[4]["lines"]] == ["290"]
    assert was[16]["lines"] == [] and was[19]["lines"] == []
    d = _detail(tenant, EID)
    t = d["totals"]
    assert (t["kept_original"], t["kept_change_orders"], t["kept_total"], t["omitted"]) == (
        "465469.59",
        "9660.09",
        "475129.68",
        "15000.00",
    )
    assert (t["kept_cost"], t["kept_hours"], t["eac_in_basis"], t["basis_decided"]) == (
        "315832.69",
        "856.00",
        "286634.20",
        True,
    )
    by_slot = {c["slot"]: (c["amount"], c["in_basis_label"]) for c in t["by_category"]}
    assert by_slot["10"] == ("32585.55", "Yes") and by_slot["45"] == ("14589.70", "No")
    assert by_slot["90"] == ("46531.35", "Yes") and by_slot["20"] == ("0.00", "No")
    assert _codes(d) == ["EST_UNIT_PRICED"]
    assert d["attention"][0]["message"].startswith('Work area #18 "CO: Ledge Removal per Day"')
    assert d["baseline_version_no"] == 1 and d["versions"] == 1
    wa5 = _wa(d, 5)
    assert (wa5["cost"], wa5["lines"][0]["category_name"], wa5["lines"][0]["division_code"]) == (
        "46381.35",
        "Other",
        "EX",
    )
    actions = _audit(rw_engine, tenant["id"], EID)
    assert [a["action"] for a in actions] == ["estimate_created", "estimate_version_created"]
    assert actions[0]["after"]["price"] == "475129.68"


def test_the_same_file_again_is_a_duplicate_and_a_re_export_changes_nothing(
    tenant: dict, rw_engine: Engine
) -> None:
    _load(tenant, rw_engine, ELM.read_bytes())
    r = upload_template(tenant["client"], ELM.read_bytes(), "again.xlsx")
    assert r["duplicate"] is True
    run_until_quiet(rw_engine)
    # A re-export: the same rows, different bytes.
    b = _load(tenant, rw_engine, build_workbook(sheets=fixture_rows(ELM)), "re-export.xlsx")
    assert (b["status"], b["rows_loaded"], b["followup_status"]) == ("loaded", 3, "unchanged")
    assert b["followup_label"] == "Nothing changed"
    assert b["message"] == "Loaded. Nothing changed: this file was already loaded."
    assert len(versions_of(rw_engine, tenant["id"], EID)) == 1
    assert [a["action"] for a in _audit(rw_engine, tenant["id"])] == [
        "estimate_created",
        "estimate_version_created",
    ]


def test_a_changed_header_updates_one_estimate_by_id_with_one_audit_row(
    tenant: dict, rw_engine: Engine
) -> None:
    _load(tenant, rw_engine, ELM.read_bytes(), "elm.xlsx")
    _load(tenant, rw_engine, TURLEY.read_bytes(), "turley.xlsx")
    rows = fixture_rows(TURLEY)
    rows["Estimates"][0].update({"status": "Pending", "price": "300000.00"})
    b = _load(tenant, rw_engine, build_workbook(sheets=rows), "turley-2.xlsx")
    assert b["followup_status"] == "succeeded"
    est = estimate_rows(rw_engine, tenant["id"])
    assert (est[TID].status_norm, est[TID].price) == ("pending", D("300000.00"))
    assert (est[EID].status_norm, est[EID].price) == ("sold", D("475129.68"))
    updated = [a for a in _audit(rw_engine, tenant["id"]) if a["action"] == "estimate_updated"]
    assert len(updated) == 1
    assert updated[0]["changed_fields"] == ["price", "status", "status_norm"]
    assert updated[0]["before"] == {"price": "366889.80", "status": "Sold", "status_norm": "sold"}
    assert updated[0]["external_id"] == TID and updated[0]["version_no"] == 2
    assert len(versions_of(rw_engine, tenant["id"], TID)) == 2
    assert len(versions_of(rw_engine, tenant["id"], EID)) == 1
    d = _detail(tenant, TID)
    assert "EST_PRICE_MISMATCH" in _codes(d) and d["baseline_version_no"] == 1


@pytest.mark.skipif(not EIGHTY.exists(), reason="owner supplies estimates_2026-09-17.xlsx")
def test_the_80_row_workbook_loads_79_and_est6326680_updates_by_id(
    tenant: dict, rw_engine: Engine
) -> None:
    b = _load(tenant, rw_engine, EIGHTY.read_bytes(), "estimates_2026-09-17.xlsx")
    assert (b["status"], b["rows_loaded"], b["rows_rejected"]) == ("loaded_with_issues", 79, 1)
    assert [i["code"] for i in b["issues"]] == ["EST_NO_ID"]
    rows = estimate_rows(rw_engine, tenant["id"])
    assert len(rows) == 79
    by = {}
    for e in rows.values():
        n, total = by.get((e.estimator, e.status_norm), (0, D(0)))
        by[(e.estimator, e.status_norm)] = (n + 1, total + e.price)
    assert by[("William Hess", "sold")] == (10, D("429962.74"))
    assert by[("Stephanie Sanford", "lost")] == (11, D("190798.89"))
    listing = tenant["client"].get("/api/estimates?status=sold").json()
    assert listing["total"] == 15
    assert set(listing["estimators"]) == {"William Hess", "Stephanie Sanford"}
    # Every sold estimate has no work areas yet: the D-04 exception, and only that.
    assert all(
        [i["code"] for i in r["attention"]] == ["EST_NO_CATEGORY_SPLIT"]
        for r in listing["estimates"]
    )
    # A newer file: EST6326680 sold at another price; nothing else touched.
    data = fixture_rows(EIGHTY)
    row = next(r for r in data["Estimates"] if r["estimate_id"] == "EST6326680")
    assert (row["status"], D(str(row["price"]))) == ("Pending", D("58102.43"))
    row.update({"status": "Sold", "price": "60000.00"})
    before = {k: (v.status_norm, v.price) for k, v in rows.items()}
    b2 = _load(tenant, rw_engine, build_workbook(sheets=data), "estimates-2.xlsx")
    assert b2["followup_status"] == "succeeded"
    after = {k: (v.status_norm, v.price) for k, v in estimate_rows(rw_engine, tenant["id"]).items()}
    assert after.pop("EST6326680") == ("sold", D("60000.00"))
    before.pop("EST6326680")
    assert after == before
    updated = [a for a in _audit(rw_engine, tenant["id"]) if a["action"] == "estimate_updated"]
    assert len(updated) == 1 and updated[0]["changed_fields"] == ["price", "status", "status_norm"]
    assert len(versions_of(rw_engine, tenant["id"], "EST6326680")) == 2


# --- zero prices, unknown status, no id


def test_zero_prices_and_status_words(tenant: dict, rw_engine: Engine) -> None:
    content = build_workbook(
        estimates=[
            {"estimate_id": "EST1", "name": "a", "status": "Pending", "price": 0},
            {"estimate_id": "EST2", "name": "b", "status": "Lost", "price": "0.00"},
            {"estimate_id": "EST3", "name": "c", "status": "Sold", "price": 0},
            {"estimate_id": "EST4", "name": "d", "status": "Won", "price": "5.00"},
            {"name": "no id", "status": "Sold", "price": "8548.03"},
        ]
    )
    b = _load(tenant, rw_engine, content)
    assert (b["status"], b["rows_loaded"], b["rows_rejected"]) == ("loaded_with_issues", 4, 1)
    assert b["message"].startswith("1 row could not be read and was skipped; the rest were loaded.")
    assert [i["code"] for i in b["issues"]] == ["EST_NO_ID"]
    assert 'Row 6 on "Estimates" has no estimate id' in b["issues"][0]["message"]
    rows = {r["external_id"]: r for r in tenant["client"].get("/api/estimates").json()["estimates"]}
    assert set(rows) == {"EST1", "EST2", "EST3", "EST4"}
    assert [i["code"] for i in rows["EST1"]["attention"]] == []
    assert [i["code"] for i in rows["EST2"]["attention"]] == []
    assert [i["code"] for i in rows["EST3"]["attention"]] == [
        "EST_ZERO_SOLD",
        "EST_NO_CATEGORY_SPLIT",
    ]
    assert [i["code"] for i in rows["EST4"]["attention"]] == ["EST_UNKNOWN_STATUS"]
    assert (rows["EST4"]["status_label"], rows["EST4"]["status_norm"]) == ("Won", None)
    assert (rows["EST1"]["price"], rows["EST3"]["status_label"]) == ("0.00", "Sold")
    unknown = tenant["client"].get("/api/estimates?status=unknown").json()
    assert [r["external_id"] for r in unknown["estimates"]] == ["EST4"]


# --- the exceptions from test copies


def test_unknown_code_line_on_omitted_price_mismatch_and_missing_split(
    tenant: dict, rw_engine: Engine
) -> None:
    rows = fixture_rows(ELM)
    costs = rows["Estimate costs"]
    next(c for c in costs if c["order"] == 1)["cost_code"] = "999"  # unknown code
    costs.append({"estimate_id": EID, "order": 17, "cost_code": "230", "amount": "10.00"})
    rows["Estimate costs"] = [c for c in costs if c["order"] != 2]  # #2 loses its split
    b = _load(tenant, rw_engine, build_workbook(sheets=rows))
    assert b["status"] == "loaded" and b["issues"] == []
    d = _detail(tenant, EID)
    assert sorted(_codes(d)) == sorted(
        [
            "EST_UNIT_PRICED",
            "EST_NO_CATEGORY_SPLIT",
            "EST_UNKNOWN_COST_CODE",
            "EST_COST_LINE_ON_OMITTED",
        ]
    )
    by_code = {i["code"]: i["message"] for i in d["attention"]}
    assert by_code["EST_UNKNOWN_COST_CODE"].startswith("Cost code 999 on work area #1")
    assert by_code["EST_COST_LINE_ON_OMITTED"].startswith(
        "Work area #17 is omitted but has cost lines totalling 10.00."
    )
    assert by_code["EST_NO_CATEGORY_SPLIT"].startswith(
        "Estimated cost by cost category is missing for work area #2"
    )
    unknown = next(c for c in d["totals"]["by_category"] if c["slot"] is None)
    assert unknown["name"] == "Unknown cost code" and unknown["in_basis_label"] == "No"
    wa17 = _wa(d, 17)
    assert wa17["cost"] == "10.00" and wa17["lines"][0]["category_name"] == "Materials"
    line999 = next(ln for ln in _wa(d, 1)["lines"] if ln["cost_code"] == "999")
    assert line999["category_name"] is None and line999["division_code"] is None
    # Totals leave out #17's line and #2's missing lines (13,616.27 was never there here).
    assert d["totals"]["kept_cost"] == str(D("315832.69") - D("7217.62"))
    # The price mismatch: kept prices no longer sum to the estimate price.
    rows = fixture_rows(ELM)
    rows["Estimates"][0]["price"] = "475000.00"
    _load(tenant, rw_engine, build_workbook(sheets=rows))
    d = _detail(tenant, EID)
    mismatch = next(i for i in d["attention"] if i["code"] == "EST_PRICE_MISMATCH")
    assert mismatch["message"].startswith(
        "Kept work areas total 475129.68 but the estimate price is 475000.00"
    )


# --- versions and the baseline (D-01, plan question 11) -------------------------------------------
def _turley(**estimate_over) -> dict[str, list[dict]]:
    rows = fixture_rows(TURLEY)
    rows["Estimates"][0].update(estimate_over)
    return rows


def test_versions_pending_first_then_sold_then_the_three_scenarios(
    tenant: dict, rw_engine: Engine
) -> None:
    # 1. Pending: the version is not the baseline and raises no comparison exception.
    b = _load(tenant, rw_engine, build_workbook(sheets=_turley(status="Pending")), "v1.xlsx")
    assert b["followup_status"] == "succeeded"
    (v1,) = versions_of(rw_engine, tenant["id"], TID)
    assert (v1.is_baseline, v1.status_norm, v1.kept_total) == (False, "pending", D("366889.80"))
    assert _codes(_detail(tenant, TID)) == []
    # 2. The sale: the latest version with work areas (v1) becomes the baseline.
    _load(tenant, rw_engine, build_workbook(sheets=_turley()), "v2.xlsx")
    v1, v2 = versions_of(rw_engine, tenant["id"], TID)
    assert (v1.is_baseline, v2.is_baseline, v2.status_norm) == (True, False, "sold")
    d = _detail(tenant, TID)
    assert _codes(d) == [] and d["baseline_version_no"] == 1
    # 3a. Row 2 omitted: a deductive change naming order 2 and 23,221.63.
    rows = _turley(price=str(D("366889.80") - D("23221.63")))
    next(w for w in rows["Work areas"] if w["order"] == 2)["kept"] = "N"
    _load(tenant, rw_engine, build_workbook(sheets=rows), "v3.xlsx")
    d = _detail(tenant, TID)
    assert _codes(d) == ["EST_COST_LINE_ON_OMITTED", "EST_DEDUCTIVE_CHANGE"]
    assert d["attention"][1]["message"].startswith(
        "Work area #2 (23221.63) was in the baseline and is now omitted"
    )
    assert d["baseline_version_no"] == 1 and d["versions"] == 3
    # 3b. A new row 33: a change order by definition, no name rule involved.
    rows = _turley(price=str(D("366889.80") + D("100.00")))
    rows["Work areas"].append(
        {"estimate_id": TID, "order": 33, "kept": "Y", "name": "Extra planting", "price": "100.00"}
    )
    rows["Estimate costs"].append(
        {"estimate_id": TID, "order": 33, "cost_code": "130", "amount": "60.00"}
    )
    _load(tenant, rw_engine, build_workbook(sheets=rows), "v4.xlsx")
    d = _detail(tenant, TID)
    assert _codes(d) == []
    wa33 = _wa(d, 33)
    assert wa33["change_order_suggested"] is True and wa33["name"] == "Extra planting"
    assert d["totals"]["kept_change_orders"] == str(D("65336.58") + D("100.00"))
    # 3c. A different name at order 5: reported, nothing re-keyed.
    rows = _turley()
    next(w for w in rows["Work areas"] if w["order"] == 5)["name"] = "SOMETHING ELSE"
    _load(tenant, rw_engine, build_workbook(sheets=rows), "v5.xlsx")
    d = _detail(tenant, TID)
    assert _codes(d) == ["EST_WORK_AREA_RENUMBERED"]
    assert d["attention"][0]["message"].startswith(
        'Work area #5 was "MOVING EXISTING GRANITE DECK STEPS" in the baseline'
    )
    assert [w["order_no"] for w in d["work_areas"]] == list(range(1, 33))
    assert _wa(d, 5)["name"] == "SOMETHING ELSE"
    versions = versions_of(rw_engine, tenant["id"], TID)
    assert [v.version_no for v in versions] == [1, 2, 3, 4, 5]
    assert [v.is_baseline for v in versions] == [True, False, False, False, False]
    listed = [
        (v["version_no"], v["is_baseline"], v["original_filename"]) for v in d["versions_list"]
    ]
    assert listed[:2] == [(1, True, "v1.xlsx"), (2, False, "v2.xlsx")]


def test_a_sold_upload_with_new_work_areas_is_itself_the_baseline(
    tenant: dict, rw_engine: Engine
) -> None:
    header = {"estimate_id": TID, "name": "T", "status": "Pending", "price": "366889.80"}
    _load(tenant, rw_engine, build_csv("Estimates", [header]), "e.csv")
    _load(tenant, rw_engine, build_csv("Estimates", [{**header, "status": "Sold"}]), "e2.csv")
    v1, v2 = versions_of(rw_engine, tenant["id"], TID)
    assert (v1.is_baseline, v2.is_baseline) == (False, False)  # no work areas yet
    assert _codes(_detail(tenant, TID)) == ["EST_NO_CATEGORY_SPLIT"]
    _load(tenant, rw_engine, TURLEY.read_bytes(), "turley.xlsx")
    versions = versions_of(rw_engine, tenant["id"], TID)
    assert [(v.version_no, v.is_baseline) for v in versions] == [(1, False), (2, False), (3, True)]
    assert _codes(_detail(tenant, TID)) == []


# --- the three-CSV form, in any order -------------------------------------------------------------
def test_three_csvs_in_any_order_equal_the_workbook(tenant: dict, rw_engine: Engine) -> None:
    rows = fixture_rows(ELM)
    b1 = _load(tenant, rw_engine, build_csv("Estimate costs", rows["Estimate costs"]), "costs.csv")
    assert b1["status"] == "loaded" and b1["followup_status"] == "succeeded"
    assert len(b1["issues"]) == 1 and b1["issues"][0]["message"].startswith(
        "EST6115758 is not on the Estimates sheet and is not a loaded estimate yet; "
        "its work areas and cost lines are held"
    )
    assert estimate_rows(rw_engine, tenant["id"]) == {}
    b2 = _load(tenant, rw_engine, build_csv("Work areas", rows["Work areas"]), "areas.csv")
    assert len(b2["issues"]) == 1 and estimate_rows(rw_engine, tenant["id"]) == {}
    b3 = _load(tenant, rw_engine, build_csv("Estimates", rows["Estimates"]), "estimates.csv")
    assert b3["issues"] == [] and b3["followup_status"] == "succeeded"
    (v1,) = versions_of(rw_engine, tenant["id"], EID)
    assert v1.is_baseline and v1.kept_total == D("475129.68")
    d = _detail(tenant, EID)
    assert (d["totals"]["kept_cost"], d["totals"]["eac_in_basis"]) == ("315832.69", "286634.20")
    assert _codes(d) == ["EST_UNIT_PRICED"]
    # A costs-only file for a loaded estimate naming a work area it does not have.
    stray = [{"estimate_id": EID, "order": 99, "cost_code": "130", "amount": "1.00"}]
    b4 = _load(tenant, rw_engine, build_csv("Estimate costs", stray), "stray.csv")
    assert b4["issues"][0]["message"].startswith(
        "EST6115758: cost lines for work area #99 were held"
    )
    d = _detail(tenant, EID)
    # The latest cost-lines sheet holds one line, and it is held: the version has none.
    assert d["versions"] == 2 and d["totals"]["kept_cost"] == "0.00"
    assert d["totals"]["kept_total"] == "475129.68"


def test_a_malformed_row_and_a_bad_kept_value_leave_the_rest_loaded(
    tenant: dict, rw_engine: Engine
) -> None:
    rows = fixture_rows(ELM)
    next(w for w in rows["Work areas"] if w["order"] == 3)["kept"] = "maybe"
    next(c for c in rows["Estimate costs"] if c["order"] == 4)["amount"] = "lots"
    b = _load(tenant, rw_engine, build_workbook(sheets=rows))
    # The bad work-area row takes its three cost lines with it (no such work area in
    # this file), so five rows are named, each with its sentence.
    assert (b["status"], b["rows_loaded"], b["rows_rejected"]) == ("loaded_with_issues", 3, 5)
    messages = sorted(i["message"] for i in b["issues"])
    assert len(messages) == 5
    assert 'Row 16 on "Estimate costs" was not loaded: not a number.' in messages
    assert 'Row 4 on "Work areas" was not loaded: kept must be Y or N.' in messages
    assert sum('work area #3 of EST6115758 is not on "Work areas"' in m for m in messages) == 3
    d = _detail(tenant, EID)
    assert [w["order_no"] for w in d["work_areas"]] == [o for o in range(1, 22) if o != 3]
    assert "EST_PRICE_MISMATCH" in _codes(d)


def test_tenant_b_sees_none_of_tenant_a_estimates(
    tenant: dict, rw_engine: Engine, seed: Seed, login_as: Callable[..., TestClient]
) -> None:
    _load(tenant, rw_engine, ELM.read_bytes())
    other = login_as("firm_admin", tenant=seed.tenant_b)
    assert other.get("/api/estimates").json()["estimates"] == []
    est = estimate_rows(rw_engine, tenant["id"])[EID]
    assert other.get(f"/api/estimates/{est.id}").status_code == 404
    with tenant_session(rw_engine, seed.tenant_b) as s:
        for table in ("estimate", "estimate_version", "estimate_work_area", "estimate_cost"):
            assert s.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one() == 0
    with tenant_session(rw_engine, tenant["id"]) as s:
        assert s.execute(select(EstimateCost)).first() is not None
        assert s.execute(select(EstimateVersion)).first() is not None
        assert s.execute(select(Estimate)).first() is not None
