"""F07: linking a job to QuickBooks rows by id (§5, §13.2; owner's answers 3, 11, 15,
20, 21), the review items on the crosswalk, pool jobs (D-30) and the read-only
customer duplicates list (§10 CUSTOMER_FUZZY; answers 13, 22). The customers are
synthetic rows built in the test. Nothing is linked except by a request naming an
id, and nothing is written to QuickBooks (there is no client for it here)."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.core.db import tenant_session
from app.domain.jobs.models import JobAlias
from tests.conftest import CSRF, Seed
from tests.job_helpers import (
    DEVELLIS_ID,
    ELM_ID,
    TURLEY_HESS_ID,
    TURLEY_ID,
    Tenant,
    add_payment,
    make_tenant,
    seed_customers,
)


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    return make_tenant(seed, rw_engine, login_as, fresh_tenant)


def _suggested(t: Tenant, job_id: str) -> list[tuple[str, str]]:
    rows = t.get(f"/api/jobs/{job_id}/qbo-candidates")["rows"]
    return [(r["display_name"], r["reason"]) for r in rows]


def _qbo_aliases(t: Tenant) -> int:
    with tenant_session(t.engine, t.id) as s:
        return s.execute(
            select(func.count()).select_from(JobAlias).where(JobAlias.system == "qbo_customer")
        ).scalar_one()


def test_candidates_per_rule_and_never_an_inactive_pool_or_linked_row(t: Tenant) -> None:
    elm = t.new_job(ELM_ID)
    turley = t.new_job(TURLEY_ID)
    hess = t.new_job(TURLEY_HESS_ID, "LS")
    ocean = t.new_job(DEVELLIS_ID, "LS")
    seen = t.audit_rows()
    assert _suggested(t, elm["id"]) == [
        ("6115758 Client 05 - 67 Elm St Parking Lot", "estimate id in name")
    ]
    assert _suggested(t, turley["id"]) == [
        ("EST6120638 Client 26 - Landscape Projects 2026", "estimate id in name"),
        ("Client 26 - 378 E Dunbarton Rd", "customer name"),
        ("Old", "customer name"),
    ]
    assert _suggested(t, hess["id"]) == [
        ("Client 26 - 378 E Dunbarton Rd", "customer name and address"),
        ("EST6120638 Client 26 - Landscape Projects 2026", "customer name"),
        ("Old", "customer name"),
    ]
    assert _suggested(t, ocean["id"]) == [("1701 Ocean Boulevard", "customer name and address")]
    for job in (elm, turley, hess, ocean):
        names = {n for n, _ in _suggested(t, job["id"])}
        assert "Pool - Hydroseed" not in names
        assert "6115758 Client 05 - Parking Lot (old)" not in names
    assert t.audit_rows() == seen and _qbo_aliases(t) == 0  # reads link nothing
    linked = t.link(turley["id"], "turley")
    assert linked["customer_name"] == "Client 26"  # the project's parent, by id
    assert "EST6120638 Client 26 - Landscape Projects 2026" not in {
        n for n, _ in _suggested(t, hess["id"])
    }
    search = t.get(f"/api/jobs/{hess['id']}/qbo-search?q=est6120638")["rows"]
    assert search == []  # linked rows are never offered, by search either


def test_link_sets_the_paying_customer_and_unlink_offers_the_row_again(t: Tenant) -> None:
    elm = t.new_job(ELM_ID)
    seen = t.audit_rows()
    linked = t.link(elm["id"], "elm")
    (row,) = t.audit_actions(seen)
    assert row.action == "job_alias_linked"
    assert row.detail["after"]["external_id"] == t.customers["elm"]["external_id"]
    assert row.detail["rows"]["job.customer_id"] == [None, str(t.customers["c05"]["id"])]
    assert linked["customer_name"] == "Client 05" and linked["qbo_linked"] is True
    assert "JOB_NO_LEDGER_LINK" not in [i["code"] for i in linked["attention"]]
    (alias,) = [a for a in linked["aliases"] if a["system"] == "qbo_customer"]
    assert (alias["display_name"], alias["kind_label"]) == (
        "6115758 Client 05 - 67 Elm St Parking Lot",
        "Project",
    )
    # The same row from another job: 409, one sentence, nothing written.
    other = t.new_job(TURLEY_ID)
    seen = t.audit_rows()
    r = t.client.post(
        f"/api/jobs/{other['id']}/aliases",
        json={"system": "qbo_customer", "external_id": t.customers["elm"]["external_id"]},
        headers=CSRF,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == (
        f'"6115758 Client 05 - 67 Elm St Parking Lot" is already linked to job '
        f'"{elm["name"]}"; unlink it there first.'
    )
    assert t.audit_rows() == seen
    # A second QuickBooks row on the same job is allowed (answer 15).
    two = t.link(elm["id"], "inactive", status=422)
    assert "inactive" in two["detail"]
    two = t.link(elm["id"], "old")
    assert len(two["qbo_names"]) == 2 and two["customer_name"] == "Client 05"
    seen = t.audit_rows()
    after = t.send("DELETE", f"/api/jobs/{elm['id']}/aliases/{alias['id']}")
    (row,) = t.audit_actions(seen)
    assert row.action == "job_alias_unlinked"
    assert row.detail["before"]["external_id"] == t.customers["elm"]["external_id"]
    assert after["customer_name"] == "Client 05"  # one QuickBooks row still linked
    assert ("6115758 Client 05 - 67 Elm St Parking Lot", "estimate id in name") in _suggested(
        t, elm["id"]
    )
    old = next(a for a in after["aliases"] if a["system"] == "qbo_customer")
    last = t.send("DELETE", f"/api/jobs/{elm['id']}/aliases/{old['id']}")
    assert last["customer_name"] is None and last["qbo_linked"] is False
    # An estimate id is not unlinked here: it leaves with the estimate.
    lmn = next(a for a in last["aliases"] if a["system"] == "lmn_estimate")
    r = t.client.delete(f"/api/jobs/{elm['id']}/aliases/{lmn['id']}", headers=CSRF)
    assert r.status_code == 422
    r = t.client.post(
        f"/api/jobs/{elm['id']}/aliases",
        json={"system": "lmn_estimate", "external_id": "EST1"},
        headers=CSRF,
    )
    assert r.status_code == 422


def test_review_items_on_the_crosswalk(t: Tenant) -> None:
    ocean_items = [
        i for i in t.get("/api/jobs")["ledger_items"] if i["code"] == "LEDGER_PROJECT_NO_JOB"
    ]
    assert [("1701 Ocean Boulevard" in i["message"]) for i in ocean_items] == [True]
    add_payment(t.engine, t.id, t.customers["old"])  # a sub-customer with activity
    items = t.get("/api/jobs")["ledger_items"]
    assert len(items) == 2 and any('sub-customer "Old"' in i["message"] for i in items)
    job = t.new_job(DEVELLIS_ID, "LS")
    assert [i["code"] for i in job["attention"]] == ["JOB_NO_LEDGER_LINK"]
    t.link(job["id"], "ocean")
    assert t.job(job["id"])["attention"] == []
    items = t.get("/api/jobs")["ledger_items"]
    assert [('"Old"' in i["message"]) for i in items] == [True]
    unlinked = t.get("/api/jobs?no_link=true")["jobs"]
    assert job["id"] not in {j["id"] for j in unlinked}


def test_pool_job_is_found_by_search_and_never_takes_an_estimate(t: Tenant) -> None:
    seen = t.audit_rows()
    pool = t.send(
        "POST",
        "/api/jobs/pool",
        {"name": "Pool - Hydroseed", "division_id": str(t.divisions["LS"])},
        status=201,
    )
    (row,) = t.audit_actions(seen)
    assert row.action == "job_created" and row.detail["after"]["revenue_method"] == "pool"
    assert pool["revenue_method_label"] == "Pool" and pool["estimates"] == []
    assert (pool["revised_contract"], pool["unapproved_change_orders"], pool["eac_in_basis"]) == (
        None,
        None,
        None,
    )
    assert pool["sold_on_set_when_created"] is True
    assert [i["code"] for i in pool["attention"]] == ["JOB_NO_LEDGER_LINK"]
    assert _suggested(t, pool["id"]) == []
    found = t.get(f"/api/jobs/{pool['id']}/qbo-search?q=pool")["rows"]
    assert [(r["display_name"], r["kind_label"]) for r in found] == [
        ("Pool - Hydroseed", "Customer"),
        ("Pool - Hydroseed", "Project"),
    ]
    assert "Parking Lot (old)" not in str(t.get(f"/api/jobs/{pool['id']}/qbo-search?q=6115758"))
    linked = t.link(pool["id"], "pool")
    assert linked["attention"] == [] and linked["customer_name"] == "Pool - Hydroseed"
    elm = t.queue()[ELM_ID]
    assert all(c["job_id"] != pool["id"] for e in t.queue().values() for c in e["candidates"])
    r = t.client.post(
        f"/api/jobs/{pool['id']}/estimates",
        json={"estimate_id": elm["estimate_id"], "role": "change_order"},
        headers=CSRF,
    )
    assert r.status_code == 422 and "pool" in r.json()["detail"].lower()
    assert len(t.queue()) == 16


def test_customer_duplicates_are_listed_read_only(
    t: Tenant, login_as: Callable[..., TestClient]
) -> None:
    extra = seed_customers(
        t.engine,
        t.id,
        {
            "pair_a": ("301", "Client 31, Pat & Sam", None, False, True),
            "pair_b": ("302", "Client 31 Pat and Sam", None, False, True),
        },
    )
    add_payment(t.engine, t.id, extra["pair_a"])
    seen = t.audit_rows()
    pairs = t.get("/api/customers/duplicates")["pairs"]
    assert t.audit_rows() == seen
    assert len(pairs) == 1
    (p,) = pairs
    assert p["code"] == "CUSTOMER_FUZZY" and "CUSTOMER_FUZZY" not in p["message"]
    assert p["message"].endswith("Merge in QuickBooks; the platform follows.")
    sides = {p["a"]["external_id"]: p["a"], p["b"]["external_id"]: p["b"]}
    assert set(sides) == {"301", "302"}
    assert (sides["301"]["payment_count"], sides["302"]["payment_count"]) == (1, 0)
    for side in sides.values():
        assert side["billing_count"] == 0 and side["customer_id"]
    viewer = login_as("client_viewer")
    assert viewer.get("/api/customers/duplicates").status_code == 403
