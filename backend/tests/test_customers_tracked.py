"""F07.2 (D-37): a person picks the QuickBooks customers and projects the platform works
on. The picker's search (active rows by name, projects first, paged), the Tracked
list, track and untrack by id with one audit row each, the link that tracks in the
same action, the narrowed ``LEDGER_PROJECT_NO_JOB`` (tracked, active rows only), the
sync writer leaving the flag alone, tenant isolation, and the figures that must not
move: the F07 contract figures and the month totals. Synthetic customers from
``tests/job_helpers.py``; 67 Elm Street from the two template fixtures."""

import uuid
from collections.abc import Callable
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from app.audit.models import AuditLog
from app.core.db import tenant_session, untenanted_session
from app.domain.billing.models import Customer
from app.domain.billing.sync import apply_raw
from app.ingest.raw import RawOrigin, store_raw
from app.tenancy.models import Membership
from app.tenancy.models import Tenant as TenantRow
from tests.conftest import CSRF, Seed
from tests.job_helpers import CUSTOMERS, ELM_ID, Tenant, make_tenant, seed_customers

OCEAN = "1701 Ocean Boulevard"
DUNBARTON_SENTENCE = (
    'QuickBooks project "Client 26 - 378 E Dunbarton Rd" is tracked and has no job. '
    "Link it to its job, or make the job first."
)


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    return make_tenant(seed, rw_engine, login_as, fresh_tenant, load=False)


