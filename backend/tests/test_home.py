"""F07.3: Home says what to do next. The pure checklist and job-need order
(``app/domain/home/checklist.py``), then ``GET /api/home`` on a fresh tenant and at each
step of taking a company from nothing to a linked job: the Rye Beach rules and chart,
policy and burden (the owner's answers to Plan questions 2 and 3), the sixteen sold
estimates, the EST6115758 job (both files), D-35, D-37, roles and isolation. Every
count on Home is compared to the page it links to."""

import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.db import tenant_session
from app.domain.config.burden import add_burden_rate
from app.domain.home.checklist import (
    REQUIRED_POLICY_KEYS,
    Item,
    JobFacts,
    Link,
    SetupFacts,
    job_lines,
    next_need,
    review_item,
    setup_lines,
    tracked_items,
)
from tests.config_helpers import CHART_CSV, load_rye_beach_rules, run_until_quiet, upload_chart
from tests.conftest import CSRF, Seed
from tests.estimate_helpers import D04_BASIS, configure_tenant
from tests.job_helpers import D05_BASIS, ELM_ID, Tenant, actor, make_tenant, policy

ORDER = [
    "quickbooks",
    "suggestion_rules",
    "chart_of_accounts",
    "account_mapping",
    "policy",
    "burden_rates",
]
BACKLOG = "Backlog: sold, no money moved yet (D-35). Nothing needed."
KEYS = (
    "timezone",
    "fiscal_year_start_month",
    "wip_basis",
    "small_job_threshold",
    "deposit_identification",
    "fuel_surcharge_treatment",
    "change_order_evidence",
)
LABELS = {
    "timezone": "Time zone",
    "wip_basis": "WIP basis",
    "deposit_identification": "Deposit identification",
    "fuel_surcharge_treatment": "Fuel surcharge treatment",
    "change_order_evidence": "Change order evidence",
}
# F08: the four keys read today (owner's go-ahead, 2026-10-06: these seven assertions
# moved from two keys to four; nothing else in this file changed). F07.4 (D-42): five,
# with the change order evidence key; the same assertions, nothing else.
FOUR = (
    "5 of 5 policy keys needed now are not decided: Time zone, WIP basis, "
    "Deposit identification, Fuel surcharge treatment, Change order evidence."
)
THREE = (
    "4 of 5 policy keys needed now are not decided: WIP basis, Deposit identification, "
    "Fuel surcharge treatment, Change order evidence."
)


def _facts(**over) -> SetupFacts:
    base = dict(
        connection_status=None,
        attention=(),
        active_rules=0,
        active_accounts=0,
        unmapped_accounts=0,
        suggested_accounts=0,
        all_policy_keys=KEYS,
        decided_keys=frozenset(),
        policy_labels=LABELS,
        wip_basis=None,
        divisions_with_digit=(),
        divisions_without_rate=(),
    )
    base.update(over)
    return SetupFacts(**base)


def _job(**over) -> JobFacts:
    base = dict(
        id="j",
        name="J",
        status="sold",
        status_label="Sold",
        revenue_method="fixed_price",
        to_confirm=0,
        qbo_linked=False,
        needs_link=False,
    )
    base.update(over)
    return JobFacts(**base)


# --- the pure parts ------------------------------------------------------------------------------


def test_a_fresh_company_has_six_not_done_lines_in_order_and_one_primary_action() -> None:
    lines = setup_lines(_facts())
    assert [line.code for line in lines] == ORDER
    assert all(not line.done for line in lines)
    assert [line.primary for line in lines] == [True, False, False, False, False, False]
    assert lines[0].message == "Connect to QuickBooks." and lines[0].link == Link("connections")
    assert lines[1].link is None and "no screen" in lines[1].message
    assert lines[2].message == "Upload the chart of accounts." and lines[2].link == Link("imports")
    assert lines[3].message == "Account mapping waits for the chart of accounts."
    assert lines[4].message == FOUR
    assert lines[5].message == "Burden rates are set after the WIP basis is decided."
    assert lines[5].link == Link("config", "policy")
    assert setup_lines(_facts(can_view_config=False)) is None
    without = setup_lines(_facts(can_view_connections=False))
    assert [line.code for line in without] == ORDER[1:] and without[
        1
    ].primary  # the chart line acts first


