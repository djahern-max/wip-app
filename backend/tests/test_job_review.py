"""F07: the review queue and the D-03 decisions over the Rye Beach fixtures, loaded in
production order. Nothing is attached or created except by a request naming an id;
every action writes exactly one audit row and reads write none."""

from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.core.db import tenant_session
from app.domain.jobs.models import Job, JobAlias, JobEstimate
from tests.config_helpers import run_until_quiet
from tests.conftest import Seed
from tests.estimate_helpers import EIGHTY, build_workbook, fixture_rows, upload_template
from tests.job_helpers import (
    DEVELLIS_ADDON_ID,
    DEVELLIS_ID,
    ELM_ID,
    MONEY,
    TURLEY_HESS_ID,
    TURLEY_ID,
    Tenant,
    make_tenant,
    money_values,
)

SOLD_ON_SHEET = 15  # the 80-row sheet's sold rows with an id (Mijal has none)


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    return make_tenant(seed, rw_engine, login_as, fresh_tenant)


def _counts(t: Tenant) -> dict[str, int]:
    with tenant_session(t.engine, t.id) as s:
        return {
            name: s.execute(select(func.count()).select_from(model)).scalar_one()
            for name, model in (
                ("job", Job),
                ("job_estimate", JobEstimate),
                ("job_alias", JobAlias),
            )
        }


def _sheet_copy(**status_by_id: str) -> bytes:
    rows = fixture_rows(EIGHTY)["Estimates"]
    for r in rows:
        if r["estimate_id"] in status_by_id:
            r["status"] = status_by_id[r["estimate_id"]]
    return build_workbook(estimates=rows)


def test_the_queue_is_the_sixteen_sold_estimates_and_reviewing_all_as_new_jobs_empties_it(
    t: Tenant,
) -> None:
    queue = t.queue()
    assert len(queue) == SOLD_ON_SHEET + 1 == 16
    assert ELM_ID in queue and TURLEY_ID in queue and TURLEY_HESS_ID in queue
    assert DEVELLIS_ADDON_ID not in queue  # pending
    statuses = {e["external_id"]: e["status_label"] for e in t.get("/api/estimates")["estimates"]}
    assert all(statuses[i] == "Sold" for i in queue)
    for e in queue.values():
        assert [i["code"] for i in e["attention"]][0] == "EST_UNATTACHED"
        assert "EST_UNATTACHED" not in e["attention"][0]["message"]
    before = t.audit_rows()
    for external_id, entry in sorted(queue.items()):
        t.new_job(external_id, None if entry["division_suggestion_id"] else "LS")
    assert t.audit_rows() == before + 16  # one row per New job
    assert t.queue() == {}
    assert _counts(t) == {"job": 16, "job_estimate": 16, "job_alias": 16}
    jobs = t.get("/api/jobs")
    assert jobs["total"] == 16 and jobs["to_review"] == 0
    # Turley is two jobs (D-03), each with its own estimate alias.
    with tenant_session(t.engine, t.id) as s:
        aliases = set(
            s.execute(
                select(JobAlias.external_id).where(JobAlias.system == "lmn_estimate")
            ).scalars()
        )
        roles = set(s.execute(select(JobEstimate.role)).scalars())
    assert {TURLEY_ID, TURLEY_HESS_ID, ELM_ID} <= aliases and len(aliases) == 16
    assert roles == {"original"}
    for body in (jobs, t.get(f"/api/jobs/{jobs['jobs'][0]['id']}")):
        for where, value in money_values(body):
            assert value is None or (isinstance(value, str) and MONEY.match(value)), where


def test_d03_same_customer_is_a_suggestion_and_new_job_clears_it(t: Tenant) -> None:
    turley = t.new_job(TURLEY_ID)
    t.link(turley["id"], "turley")
    counts = _counts(t)
    reads = t.audit_rows()
    entry = t.queue()[TURLEY_HESS_ID]
    assert t.audit_rows() == reads  # a read writes nothing
    assert _counts(t) == counts  # nothing attached without the choice
    (candidate,) = entry["candidates"]
    assert candidate["job_id"] == turley["id"]
    assert "by client name only" in candidate["reasons"] and candidate["same_customer"]
    codes = [i["code"] for i in entry["attention"]]
    assert codes == ["EST_UNATTACHED", "JOB_SECOND_ESTIMATE_FOR_CUSTOMER"]
    assert "Landscape Projects 2026" in entry["attention"][1]["message"]
    second = t.new_job(TURLEY_HESS_ID, "LS")
    assert second["id"] != turley["id"]
    assert TURLEY_HESS_ID not in t.queue()
    assert _counts(t)["job"] == counts["job"] + 1


