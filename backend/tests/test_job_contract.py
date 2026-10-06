"""F07: work-area kinds and the job's contract figures (D-01; owner's answers 5, 7, 8,
9). Revised contract counts only confirmed originals; EAC in the WIP basis is the
F06.1 figure and does not move with kind; a confirmation is carried to the next
version only when the order number and the name both match."""

import uuid
from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.config_helpers import run_until_quiet
from tests.conftest import Seed
from tests.estimate_helpers import ELM, TURLEY, build_workbook, fixture_rows, upload_template
from tests.job_helpers import ELM_ID, TURLEY_ID, Tenant, make_tenant

D = Decimal


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    return make_tenant(seed, rw_engine, login_as, fresh_tenant)


def _areas(job: dict) -> dict[int, dict]:
    return {w["order_no"]: w for w in job["work_areas"]}


def _confirm(t: Tenant, job: dict, orders: list[int], kind: str) -> dict:
    areas = _areas(job)
    for o in orders:
        job = t.send(
            "POST", f"/api/jobs/{job['id']}/work-areas/{areas[o]['id']}/kind", {"kind": kind}
        )
    return job


def test_elm_street_kinds_and_revised_contract(t: Tenant) -> None:
    job = t.new_job(ELM_ID)
    areas = _areas(job)
    assert [o for o, w in areas.items() if w["suggested_kind"] == "change_order"] == list(
        range(18, 30)
    )
    assert [o for o, w in areas.items() if w["suggested_kind"] == "original"] == list(range(1, 17))
    assert areas[17]["kind_label"] == "Omitted" and areas[17]["suggested_kind"] is None
    assert areas[18]["kind_label"] == "Change order (suggested)"
    assert (job["revised_contract"], job["to_confirm"]) == ("0.00", 28)
    assert job["revised_contract_note"] == "28 work areas to confirm"
    assert job["unapproved_change_orders"] == "0.00"
    assert job["eac_in_basis"] == "327929.93"  # F06.1, EX 0.1959 on 59,941.16 of labor
    seen = t.audit_rows()
    job = _confirm(t, job, [18], "change_order")
    (row,) = t.audit_actions(seen)
    assert row.action == "work_area_kind_confirmed"
    assert (row.detail["before"], row.detail["after"]) == ({"kind": None}, {"kind": "change_order"})
    assert row.detail["rows"]["order_no"] == 18 and row.detail["rows"]["estimate"] == ELM_ID
    assert job["to_confirm"] == 27 and _areas(job)[18]["kind_label"] == "Change order, confirmed"
    job = _confirm(t, job, list(range(1, 17)), "original")
    job = _confirm(t, job, list(range(19, 30)), "change_order")
    assert job["revised_contract"] == "465469.59"
    assert job["unapproved_change_orders"] == "53704.13"
    assert job["to_confirm"] == 0 and job["revised_contract_note"] is None
    assert job["eac_in_basis"] == "327929.93"  # kind never moves EAC
    estimate = t.get(f"/api/estimates/{t.estimate_id(ELM_ID)}")
    assert estimate["totals"]["kept_total"] == "519173.72"
    r = t.client.post(
        f"/api/jobs/{job['id']}/work-areas/{areas[17]['id']}/kind",
        json={"kind": "original"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 422 and "omitted" in r.json()["detail"]


def test_a_confirmation_is_carried_forward_only_when_order_and_name_match(t: Tenant) -> None:
    job = t.new_job(ELM_ID)
    job = _confirm(t, job, list(range(1, 17)), "original")
    job = _confirm(t, job, list(range(18, 30)), "change_order")
    sheets = fixture_rows(ELM)
    # The same work areas as a new version (the estimator field filled in).
    sheets["Estimates"][0]["estimator"] = "Test Estimator"
    upload_template(t.client, build_workbook(sheets=dict(sheets)), "elm_v2.xlsx")
    run_until_quiet(t.engine)
    again = t.job(job["id"])
    assert [a["id"] for a in again["work_areas"]] != [a["id"] for a in job["work_areas"]]
    assert (again["revised_contract"], again["to_confirm"]) == ("465469.59", 0)
    # A new name at order 3 drops that one confirmation (the renumbered case).
    price3 = D(_areas(again)[3]["price"])
    for w in sheets["Work areas"]:
        if w["order"] == 3:
            w["name"] = "Renamed work area"
    upload_template(t.client, build_workbook(sheets=dict(sheets)), "elm_v3.xlsx")
    run_until_quiet(t.engine)
    third = t.job(job["id"])
    assert third["to_confirm"] == 1 and _areas(third)[3]["confirmed"] is False
    assert D(third["revised_contract"]) == D("465469.59") - price3
    assert third["unapproved_change_orders"] == "53704.13"


def test_turley_contract_is_computed_from_the_fixture(t: Tenant) -> None:
    rows = {w["order"]: w for w in fixture_rows(TURLEY)["Work areas"]}
    originals = [*range(1, 6), 8, 9, *range(11, 20)]
    change_orders = list(range(24, 33))
    assert all(rows[o]["kept"] == "Y" for o in originals + change_orders)
    job = t.new_job(TURLEY_ID)
    job = _confirm(t, job, originals, "original")
    job = _confirm(t, job, change_orders, "change_order")
    expected_contract = sum(
        (D(str(rows[o]["price"])).quantize(D("0.01")) for o in originals), D("0.00")
    )
    expected_co = sum(
        (D(str(rows[o]["price"])).quantize(D("0.01")) for o in change_orders), D("0.00")
    )
    assert D(job["revised_contract"]) == expected_contract
    assert D(job["unapproved_change_orders"]) == expected_co
    assert job["to_confirm"] == 0
    assert expected_contract + expected_co == D("366889.80")  # every kept row, once


def test_time_and_materials_shows_no_contract_and_a_change_order_estimate_adds_its_eac(
    t: Tenant,
) -> None:
    job = t.new_job(ELM_ID)
    turley = t.queue()[TURLEY_ID]
    after = t.send(
        "POST",
        f"/api/jobs/{job['id']}/estimates",
        {"estimate_id": turley["estimate_id"], "role": "change_order"},
    )
    turley_eac = t.get(f"/api/estimates/{turley['estimate_id']}")["totals"]["eac_in_basis"]
    assert D(after["eac_in_basis"]) == D("327929.93") + D(turley_eac)
    assert after["unapproved_change_orders"] == "366889.80"  # every kept Turley row, by role
    tm = t.send("PATCH", f"/api/jobs/{job['id']}", {"revenue_method": "time_and_materials"})
    assert tm["revised_contract"] is None and tm["unapproved_change_orders"] is None
    assert "D-24" in tm["revised_contract_note"]
    # A kind is confirmed only on the latest version of the original's work areas.
    r = t.client.post(
        f"/api/jobs/{job['id']}/work-areas/{uuid.uuid4()}/kind",
        json={"kind": "original"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 404 and "original estimate" in r.json()["detail"]