@pytest.fixture
def elm(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    """67 Elm Street as production stands (the reviewed workbook of 2026-10-06)."""
    return make_tenant(seed, rw_engine, login_as, fresh_tenant)


def _ledger(t: Tenant) -> list[str]:
    items = t.get("/api/jobs")["ledger_items"]
    assert all(i["code"] == "LEDGER_PROJECT_NO_JOB" for i in items)
    return [i["message"] for i in items]


def _tracked(t: Tenant) -> list[dict]:
    return t.get("/api/customers/tracked")["rows"]


def _search(t: Tenant, q: str, page: int = 1) -> dict:
    return t.get(f"/api/customers?q={quote(q)}&page={page}")


def _track(t: Tenant, key: str, status: int = 200) -> dict:
    return t.send("POST", f"/api/customers/{t.customers[key]['id']}/track", None, status)


def _untrack(t: Tenant, key: str, status: int = 200) -> dict:
    return t.send("POST", f"/api/customers/{t.customers[key]['id']}/untrack", None, status)


def _pool_job(t: Tenant) -> dict:
    return t.send(
        "POST",
        "/api/jobs/pool",
        {"name": "Pool - Hydroseed", "division_id": str(t.divisions["LS"])},
        201,
    )


def _apply(t: Tenant, payload: dict) -> None:
    """One QuickBooks customer payload through the F05 writer, as a change poll would."""
    with tenant_session(t.engine, t.id) as db:
        run_id = db.execute(text("SELECT id FROM sync_run LIMIT 1")).scalar_one()
        stored = store_raw(
            db, t.id, "qbo", "Customer", payload["Id"], payload, RawOrigin(sync_run_id=run_id)
        )
        assert stored.record is not None
        apply_raw(db, t.id, stored.record)


def _flag(t: Tenant, key: str) -> tuple:
    with tenant_session(t.engine, t.id) as s:
        c = s.get(Customer, t.customers[key]["id"])
        return (c.display_name, c.active, c.tracked_at, c.tracked_by)


# --- the review item (D-37) -----------------------------------------------------------------


def test_nothing_tracked_raises_nothing_even_with_a_billing_row(t: Tenant) -> None:
    assert _search(t, "Ocean")["rows"][0]["billing_count"] == 1  # 1701 Ocean has a document
    assert _ledger(t) == []
    assert _tracked(t) == []


def test_tracking_writes_one_row_lists_the_row_and_raises_the_item_until_linked(t: Tenant) -> None:
    seen = t.audit_rows()
    _track(t, "ocean")
    rows = t.audit_actions(seen)
    assert [(r.action, r.entity_type, r.entity_id) for r in rows] == [
        ("customer_tracked", "customer", str(t.customers["ocean"]["id"]))
    ]
    after = rows[0].detail["after"]
    assert (after["external_id"], after["display_name"]) == ("204", OCEAN)
    assert after["tracked_by"] == str(t.seed.users["rotate_me"].id) and after["tracked_at"]

    listed = _tracked(t)
    assert [(r["display_name"], r["kind_label"], r["tracked"], r["job"]) for r in listed] == [
        (OCEAN, "Project", True, None)
    ]
    sentence = (
        f'QuickBooks project "{OCEAN}" is tracked and has no job (1 billing or payment '
        "document). Link it to its job, or make the job first."
    )
    assert listed[0]["needs_job"] == sentence
    assert _ledger(t) == [sentence]

    # A tracked row with no documents raises it too (D-37), in words without a count.
    _track(t, "dunbarton")
    assert _ledger(t) == [
        sentence,
        DUNBARTON_SENTENCE,
    ]
    # Linking clears the item; the Tracked list shows the job.
    job = _pool_job(t)
    t.link(job["id"], "ocean")
    assert _ledger(t) == [DUNBARTON_SENTENCE]
    ocean = next(r for r in _tracked(t) if r["external_id"] == "204")
    assert (
        ocean["job"] == {"id": job["id"], "name": "Pool - Hydroseed"} and ocean["needs_job"] is None
    )
    # Twice is a 409 and writes nothing.
    seen = t.audit_rows()
    assert (
        _track(t, "dunbarton", 409)["detail"]
        == '"Client 26 - 378 E Dunbarton Rd" is already tracked.'
    )
    assert t.audit_rows() == seen


def test_the_link_tracks_the_row_in_the_same_action_and_untrack_waits_for_unlink(
    elm: Tenant,
) -> None:
    job = elm.new_job(ELM_ID)
    before = elm.job(job["id"])
    figures = (
        before["revised_contract"],
        before["unapproved_change_orders"],
        before["eac_in_basis"],
    )
    assert figures[2] == "327929.93"

    seen = elm.audit_rows()
    elm.link(job["id"], "elm", in_progress=True)
    rows = elm.audit_actions(seen)
    assert [r.action for r in rows] == ["job_alias_linked"]  # one row, not two
    tracked_at = rows[0].detail["rows"]["customer.tracked_at"]
    assert tracked_at[0] is None and tracked_at[1]
    _name, _active, when, who = _flag(elm, "elm")
    assert when.isoformat() == tracked_at[1] and who == elm.seed.users["rotate_me"].id
    assert [r["external_id"] for r in _tracked(elm)] == ["201"]
    assert _ledger(elm) == []

    detail = _untrack(elm, "elm", 409)["detail"]
    assert (
        detail
        == f'"{CUSTOMERS["elm"][1]}" is linked to job "{job["name"]}"; unlink it there first.'
    )
    assert _flag(elm, "elm")[2] is not None

    # Track and untrack move no figure (D-37: no accounting behaviour changes).
    _track(elm, "ocean")
    _untrack(elm, "ocean")
    after = elm.job(job["id"])
    assert (
        after["revised_contract"],
        after["unapproved_change_orders"],
        after["eac_in_basis"],
    ) == figures

    alias = next(a for a in after["aliases"] if a["system"] == "qbo_customer")
    elm.send("DELETE", f"/api/jobs/{job['id']}/aliases/{alias['id']}")
    assert _flag(elm, "elm")[2] is not None  # unlinking never untracks
    seen = elm.audit_rows()
    _untrack(elm, "elm")
    rows = elm.audit_actions(seen)
    assert [(r.action, r.entity_type) for r in rows] == [("customer_untracked", "customer")]
    assert rows[0].detail["before"]["external_id"] == "201"
    assert _flag(elm, "elm")[2:] == (None, None) and _tracked(elm) == []
    assert _untrack(elm, "elm", 409)["detail"] == f'"{CUSTOMERS["elm"][1]}" is not tracked.'


# --- the picker's search --------------------------------------------------------------------


def test_search_returns_active_rows_projects_first_and_empty_text_returns_nothing(
    t: Tenant,
) -> None:
    rows = _search(t, "hydroseed")["rows"]
    assert [(r["display_name"], r["kind_label"], r["parent_name"]) for r in rows] == [
        ("Pool - Hydroseed", "Project", "Pool - Hydroseed"),
        ("Pool - Hydroseed", "Customer", None),
    ]
    assert [r["external_id"] for r in _search(t, "6115758")["rows"]] == ["201"]  # 207 is inactive
    # projects by name ("Client 26 - 378…" before "EST6120638 Client 26 - …"), then the customer
    assert [r["external_id"] for r in _search(t, "Client 26")["rows"]] == ["203", "202", "102"]
    for q in ("", "   "):
        assert _search(t, q) == {"rows": [], "page": 1, "pages": 0, "total": 0, "page_size": 50}
    assert _search(t, "%")["total"] == 0  # a literal percent sign, not a wildcard
    assert _search(t, "_")["total"] == 0
    assert t.client.get(f"/api/customers?q={'a' * 201}").status_code == 422


def test_search_is_paged_at_fifty_and_the_page_is_clamped(t: Tenant) -> None:
    extra = {
        f"p{i}": (f"9{i:03d}", f"Paged customer {i:02d}", None, False, True) for i in range(55)
    }
    seed_customers(t.engine, t.id, extra)
    first = _search(t, "paged customer")
    assert (first["page"], first["pages"], first["total"], len(first["rows"])) == (1, 2, 55, 50)
    assert first["rows"][0]["display_name"] == "Paged customer 00"
    second = _search(t, "paged customer", 2)
    assert (second["page"], len(second["rows"])) == (2, 5)
    assert second["rows"][-1]["display_name"] == "Paged customer 54"
    assert _search(t, "paged customer", 9)["page"] == 2


def test_track_and_untrack_take_an_id_and_nothing_else(t: Tenant) -> None:
    unknown = uuid.uuid4()
    assert t.send("POST", f"/api/customers/{unknown}/track", None, 404)["detail"] == (
        "That QuickBooks customer is not in the platform's copy; sync and try again."
    )
    assert t.client.post(f"/api/customers/{OCEAN}/track", headers=CSRF).status_code == 422
    assert t.client.post("/api/customers/track", headers=CSRF).status_code in {404, 405}
    # tracking an inactive row by id is refused in one sentence
    assert _track(t, "inactive", 422)["detail"] == (
        '"6115758 Client 05 - Parking Lot (old)" is inactive in QuickBooks; track an active row.'
    )


# --- the sync writer and the inactive rule --------------------------------------------------


def test_a_change_poll_that_rewrites_a_tracked_row_leaves_it_tracked(t: Tenant) -> None:
    _track(t, "ocean")
    name, active, when, who = _flag(t, "ocean")
    _apply(
        t,
        {
            "Id": "204",
            "DisplayName": "1701 Ocean Blvd",
            "Active": True,
            "IsProject": True,
            "Job": True,
            "ParentRef": {"value": "103"},
        },
    )
    assert _flag(t, "ocean") == ("1701 Ocean Blvd", True, when, who)
    assert [(r["display_name"], r["tracked"]) for r in _search(t, "Ocean Blvd")["rows"]] == [
        ("1701 Ocean Blvd", True)
    ]


def test_an_inactive_tracked_row_stays_tracked_says_so_and_raises_nothing(t: Tenant) -> None:
    _track(t, "dunbarton")
    assert len(_ledger(t)) == 1
    _apply(
        t,
        {
            "Id": "203",
            "DisplayName": "Client 26 - 378 E Dunbarton Rd",
            "Active": False,
            "IsProject": True,
            "Job": True,
            "ParentRef": {"value": "102"},
        },
    )
    assert _flag(t, "dunbarton")[1] is False and _flag(t, "dunbarton")[2] is not None
    listed = _tracked(t)
    assert [(r["external_id"], r["active"], r["needs_job"], r["job"]) for r in listed] == [
        ("203", False, None, None)
    ]
    assert _ledger(t) == []
    assert _search(t, "Dunbarton")["total"] == 0  # the picker shows active rows only
    _untrack(t, "dunbarton")  # still a person's decision to undo
    assert _tracked(t) == []


# --- isolation and the figures that must not move ------------------------------------------------


def test_a_row_tracked_in_one_tenant_is_not_tracked_listed_or_audited_in_another(
    t: Tenant, seed: Seed, owner_engine: Engine, rw_engine: Engine, login_as
) -> None:
    marker = uuid.uuid4().hex[:8]
    with untenanted_session(owner_engine) as s:
        other = TenantRow(firm_id=seed.firm_id, name=f"Other {marker}", slug=f"other-{marker}")
        s.add(other)
        s.flush()
        other_id = other.id
    with tenant_session(owner_engine, other_id) as s:
        s.add(Membership(tenant_id=other_id, user_id=seed.users["rotate_me"].id, role=None))
    seed_customers(rw_engine, other_id, {k: CUSTOMERS[k] for k in ("c23", "ocean")})
    o = login_as("rotate_me", tenant=other_id)

    _track(t, "ocean")
    rows = o.get(f"/api/customers?q={quote('Ocean')}").json()["rows"]
    assert [(r["external_id"], r["tracked"]) for r in rows] == [("204", False)]
    assert o.get("/api/customers/tracked").json() == {"rows": []}
    assert (
        o.post(f"/api/customers/{t.customers['ocean']['id']}/track", headers=CSRF).status_code
        == 404
    )
    with tenant_session(rw_engine, other_id) as s:
        actions = set(s.execute(select(AuditLog.action)).scalars())
    assert "customer_tracked" not in actions


def test_month_totals_are_identical_before_and_after_tracking_and_untracking(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    from tests.qbo_fixtures import fixture
    from tests.qbo_helpers import FakeIntuit, FixtureCompany, connect_directly, installed
    from tests.test_qbo_tasks import _drain

    admin = login_as("rotate_me", tenant=fresh_tenant)
    fake = FakeIntuit()
    FixtureCompany().serve(fake)
    with installed(fake):
        connect_directly(rw_engine, fresh_tenant, fake)
        assert admin.post("/api/qbo/backfill", headers=CSRF).status_code == 200
        _drain(rw_engine, fresh_tenant)
        before = admin.get("/api/qbo/status").json()["held"]["month_totals"]
        assert before == fixture("oracle_month_totals")

        rows = admin.get("/api/customers?q=Amy").json()["rows"]
        assert rows and rows[0]["tracked"] is False
        cid = rows[0]["customer_id"]
        assert admin.post(f"/api/customers/{cid}/track", headers=CSRF).status_code == 200
        assert admin.get("/api/qbo/status").json()["held"]["month_totals"] == before
        assert admin.post(f"/api/customers/{cid}/untrack", headers=CSRF).status_code == 200
        assert admin.get("/api/qbo/status").json()["held"]["month_totals"] == before