def test_the_quickbooks_line_follows_the_connection_and_its_attention() -> None:
    reconnect = (
        ("needs_reconnect", "QuickBooks stopped accepting this connection. Reconnect it."),
    )
    assert (
        setup_lines(_facts(connection_status="needs_reconnect", attention=reconnect))[0].message
        == reconnect[0][1]
    )
    backfill = (("backfill_needed", "No backfill has completed for this company."),)
    line = setup_lines(_facts(connection_status="connected", attention=backfill))[0]
    assert (
        line.message == "No backfill has completed for this company. Run a backfill on Connections."
    )
    done = setup_lines(_facts(connection_status="connected"))[0]
    assert done.done and done.message == "QuickBooks is connected and a backfill has succeeded."


def test_policy_needs_the_two_keys_read_today_and_then_notes_the_rest() -> None:
    assert REQUIRED_POLICY_KEYS == (
        "timezone",
        "wip_basis",
        "deposit_identification",
        "fuel_surcharge_treatment",
        "change_order_evidence",
    )
    one = setup_lines(_facts(decided_keys=frozenset({"timezone"})))[4]
    assert (one.done, one.message, one.count, one.total) == (False, THREE, 4, 5)
    both = setup_lines(
        _facts(decided_keys=frozenset(REQUIRED_POLICY_KEYS), wip_basis=frozenset(D05_BASIS))
    )[4]
    assert both.done and both.message == "The policy keys needed now are decided."
    assert both.note == "2 more keys are decided when their features arrive."
    every = setup_lines(_facts(decided_keys=frozenset(KEYS), wip_basis=frozenset(D05_BASIS)))[4]
    assert every.done and every.note is None


def test_burden_follows_the_basis_then_names_the_grid_divisions_without_a_rate() -> None:
    undecided = setup_lines(_facts())[5]
    assert (
        not undecided.done
        and undecided.message == "Burden rates are set after the WIP basis is decided."
    )
    no_burden = setup_lines(_facts(wip_basis=frozenset(D04_BASIS), divisions_with_digit=("LS",)))[5]
    assert no_burden.done and no_burden.note == "Labor Burden is not in the WIP basis."
    no_divisions = setup_lines(_facts(wip_basis=frozenset(D05_BASIS)))[5]
    assert not no_divisions.done and no_divisions.link == Link("config", "divisions")
    missing = setup_lines(
        _facts(
            wip_basis=frozenset(D05_BASIS),
            divisions_with_digit=("LS", "EX", "GC", "SNOW"),
            divisions_without_rate=("EX", "SNOW"),
        )
    )[5]
    assert (missing.done, missing.message, missing.count, missing.total) == (
        False,
        "No burden rate in force today for EX, SNOW.",
        2,
        4,
    )
    assert missing.link == Link("config", "burden")
    done = setup_lines(_facts(wip_basis=frozenset(D05_BASIS), divisions_with_digit=("LS",)))[5]
    assert done.done and done.message == "Every division has a burden rate in force today."


def test_the_account_mapping_line_counts_as_the_accounts_page_does() -> None:
    line = setup_lines(_facts(active_accounts=90, unmapped_accounts=90, suggested_accounts=0))[3]
    assert line.message == "90 of 90 accounts to confirm; 0 have a suggestion." and (
        line.count,
        line.total,
    ) == (90, 90)
    suggested = setup_lines(
        _facts(active_accounts=90, unmapped_accounts=90, suggested_accounts=64)
    )[3]
    assert suggested.message == "90 of 90 accounts to confirm; 64 have a suggestion."
    done = setup_lines(_facts(active_accounts=90, unmapped_accounts=0, suggested_accounts=0))[3]
    assert done.done and done.message == "Every active account is mapped."