def test_devellis_add_on_attaches_as_a_change_order_only_when_sold_and_chosen(t: Tenant) -> None:
    job = t.new_job(DEVELLIS_ID, "LS")
    assert job["revised_contract"] == "39032.44"
    assert job["revised_contract_note"] == "No work areas loaded: the original estimate's price."
    assert DEVELLIS_ADDON_ID not in t.queue()
    upload_template(t.client, _sheet_copy(**{DEVELLIS_ADDON_ID: "Sold"}), "sold_copy.xlsx")
    run_until_quiet(t.engine)
    entry = t.queue()[DEVELLIS_ADDON_ID]
    assert [(c["job_id"], c["reasons"]) for c in entry["candidates"]] == [
        (job["id"], ["by name only"])
    ]
    assert "JOB_SECOND_ESTIMATE_FOR_CUSTOMER" not in [i["code"] for i in entry["attention"]]
    counts, seen = _counts(t), t.audit_rows()
    after = t.send(
        "POST",
        f"/api/jobs/{job['id']}/estimates",
        {"estimate_id": entry["estimate_id"], "role": "change_order"},
    )
    assert _counts(t) == {
        **counts,
        "job_estimate": counts["job_estimate"] + 1,
        "job_alias": counts["job_alias"] + 1,
    }
    (row,) = t.audit_actions(seen)
    assert row.action == "job_estimate_attached" and row.detail["after"]["role"] == "change_order"
    assert row.actor_user_id == t.seed.users["rotate_me"].id and row.entity_id == job["id"]
    assert row.detail["after"]["estimate"] == DEVELLIS_ADDON_ID
    assert row.detail["rows"]["job_alias"][0]["external_id"] == DEVELLIS_ADDON_ID
    assert after["unapproved_change_orders"] == "998.71"
    assert after["revised_contract"] == "39032.44"
    assert [e["role_label"] for e in after["estimates"]] == ["Original", "Change order"]
    assert DEVELLIS_ADDON_ID not in t.queue()
    # Detaching the original is refused while the change order is attached.
    r = t.client.request(
        "DELETE",
        f"/api/jobs/{job['id']}/estimates/{after['estimates'][0]['estimate_id']}",
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 409 and r.json()["detail"].startswith("Detach the job's other")
    seen = t.audit_rows()
    back = t.send("DELETE", f"/api/jobs/{job['id']}/estimates/{entry['estimate_id']}")
    (row,) = t.audit_actions(seen)
    assert row.action == "job_estimate_detached" and row.detail["before"]["role"] == "change_order"
    assert back["unapproved_change_orders"] == "0.00"
    assert DEVELLIS_ADDON_ID in t.queue()
    assert _counts(t) == counts


def test_ignored_needs_a_note_and_moves_no_figure(t: Tenant) -> None:
    job = t.new_job(ELM_ID)
    target = t.queue()["EST6355355"]
    body = {"estimate_id": target["estimate_id"], "role": "ignored"}
    seen = t.audit_rows()
    r = t.client.post(
        f"/api/jobs/{job['id']}/estimates", json=body, headers={"X-Requested-With": "fetch"}
    )
    assert r.status_code == 422 and "reason" in r.json()["detail"]
    assert t.audit_rows() == seen
    before = t.job(job["id"])
    after = t.send(
        "POST",
        f"/api/jobs/{job['id']}/estimates",
        {**body, "note": "Sold in error; superseded by EST6115758."},
    )
    assert "EST6355355" not in t.queue()
    for key in ("revised_contract", "unapproved_change_orders", "eac_in_basis", "to_confirm"):
        assert after[key] == before[key], key
    assert after["estimates"][-1]["role_label"] == "Ignored"
    assert after["estimates"][-1]["eac_in_basis"] is None


def test_attach_rules_pool_original_and_a_second_job_for_one_estimate(t: Tenant) -> None:
    job = t.new_job(ELM_ID)
    other = t.queue()[DEVELLIS_ID]
    orig = {"estimate_id": other["estimate_id"], "role": "original"}
    r = t.client.post(
        f"/api/jobs/{job['id']}/estimates", json=orig, headers={"X-Requested-With": "fetch"}
    )
    assert r.status_code == 409 and "already has its original" in r.json()["detail"]
    # A job whose only (original) estimate was detached takes a new original.
    t.send("DELETE", f"/api/jobs/{job['id']}/estimates/{t.estimate_id(ELM_ID)}")
    assert t.job(job["id"])["revised_contract_note"] == "No original estimate on this job."
    fixed = t.send("POST", f"/api/jobs/{job['id']}/estimates", orig)
    assert fixed["estimates"][0]["external_id"] == DEVELLIS_ID
    # An estimate already on a job cannot become a second job.
    r = t.client.post(
        "/api/jobs",
        json={"estimate_id": other["estimate_id"], "division_id": str(t.divisions["LS"])},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 409 and "already on job" in r.json()["detail"]
    # A pending estimate is never put on a job.
    pending = t.estimate_id(DEVELLIS_ADDON_ID)
    r = t.client.post(
        "/api/jobs",
        json={"estimate_id": pending, "division_id": str(t.divisions["LS"])},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 422 and "Pending" in r.json()["detail"]


def test_division_suggestion_and_a_job_without_a_division_is_refused(t: Tenant) -> None:
    queue = t.queue()
    assert queue[ELM_ID]["division_suggestion_code"] == "EX"  # every labor line is 210
    assert queue[TURLEY_ID]["division_suggestion_code"] == "LS"  # all lines on 190
    assert queue[DEVELLIS_ID]["division_suggestion_code"] is None  # no cost lines
    seen = t.audit_rows()
    r = t.client.post(
        "/api/jobs",
        json={"estimate_id": queue[DEVELLIS_ID]["estimate_id"]},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 422 and r.json()["detail"] == "Choose the job's division."
    assert t.audit_rows() == seen and DEVELLIS_ID in t.queue()


def test_sold_on_is_the_estimate_date_or_the_creation_date(t: Tenant) -> None:
    job = t.new_job(ELM_ID)
    assert job["sold_on_set_when_created"] is True
    assert date.fromisoformat(job["sold_on"]) <= date.today()
    dated = build_workbook(
        estimates=[
            {
                "estimate_id": "EST9000001",
                "estimator": "Test",
                "client": "Client 90",
                "jobsite": "Site",
                "name": "Dated",
                "status": "Sold",
                "price": 100,
                "estimate_date": date(2026, 5, 4),
            }
        ]
    )
    upload_template(t.client, dated, "dated.xlsx")
    run_until_quiet(t.engine)
    job2 = t.new_job("EST9000001", "LS")
    assert (job2["sold_on"], job2["sold_on_set_when_created"]) == ("2026-05-04", False)


def test_patch_writes_one_row_with_the_changed_fields_and_the_estimate_shows_its_job(
    t: Tenant,
) -> None:
    detail = t.get(f"/api/estimates/{t.estimate_id(ELM_ID)}")
    assert detail["job"] is None and detail["to_review"] is True
    job = t.new_job(ELM_ID)
    detail = t.get(f"/api/estimates/{t.estimate_id(ELM_ID)}")
    assert detail["job"]["id"] == job["id"] and detail["job"]["role_label"] == "Original"
    assert detail["to_review"] is False
    pending = t.get(f"/api/estimates/{t.estimate_id(DEVELLIS_ADDON_ID)}")
    assert pending["job"] is None and pending["to_review"] is False
    seen = t.audit_rows()
    after = t.send(
        "PATCH",
        f"/api/jobs/{job['id']}",
        {"name": "67 Elm Street", "status": "in_progress", "revenue_method": "none"},
    )
    (row,) = t.audit_actions(seen)
    assert row.action == "job_updated"
    assert row.detail["changed_fields"] == ["name", "revenue_method", "status"]
    assert (
        row.detail["before"]["status"] == "sold" and row.detail["after"]["status"] == "in_progress"
    )
    assert (after["name"], after["status_label"], after["revenue_method_label"]) == (
        "67 Elm Street",
        "In progress",
        "None",
    )
    t.send("PATCH", f"/api/jobs/{job['id']}", {"name": "67 Elm Street"})
    assert t.audit_rows() == seen + 1  # nothing changed, no row
    r = t.client.patch(
        f"/api/jobs/{job['id']}",
        json={"revenue_method": "pool"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 422 and "detach" in r.json()["detail"]
    r = t.client.patch(
        f"/api/jobs/{job['id']}", json={"status": "done"}, headers={"X-Requested-With": "fetch"}
    )
    assert r.status_code == 422
