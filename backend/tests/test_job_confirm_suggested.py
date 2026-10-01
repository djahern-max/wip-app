"""F07.1: 67 Elm Street as it stands on production (the v2 fixture loaded after the F06
one, as production did) and "Confirm all as suggested" (D-01: the suggestion is the
platform's, the confirmation is the person's; one press confirms each suggestion and
writes one ``work_area_kind_confirmed`` row per work area, never a summary row)."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.core.db import tenant_session
from app.domain.estimates.models import EstimateWorkArea
from app.domain.jobs.service import AreaChoice, as_suggested
from tests.config_helpers import run_until_quiet
from tests.conftest import Seed
from tests.estimate_helpers import ELM_V2, upload_template
from tests.job_helpers import ELM_ID, Tenant, make_tenant

D = Decimal
CONFIRM_ALL = "work-areas/kinds/confirm-suggested"
ORIGINALS = list(range(1, 17))
CHANGE_ORDERS = list(range(18, 30))


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    t = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    upload_template(t.client, ELM_V2.read_bytes(), "estimate_upload_EST6115758_v2.xlsx")
    run_until_quiet(t.engine)
    return t


def _areas(job: dict) -> dict[int, dict]:
    return {w["order_no"]: w for w in job["work_areas"]}


def _confirm_all(t: Tenant, job_id: str) -> dict:
    return t.send("POST", f"/api/jobs/{job_id}/{CONFIRM_ALL}")


def _confirm(t: Tenant, job: dict, orders: list[int], kind: str) -> dict:
    areas = _areas(job)
    for o in orders:
        job = t.send(
            "POST", f"/api/jobs/{job['id']}/work-areas/{areas[o]['id']}/kind", {"kind": kind}
        )
    return job


def _confirmed_at(t: Tenant, ids: list[str]) -> dict[str, datetime]:
    with tenant_session(t.engine, t.id) as s:
        rows = s.execute(
            select(EstimateWorkArea).where(EstimateWorkArea.id.in_([UUID(i) for i in ids]))
        ).scalars()
        return {str(r.id): r.kind_confirmed_at for r in rows}


def test_the_second_file_is_the_latest_version_with_the_production_figures(t: Tenant) -> None:
    d = t.get(f"/api/estimates/{t.estimate_id(ELM_ID)}")
    assert d["versions"] == 2 and d["price"] == "519173.72"
    areas = {w["order_no"]: w for w in d["work_areas"]}
    assert len(areas) == 29 and sum(1 for w in areas.values() if w["kept"]) == 28
    assert [o for o, w in areas.items() if not w["kept"]] == [17]
    assert sum(len(w["lines"]) for w in areas.values()) == 104
    totals = d["totals"]
    assert totals["kept_total"] == "519173.72"
    assert (totals["cost_total_as_estimated"], totals["kept_cost"]) == ("343693.43", "343693.43")
    assert totals["burden_total"] == "11742.47"  # EX 0.1959 on 59,941.16 of labor
    assert (totals["eac_in_basis"], totals["eac_not_computed"]) == ("327929.93", False)


def test_the_job_before_any_confirmation(t: Tenant) -> None:
    job = t.new_job(ELM_ID)
    areas = _areas(job)
    assert (job["revised_contract"], job["to_confirm"]) == ("0.00", 28)
    assert job["revised_contract_note"] == "28 work areas to confirm"
    assert (job["unapproved_change_orders"], job["eac_in_basis"]) == ("0.00", "327929.93")
    assert [o for o, w in areas.items() if w["suggested_kind"] == "original"] == ORIGINALS
    assert [o for o, w in areas.items() if w["suggested_kind"] == "change_order"] == CHANGE_ORDERS
    assert areas[17]["suggested_kind"] is None and areas[17]["kind_label"] == "Omitted"
    assert areas[1]["kind_label"] == "Original (suggested)"
    assert areas[18]["kind_label"] == "Change order (suggested)"


def test_confirm_all_as_suggested_writes_one_row_per_work_area_and_is_idempotent(
    t: Tenant,
) -> None:
    job = t.new_job(ELM_ID)
    before = _areas(job)
    seen = t.audit_rows()
    out = _confirm_all(t, job["id"])
    assert (out["confirmed"], out["skipped"]) == (28, 0)
    assert out["message"] == "28 work areas confirmed as suggested."
    assert out["revised_contract"] == "465469.59"
    assert out["unapproved_change_orders"] == "53704.13"
    assert (out["to_confirm"], out["revised_contract_note"]) == (0, None)
    assert out["eac_in_basis"] == "327929.93"  # kind never moves EAC
    areas = _areas(out)
    assert all(areas[o]["kind_label"] == "Original, confirmed" for o in ORIGINALS)
    assert all(areas[o]["kind_label"] == "Change order, confirmed" for o in CHANGE_ORDERS)
    assert areas[17]["kind_label"] == "Omitted" and areas[17]["kind"] is None
    rows = t.audit_actions(seen)
    assert len(rows) == 28 and {r.action for r in rows} == {"work_area_kind_confirmed"}
    by_order = {r.detail["rows"]["order_no"]: r for r in rows}
    assert sorted(by_order) == ORIGINALS + CHANGE_ORDERS
    for o, r in by_order.items():
        assert r.detail["rows"]["estimate_work_area"] == before[o]["id"]
        assert r.detail["rows"]["estimate"] == ELM_ID
        assert r.detail["before"] == {"kind": None}
        assert r.detail["after"] == {"kind": before[o]["suggested_kind"]}
    assert len({r.request_id for r in rows}) == 1  # one request, one transaction
    # A second press has nothing to confirm: no row, the same figures.
    again = _confirm_all(t, job["id"])
    assert (again["confirmed"], again["skipped"]) == (0, 0)
    assert again["message"] == "Nothing was left to confirm."
    assert t.audit_rows() == seen + 28
    for key in ("revised_contract", "unapproved_change_orders", "eac_in_basis", "to_confirm"):
        assert again[key] == out[key], key


def test_hand_confirmed_work_areas_are_untouched_by_confirm_all(t: Tenant) -> None:
    job = t.new_job(ELM_ID)
    job = _confirm(t, job, [1, 2, 3], "original")
    by_hand = [_areas(job)[o]["id"] for o in (1, 2, 3)]
    stamped = _confirmed_at(t, by_hand)
    assert all(stamped.values())
    seen = t.audit_rows()
    out = _confirm_all(t, job["id"])
    assert (out["confirmed"], out["skipped"]) == (25, 0)
    assert out["message"] == "25 work areas confirmed as suggested."
    rows = t.audit_actions(seen)
    assert len(rows) == 25
    assert sorted(r.detail["rows"]["order_no"] for r in rows) == ORIGINALS[3:] + CHANGE_ORDERS
    assert _confirmed_at(t, by_hand) == stamped
    assert out["revised_contract"] == "465469.59"


def test_the_first_fixture_alone_still_reads_as_f07_left_it(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    """The F06 file's job (20 kept) through the same action: the F07 figures."""
    t = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    job = t.new_job(ELM_ID)
    out = _confirm_all(t, job["id"])
    assert (out["confirmed"], out["revised_contract"], out["unapproved_change_orders"]) == (
        20,
        "465469.59",
        "9660.09",
    )
    assert out["eac_in_basis"] == "293017.70"


# --- the selector (pure) -------------------------------------------------------------------


def test_as_suggested_takes_kept_unconfirmed_rows_with_a_suggestion_and_counts_the_rest() -> None:
    rows = [
        AreaChoice(1, True, None, "original"),
        AreaChoice(2, True, "original", "original"),  # already confirmed: untouched
        AreaChoice(3, False, None, None),  # omitted: never
        AreaChoice(4, True, None, None),  # kept, no suggestion: left to confirm
        AreaChoice(5, True, None, "change_order"),
    ]
    chosen, skipped = as_suggested(rows)
    assert [(c.order_no, c.suggestion) for c in chosen] == [(1, "original"), (5, "change_order")]
    assert skipped == 1
    assert as_suggested([]) == ([], 0)