def test_what_a_job_needs_next_in_order_and_which_jobs_are_listed() -> None:
    both = _job(to_confirm=3, needs_link=True, status="in_progress")
    assert next_need(both).message == "3 work areas to confirm." and next_need(both).count == 3
    assert next_need(_job(to_confirm=1)).message == "1 work area to confirm."
    link = next_need(_job(status="in_progress", needs_link=True))
    assert (link.code, link.message, link.link) == (
        "needs_link",
        "Needs its QuickBooks project: link it on the job.",
        Link("jobs", job_id="j"),
    )
    backlog = next_need(_job())
    assert (backlog.code, backlog.message, backlog.link) == (None, BACKLOG, None)
    assert next_need(_job(revenue_method="pool")).message == "Nothing needed."
    assert next_need(_job(revenue_method="recurring_service")).message == "Nothing needed."
    assert next_need(_job(qbo_linked=True, status="in_progress")).message == "Nothing needed."
    listed = job_lines(
        [
            _job(id="a"),
            _job(id="b", status="closed"),
            _job(id="c", status="cancelled"),
            _job(id="d", status="substantially_complete", qbo_linked=True),
        ]
    )
    assert [line.job.id for line in listed] == ["a", "d"]


def test_billing_on_unapproved_change_orders_shows_ahead_of_the_f07_4_needs() -> None:
    """F08.1 (D-45; the owner, 2026-10-07): a job that also has unapproved change orders
    says what has been billed on them before the plain count; the F08 billing needs still
    come first, and 0.00 raises nothing."""
    job = _job(
        qbo_linked=True,
        status="in_progress",
        unapproved_change_orders="53704.13",
        unapproved_count=12,
        billing_unapproved_co="49275.00",
        billing_unapproved_labels=tuple(f"#{n}" for n in (18, *range(22, 30))),
    )
    need = next_need(job)
    assert (need.code, need.message, need.link, need.count) == (
        "billing_unapproved_co",
        "49,275.00 billed on 9 change orders that are not approved: #18, #22, #23, #24, #25, "
        "#26, #27, #28, #29 (D-45).",
        Link("jobs", job_id="j"),
        9,
    )
    one = _job(
        qbo_linked=True,
        status="in_progress",
        billing_unapproved_co="5475.00",
        billing_unapproved_labels=("#1 of EST6120638",),
    )
    assert next_need(one).message == (
        "5,475.00 billed on 1 change order that is not approved: #1 of EST6120638 (D-45)."
    )
    ahead = _job(
        qbo_linked=True,
        status="in_progress",
        billed_over_contract="10.00",
        billing_unapproved_co="5475.00",
        billing_unapproved_labels=("#18",),
    )
    assert next_need(ahead).code == "billed_over_contract"
    quiet = _job(
        qbo_linked=True, status="in_progress", unapproved_count=12, unapproved_change_orders="1.00"
    )
    assert next_need(quiet).code == "change_orders_unapproved"


def test_the_review_item_and_the_tracked_rows_link_only_for_roles_that_can_open_them() -> None:
    assert review_item(0, 0, can_review=True, can_import=True) == Item(
        "no_estimates", "Upload an estimate.", Link("imports"), 0
    )
    assert review_item(0, 0, can_review=False, can_import=False).link is None
    assert review_item(16, 80, can_review=True, can_import=True) == Item(
        "to_review", "16 sold estimates to review.", Link("jobs", review=True), 16
    )
    assert review_item(1, 80, can_review=False, can_import=True) == Item(
        "to_review", "1 sold estimate to review.", None, 1
    )
    assert review_item(0, 80, can_review=True, can_import=True) is None
    assert tracked_items(["x"], can_track=True) == [
        Item("LEDGER_PROJECT_NO_JOB", "x", Link("customers"))
    ]
    assert tracked_items(["x"], can_track=False)[0].link is None


# --- the endpoint --------------------------------------------------------------------------


@pytest.fixture
def admin(login_as: Callable[..., TestClient], fresh_tenant: uuid.UUID) -> TestClient:
    return login_as("rotate_me", tenant=fresh_tenant)


def _home(client: TestClient) -> dict:
    r = client.get("/api/home")
    assert r.status_code == 200, r.text
    return r.json()


def _get(client: TestClient, path: str) -> dict:
    r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def _line(home: dict, code: str) -> dict:
    return next(line for line in home["setup"] if line["code"] == code)


def test_a_fresh_tenant_reads_six_not_done_lines_and_upload_an_estimate(
    admin: TestClient, fresh_tenant, rw_engine
) -> None:
    home = _home(admin)
    assert [line["code"] for line in home["setup"]] == ORDER
    assert all(line["done"] is False for line in home["setup"])
    assert [line["code"] for line in home["setup"] if line["primary"]] == ["quickbooks"]
    assert home["review"] == {
        "code": "no_estimates",
        "message": "Upload an estimate.",
        "count": 0,
        "link": {"page": "imports", "section": None, "job_id": None, "review": False},
    }
    assert home["jobs"] == [] and home["tracked_without_job"] == []
    # pairwise with the pages Home links to
    assert _get(admin, "/api/qbo/status")["status"] == "disconnected"
    assert all(p["decided"] is False for p in _get(admin, "/api/config/policy"))
    assert _get(admin, "/api/estimates")["total"] == 0
    assert _get(admin, "/api/jobs")["to_review"] == 0

    # QuickBooks: connected without a backfill, then backfilled (F05 fixtures).
    from tests.qbo_helpers import FakeIntuit, FixtureCompany, connect_directly, installed
    from tests.test_qbo_tasks import _drain

    fake = FakeIntuit()
    FixtureCompany().serve(fake)
    with installed(fake):
        connect_directly(rw_engine, fresh_tenant, fake)
        line = _line(_home(admin), "quickbooks")
        detail = next(
            a
            for a in _get(admin, "/api/qbo/status")["held"]["attention"]
            if a["code"] == "backfill_needed"
        )["detail"]
        assert (
            line["done"] is False and line["message"] == f"{detail} Run a backfill on Connections."
        )
        assert admin.post("/api/qbo/backfill", headers=CSRF).status_code == 200
        _drain(rw_engine, fresh_tenant)
        line = _line(_home(admin), "quickbooks")
    assert (
        line["done"] is True
        and line["message"] == "QuickBooks is connected and a backfill has succeeded."
    )
    assert [line["code"] for line in _home(admin)["setup"] if line["primary"]] == [
        "chart_of_accounts"
    ]  # rules have no action


def _mapping_matches_accounts_page(client: TestClient) -> tuple[dict, dict]:
    line = _line(_home(client), "account_mapping")
    accounts = _get(client, "/api/config/accounts")
    assert (line["count"], line["total"]) == (accounts["unmapped_count"], accounts["total_active"])
    assert (
        line["message"].endswith(f"{accounts['suggested_count']} have a suggestion.")
        or line["done"]
    )
    return line, accounts


def test_rules_then_chart_then_confirm_all(admin: TestClient, fresh_tenant, rw_engine) -> None:
    load_rye_beach_rules(rw_engine, fresh_tenant)
    upload_chart(admin, CHART_CSV.read_bytes())
    run_until_quiet(rw_engine)
    home = _home(admin)
    assert _line(home, "suggestion_rules")["done"] and _line(home, "chart_of_accounts")["done"]
    line, accounts = _mapping_matches_accounts_page(admin)
    assert (
        line["done"] is False
        and accounts["suggested_count"] == accounts["total_active"]
        and accounts["confirmed_count"] == 0
    )
    assert line["primary"] is False and _line(home, "quickbooks")["primary"] is True
    assert admin.post("/api/config/accounts/confirm-all", headers=CSRF).status_code == 200
    line, accounts = _mapping_matches_accounts_page(admin)
    assert line["done"] is True and accounts["unmapped_count"] == 0


def test_chart_before_rules_then_rules_and_suggestions_change_both_lines(
    admin: TestClient, fresh_tenant, rw_engine
) -> None:
    upload_chart(admin, CHART_CSV.read_bytes())
    run_until_quiet(rw_engine)
    home = _home(admin)
    rules, mapping = _line(home, "suggestion_rules"), _line(home, "account_mapping")
    assert rules["done"] is False and rules["count"] == 0
    accounts = _get(admin, "/api/config/accounts")
    assert (
        mapping["count"] == mapping["total"] == accounts["total_active"]
        and accounts["suggested_count"] == 0
    )
    assert mapping["message"].endswith("0 have a suggestion.")
    load_rye_beach_rules(rw_engine, fresh_tenant)
    assert admin.post("/api/config/accounts/suggest", headers=CSRF).status_code == 200
    home = _home(admin)
    assert _line(home, "suggestion_rules")["done"] is True
    after, accounts = _mapping_matches_accounts_page(admin)
    assert (
        after["message"] != mapping["message"]
        and accounts["suggested_count"] == accounts["total_active"]
    )
    assert (
        after["count"] == mapping["count"]
    )  # still to confirm: a suggestion is not a confirmation


def test_policy_and_burden_lines_follow_the_owners_answers(
    admin: TestClient, seed: Seed, fresh_tenant, rw_engine
) -> None:
    configure_tenant(
        rw_engine, seed, fresh_tenant, basis=False
    )  # the grid: divisions with digits, no rates
    home = _home(admin)
    assert _line(home, "policy")["message"] == FOUR
    assert (
        _line(home, "burden_rates")["message"]
        == "Burden rates are set after the WIP basis is decided."
    )
    policy(rw_engine, seed, fresh_tenant, "timezone", "America/New_York")
    assert _line(_home(admin), "policy")["message"] == THREE
    policy(rw_engine, seed, fresh_tenant, "wip_basis", D04_BASIS)  # no Labor Burden
    from tests.billing_helpers import billing_policy

    billing_policy(rw_engine, seed, fresh_tenant)  # F08: the two keys the board reads
    policy(rw_engine, seed, fresh_tenant, "change_order_evidence", "none")  # F07.4 (D-42)
    home = _home(admin)
    pol, bur = _line(home, "policy"), _line(home, "burden_rates")
    assert pol["done"] and pol["note"] == "2 more keys are decided when their features arrive."
    assert sum(p["decided"] for p in _get(admin, "/api/config/policy")) == 5
    assert bur["done"] and bur["note"] == "Labor Burden is not in the WIP basis."
    policy(rw_engine, seed, fresh_tenant, "wip_basis", D05_BASIS)
    bur = _line(_home(admin), "burden_rates")
    grid = [d for d in _get(admin, "/api/config/divisions") if d["active"] and d["code_digit"]]
    codes = [d["code"] for d in grid]
    assert bur["done"] is False and (bur["count"], bur["total"]) == (len(codes), len(codes))
    assert bur["message"] == f"No burden rate in force today for {', '.join(codes)}."
    ex = next(d for d in grid if d["code"] == "EX")
    with tenant_session(rw_engine, fresh_tenant) as s:
        add_burden_rate(
            s,
            fresh_tenant,
            division_id=uuid.UUID(ex["id"]),
            effective_from=date(2026, 1, 1),
            effective_to=None,
            rate=Decimal("0.1959"),
            basis_note="D-34 (test)",
            actor=actor(seed),
        )
    bur = _line(_home(admin), "burden_rates")
    assert "EX" not in bur["message"] and bur["count"] == len(codes) - 1
    with tenant_session(rw_engine, fresh_tenant) as s:
        for d in grid:
            if d["code"] != "EX":
                add_burden_rate(
                    s,
                    fresh_tenant,
                    division_id=uuid.UUID(d["id"]),
                    effective_from=date(2026, 1, 1),
                    effective_to=None,
                    rate=Decimal("0.2"),
                    basis_note="test",
                    actor=actor(seed),
                )
    bur = _line(_home(admin), "burden_rates")
    assert bur["done"] and bur["message"] == "Every division has a burden rate in force today."
    rated = {r["division_code"] for r in _get(admin, "/api/config/burden-rates") if r["active"]}
    assert rated >= set(codes)


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    return make_tenant(seed, rw_engine, login_as, fresh_tenant, load=False)


def test_sixteen_sold_estimates_to_review(
    seed: Seed, rw_engine: Engine, login_as, fresh_tenant
) -> None:
    # Production order (F07): the two template files, then the 80-row sheet: fifteen
    # sold on the sheet plus the Turley Hess template (test_job_review).
    t = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    review = _home(t.client)["review"]
    to_review = t.get("/api/jobs")["to_review"]
    assert review["count"] == to_review == 16
    assert review["message"] == "16 sold estimates to review."
    assert review["link"] == {"page": "jobs", "section": None, "job_id": None, "review": True}


def test_the_job_path_d35_and_d37(seed: Seed, rw_engine: Engine, login_as, fresh_tenant) -> None:
    t = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    job = t.new_job(ELM_ID)

    def mine() -> dict:
        return next(j for j in _home(t.client)["jobs"] if j["id"] == job["id"])

    line = mine()
    row = next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job["id"])
    assert (line["code"], line["message"], line["count"]) == (
        "to_confirm",
        "28 work areas to confirm.",
        28,
    )
    assert line["count"] == row["to_confirm"] and line["link"]["job_id"] == job["id"]
    t.send("POST", f"/api/jobs/{job['id']}/work-areas/kinds/confirm-suggested")
    line = mine()
    assert (line["code"], line["message"], line["link"], line["status"]) == (
        None,
        BACKLOG,
        None,
        "sold",
    )
    t.send("PATCH", f"/api/jobs/{job['id']}", {"status": "in_progress"})
    line = mine()
    assert (line["code"], line["message"]) == (
        "needs_link",
        "Needs its QuickBooks project: link it on the job.",
    )
    assert "JOB_NO_LEDGER_LINK" in {i["code"] for i in t.job(job["id"])["attention"]}
    t.link(job["id"], "elm")
    line = mine()
    # F07.4 (D-42): the linked job's next need is its unapproved change orders (the
    # reviewed workbook's twelve, 53,704.13); before F07.4 this line read "Nothing needed."
    assert (line["code"], line["message"], line["status_label"]) == (
        "change_orders_unapproved",
        "12 change orders, 53,704.13, not approved.",
        "In progress",
    )
    assert _home(t.client)["review"]["count"] == t.get("/api/jobs")["to_review"] > 0

    # D-37: a tracked row with no job, as the Jobs page words it; untracked rows never appear.
    assert _home(t.client)["tracked_without_job"] == []
    t.send("POST", f"/api/customers/{t.customers['ocean']['id']}/track")
    items = _home(t.client)["tracked_without_job"]
    assert [i["message"] for i in items] == [
        i["message"] for i in t.get("/api/jobs")["ledger_items"]
    ]
    assert "1701 Ocean Boulevard" in items[0]["message"] and items[0]["link"]["page"] == "customers"
    assert not any("Dunbarton" in i["message"] for i in items)

    # closed and cancelled jobs are not listed
    t.send("PATCH", f"/api/jobs/{job['id']}", {"status": "closed"})
    assert job["id"] not in {j["id"] for j in _home(t.client)["jobs"]}
    pool = t.send(
        "POST",
        "/api/jobs/pool",
        {"name": "Pool - Hydroseed", "division_id": str(t.divisions["LS"])},
        201,
    )
    assert (
        next(j for j in _home(t.client)["jobs"] if j["id"] == pool["id"])["message"]
        == "Nothing needed."
    )
    t.send("PATCH", f"/api/jobs/{pool['id']}", {"status": "cancelled"})
    assert pool["id"] not in {j["id"] for j in _home(t.client)["jobs"]}


def test_a_client_role_gets_no_checklist_and_no_link_it_cannot_open(
    login_as, seed: Seed, rw_engine
) -> None:
    pm = login_as("client_pm")
    home = _home(pm)
    assert home["setup"] is None
    if home["review"] is not None:
        assert home["review"]["link"] is None
    assert all(i["link"] is None for i in home["tracked_without_job"])
    assert all(j["link"] is None or j["link"]["page"] == "jobs" for j in home["jobs"])


def test_home_for_one_tenant_reads_nothing_of_another(t: Tenant, login_as) -> None:
    pool = t.send(
        "POST", "/api/jobs/pool", {"name": "Only here", "division_id": str(t.divisions["LS"])}, 201
    )
    t.send("POST", f"/api/customers/{t.customers['ocean']['id']}/track")
    other = _home(login_as("rotate_me"))  # tenant A
    assert pool["id"] not in {j["id"] for j in other["jobs"]}
    assert not any("1701 Ocean Boulevard" in i["message"] for i in other["tracked_without_job"])
    mine = _home(t.client)
    assert pool["id"] in {j["id"] for j in mine["jobs"]} and len(mine["tracked_without_job"]) == 1
