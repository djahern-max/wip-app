"""F09 · Exceptions queue v1 (D-13, D-22, D-46; the owner's answers A to D of 2026-10-08),
through the API on the brief's fixture: the reviewed 67 Elm Street workbook, linked to a
synthetic project, with the constructed invoices of F08.1 (EST6115758_PMT2 and the two
ledge invoices, the nine ledge lines assigned to #18 and #22 to #29). The run is driven
through the worker (``run_until_quiet``) where a person's write queued it, and called
directly where the test counts its writes."""

import uuid
from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.audit.models import AuditLog
from app.core.db import tenant_session
from app.domain.config.policy import CHANGE_ORDER_EVIDENCE
from app.domain.exceptions.models import ExceptionEvent, ReviewException
from app.domain.exceptions.run import RunResult, refresh
from app.tenancy.models import Membership, Role
from tests.billing_helpers import (
    WORK_ITEM,
    apply_payloads,
    billing_policy,
    document_payload,
    line,
    payment_payload,
)
from tests.config_helpers import run_until_quiet
from tests.conftest import CSRF, Seed
from tests.estimate_helpers import build_workbook, upload_template
from tests.job_helpers import ELM_ID, TURLEY_ID, Tenant, make_tenant, policy
from tests.test_change_order_approval import _approve, _withdraw
from tests.test_invoice_lines import ELM_CUSTOMER, FLAG_NINE, LEDGE, _assign, _choice, _lines
from tests.test_pay_applications import _elm, _issue, _ledge_fixture, _request
from tests.test_zz_response_scan import _walk

D = Decimal
ELM_NAME = "67 Elm Street | Parking Lot"
FLAG_ONE = (
    f'5,475.00 has been billed on job "{ELM_NAME}" on 1 change order that is not approved: '
    "#18 (D-45)."
)
NO_NOTE = "Give the note: an exception is dismissed only with a note (D-46)."
BLOCKS = (
    "This exception blocks the period close and cannot be dismissed; it closes when its cause "
    "is gone (D-46)."
)
NOT_MEMBER = "That user is not a member of this company; assign to a member."
FIGURES = ("465469.59", "327929.93", "215569.48", "249900.11")


@pytest.fixture
def as_role(seed: Seed, owner_engine: Engine, login_as):
    """A seed client user given ``role`` in the fresh tenant, logged in there; the rows
    are removed afterwards (as the F07.4 and F08.1 tests do it)."""
    added: list[tuple[uuid.UUID, uuid.UUID]] = []

    def _as(t: Tenant, key: str, role: Role) -> TestClient:
        with tenant_session(owner_engine, t.id) as s:
            s.add(Membership(tenant_id=t.id, user_id=seed.users[key].id, role=role))
        added.append((t.id, seed.users[key].id))
        return login_as(key, tenant=t.id)

    yield _as
    for tenant_id, user_id in added:
        with tenant_session(owner_engine, tenant_id) as s:
            for m in s.execute(
                select(Membership).where(
                    Membership.tenant_id == tenant_id, Membership.user_id == user_id
                )
            ).scalars():
                s.delete(m)


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    tenant = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    billing_policy(rw_engine, seed, fresh_tenant)
    policy(rw_engine, seed, fresh_tenant, CHANGE_ORDER_EVIDENCE, "none")
    return tenant


# --- helpers ----------------------------------------------------------------------------------


def _figures(job: dict) -> tuple:
    b = job["billing"]
    return (
        job["revised_contract"],
        job["eac_in_basis"],
        b["billed_to_date"],
        b["remaining_to_bill"],
    )


def _rows(t: Tenant) -> tuple[int, int, int]:
    with tenant_session(t.engine, t.id) as s:
        return tuple(
            s.execute(select(func.count()).select_from(m)).scalar_one()
            for m in (ReviewException, ExceptionEvent, AuditLog)
        )


def _run(t: Tenant) -> RunResult:
    with tenant_session(t.engine, t.id) as s:
        return refresh(s, t.id)


def _db(t: Tenant, exception_id: str) -> tuple[ReviewException, list[str]]:
    with tenant_session(t.engine, t.id) as s:
        row = s.get(ReviewException, uuid.UUID(exception_id))
        kinds = list(
            s.execute(
                select(ExceptionEvent.kind)
                .where(ExceptionEvent.exception_id == row.id)
                .order_by(ExceptionEvent.occurred_at, ExceptionEvent.id)
            ).scalars()
        )
        s.expunge(row)
    return row, kinds


def _queue(t: Tenant, query: str = "", client=None) -> dict:
    c = client or t.client
    r = c.get(f"/api/exceptions{query}")
    assert r.status_code == 200, r.text
    return r.json()


def _of(q: dict, code: str, label: str | None = None) -> list[dict]:
    return [
        e
        for e in q["exceptions"]
        if e["code"] == code and (label is None or e["subject"]["label"] == label)
    ]


def _post(t: Tenant, exception_id: str, action: str, body: dict | None = None, status: int = 200):
    r = t.client.post(f"/api/exceptions/{exception_id}/{action}", json=body or {}, headers=CSRF)
    assert r.status_code == status, r.text
    return r.json()


def _board_codes(t: Tenant, job_id: str) -> list[str]:
    row = next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job_id)
    return [i["code"] for i in row["attention"]]


def _home(t: Tenant, job_id: str) -> dict:
    return next(j for j in t.get("/api/home")["jobs"] if j["id"] == job_id)


def _areas(t: Tenant, job_id: str) -> dict[int, str]:
    return {w["order_no"]: w["id"] for w in t.job(job_id)["work_areas"]}


def _nine(t: Tenant) -> dict:
    """The brief's fixture: ``_ledge_fixture`` (PMT2's four lines assigned, the two ledge
    invoices) with the nine ledge lines assigned to #18 and #22 to #29 (D-45)."""
    job = _ledge_fixture(t)
    ledge = [ln for ln in _lines(job) if ln["description"] == "Ledge Removal"]
    assert len(ledge) == 9
    for ln, n in zip(ledge, LEDGE, strict=True):
        job = _assign(t, job["id"], ln["billing_line_id"], _choice(job, n))
    return job


def _fixture(t: Tenant) -> dict:
    job = _nine(t)
    run_until_quiet(t.engine)  # the runs the person's writes queued (the after-write hook)
    return job


# --- criteria 2, 11: one run on the fixture; a second writes nothing; figures unchanged -------


def test_one_run_holds_the_flag_and_nine_unit_priced_and_a_second_run_writes_nothing(
    t: Tenant,
) -> None:
    before_figures = _figures(_nine(t))
    assert before_figures == FIGURES
    run_until_quiet(t.engine)
    q = _queue(t)
    (flag,) = _of(q, "BILLING_UNAPPROVED_CO")
    assert flag["message"] == FLAG_NINE
    assert flag["subject"]["type"] == "job" and flag["subject"]["label"] == ELM_NAME
    assert (flag["status"], flag["status_label"], flag["severity"], flag["severity_label"]) == (
        "open",
        "Open",
        "warn",
        "Needs attention",
    )
    assert flag["may_dismiss"] is True and flag["assigned_to"] is None
    unit = _of(q, "EST_UNIT_PRICED", ELM_ID)
    assert len(unit) == 9 and all(u["status"] == "open" for u in unit)
    with tenant_session(t.engine, t.id) as s:
        keys = set(
            s.execute(
                select(ReviewException.item_key).where(ReviewException.code == "EST_UNIT_PRICED")
            ).scalars()
        )
    assert keys == {str(n) for n in LEDGE}
    assert q["as_of"] is not None and q["counts"]["warn"] >= 10 and q["counts"]["block_close"] >= 1
    assert q["tenant_name"] and q["can_manage"] is True
    problems: list[str] = []
    _walk(q, "$", problems)
    assert problems == []
    # Open first, then by severity, then by age.
    statuses = [e["status"] for e in q["exceptions"]]
    assert statuses == sorted(statuses, key=lambda s: {"open": 0, "dismissed": 1, "resolved": 2}[s])
    # A second run writes nothing: no row, no event, no audit row.
    before = _rows(t)
    result = _run(t)
    assert result.writes == 0 and _rows(t) == before
    assert _run(t).writes == 0 and _rows(t) == before
    # Every figure identical after the migration and the runs.
    job = t.job(flag["subject"]["id"])
    assert _figures(job) == FIGURES


# --- criterion 3: resolving the cause clears it on the next run; the same exception returns ---


def test_resolving_the_cause_resolves_it_and_withdrawing_raises_the_same_exception(
    t: Tenant, as_role
) -> None:
    job = _fixture(t)
    (flag,) = _of(_queue(t), "BILLING_UNAPPROVED_CO")
    pm = as_role(t, "client_pm", Role.client_pm)
    areas = _areas(t, job["id"])
    for n in LEDGE:
        _approve(pm, job["id"], areas[n])
    run_until_quiet(t.engine)  # the approvals queued the run
    row, kinds = _db(t, flag["id"])
    assert (row.status, kinds) == ("resolved", ["raised", "resolved"])
    assert row.resolved_at is not None and row.dismissed_at is None
    assert _of(_queue(t, "?status=open"), "BILLING_UNAPPROVED_CO") == []
    (resolved,) = _of(_queue(t, "?status=resolved"), "BILLING_UNAPPROVED_CO")
    assert resolved["id"] == flag["id"] and resolved["resolved_at"] is not None
    assert "BILLING_UNAPPROVED_CO" not in _board_codes(t, job["id"])

    _withdraw(pm, job["id"], areas[18], "Ledge days are time and materials (D-24).")
    run_until_quiet(t.engine)
    again, kinds = _db(t, flag["id"])
    assert again.id == row.id and again.status == "open" and again.resolved_at is None
    assert (
        again.first_raised_at == row.first_raised_at and again.last_raised_at > row.last_raised_at
    )
    assert kinds == ["raised", "resolved", "raised_again"]
    assert again.message == FLAG_ONE and again.detail["amount"] == "5475.00"
    assert [
        i["message"] for i in t.job(job["id"])["attention"] if i["code"] == "BILLING_UNAPPROVED_CO"
    ] == [FLAG_ONE]
    assert len(_of(_queue(t), "BILLING_UNAPPROVED_CO")) == 1


# --- criteria 4, 5: dismissing requires a note; a dismissed exception stays out; reopen -------


def test_dismissing_requires_a_note_keeps_it_out_everywhere_and_reopen_brings_it_back(
    t: Tenant, seed: Seed
) -> None:
    job = _fixture(t)
    (flag,) = _of(_queue(t), "BILLING_UNAPPROVED_CO")
    before = _rows(t)
    assert _post(t, flag["id"], "dismiss", {}, 422)["detail"] == NO_NOTE
    assert _post(t, flag["id"], "dismiss", {"note": "   "}, 422)["detail"] == NO_NOTE
    assert _rows(t) == before and _db(t, flag["id"])[0].status == "open"

    note = "Ledge days move to a time-and-materials job with the project manager (D-24)."
    out = _post(t, flag["id"], "dismiss", {"note": note})
    assert (out["status"], out["status_label"]) == ("dismissed", "Dismissed")
    assert out["dismissed_by"] == "rotate_me"
    assert out["dismissed_at"] is not None
    assert [e["kind"] for e in out["events"]] == ["raised", "dismissed"]
    assert out["events"][-1]["text"] == note and out["events"][-1]["actor"] == out["dismissed_by"]
    rows, events, audits = before
    assert _rows(t) == (rows, events + 1, audits + 1)
    assert t.audit_actions(audits)[0].action == "exception_dismissed"

    # Not a need anywhere: the board, the job page, Home; listed under "Dismissed" on the job.
    assert "BILLING_UNAPPROVED_CO" not in _board_codes(t, job["id"])
    detail = t.job(job["id"])
    assert "BILLING_UNAPPROVED_CO" not in [i["code"] for i in detail["attention"]]
    assert _home(t, job["id"])["code"] != "billing_unapproved_co"
    (dismissed,) = [d for d in detail["dismissed"] if d["code"] == "BILLING_UNAPPROVED_CO"]
    assert (dismissed["message"], dismissed["note"], dismissed["dismissed_by"]) == (
        FLAG_NINE,
        note,
        out["dismissed_by"],
    )
    # Stays dismissed across runs while nothing changes; no write.
    held = _rows(t)
    assert _run(t).writes == 0 and _run(t).writes == 0 and _rows(t) == held
    assert _db(t, flag["id"])[0].status == "dismissed"

    # An EST_UNIT_PRICED on the estimate: gone from the Estimates list and detail, listed
    # under "Dismissed" on the estimate page and on the job page (its estimate's).
    (unit,) = [u for u in _of(_queue(t), "EST_UNIT_PRICED", ELM_ID) if "#18" in u["message"]]
    _post(t, unit["id"], "dismiss", {"note": "Billed on its own invoice."})
    listing = next(e for e in t.get("/api/estimates")["estimates"] if e["external_id"] == ELM_ID)
    est = t.get(f"/api/estimates/{listing['id']}")
    for body in (listing, est):
        unit_priced = [i["message"] for i in body["attention"] if i["code"] == "EST_UNIT_PRICED"]
        assert len(unit_priced) == 8 and not any("#18" in m for m in unit_priced)
    assert [d["code"] for d in est["dismissed"]] == ["EST_UNIT_PRICED"]
    assert est["dismissed"][0]["note"] == "Billed on its own invoice."
    assert [d["code"] for d in t.job(job["id"])["dismissed"]] == [
        "BILLING_UNAPPROVED_CO",
        "EST_UNIT_PRICED",
    ]
    assert _run(t).writes == 0

    # Reopen brings the flag back everywhere; one event, one audit row; a second reopen is 409.
    rows, events, audits = _rows(t)
    back = _post(t, flag["id"], "reopen")
    assert back["status"] == "open" and back["dismissed_at"] is None
    assert [e["kind"] for e in back["events"]] == ["raised", "dismissed", "reopened"]
    assert _rows(t) == (rows, events + 1, audits + 1)
    assert t.audit_actions(audits)[0].action == "exception_reopened"
    assert "BILLING_UNAPPROVED_CO" in _board_codes(t, job["id"])
    assert _home(t, job["id"])["code"] == "billing_unapproved_co"
    assert "BILLING_UNAPPROVED_CO" not in [d["code"] for d in t.job(job["id"])["dismissed"]]
    assert "not dismissed" in _post(t, flag["id"], "reopen", None, 409)["detail"]


# --- criterion 6: the owner's answers A and B -------------------------------------------------


def test_answer_a_a_block_close_exception_cannot_be_dismissed(t: Tenant) -> None:
    _fixture(t)
    unattached = _of(_queue(t), "EST_UNATTACHED")
    assert unattached and all(
        (u["severity"], u["may_dismiss"]) == ("block_close", False) for u in unattached
    )
    before = _rows(t)
    out = _post(t, unattached[0]["id"], "dismiss", {"note": "Known; a program estimate."}, 409)
    assert out["detail"] == BLOCKS
    assert _rows(t) == before and _db(t, unattached[0]["id"])[0].status == "open"
    # Its severity label on screen is words, not a colour.
    assert unattached[0]["severity_label"] == "Blocks period close"


def test_answer_b_a_dismissed_exception_opens_again_when_what_it_states_changes(
    t: Tenant, as_role
) -> None:
    job = _fixture(t)
    (flag,) = _of(_queue(t), "BILLING_UNAPPROVED_CO")
    _post(t, flag["id"], "dismiss", {"note": "Judged acceptable at 49,275.00."})
    pm = as_role(t, "client_pm", Role.client_pm)
    _approve(pm, job["id"], _areas(t, job["id"])[18])  # 43,800.00 on eight
    run_until_quiet(t.engine)
    row, kinds = _db(t, flag["id"])
    assert row.status == "open" and row.dismissed_at is None and row.dismissed_by is None
    assert kinds == ["raised", "dismissed", "reopened"]
    assert row.detail["amount"] == "43800.00" and "#18" not in row.message
    events = t.get(f"/api/exceptions/{flag['id']}")["events"]
    assert (
        events[1]["kind"] == "dismissed" and events[1]["text"] == "Judged acceptable at 49,275.00."
    )
    reopened = events[2]
    assert reopened["actor"] is None  # the run, not a person
    assert reopened["detail"]["before"]["amount"] == "49275.00"
    assert reopened["detail"]["after"]["amount"] == "43800.00"
    assert reopened["detail"]["after"]["work_areas"] == "#22, #23, #24, #25, #26, #27, #28, #29"

    # PAYMENT_UNAPPLIED: dismissed, unchanged across runs, reopened by a different amount.
    received = payment_payload(
        "7901", customer=ELM_CUSTOMER, date="2026-09-20", total="100.00", unapplied="100.00"
    )
    apply_payloads(t.engine, t.id, [("Payment", received)])
    assert _run(t).raised == 1
    (unapplied,) = _of(_queue(t), "PAYMENT_UNAPPLIED")
    assert "100.00 received" in unapplied["message"]
    _post(t, unapplied["id"], "dismiss", {"note": "Applied next week."})
    held = _rows(t)
    assert _run(t).writes == 0 and _rows(t) == held
    more = payment_payload(
        "7901", customer=ELM_CUSTOMER, date="2026-09-20", total="200.00", unapplied="200.00"
    )
    apply_payloads(t.engine, t.id, [("Payment", more)])
    result = _run(t)
    assert result.reopened == 1 and result.writes == 1
    row, kinds = _db(t, unapplied["id"])
    assert row.status == "open" and kinds == ["raised", "dismissed", "reopened"]
    assert "200.00 received" in row.message
    # A dismissed exception whose cause goes away is resolved; the note stays in its history.
    _post(t, row.id, "dismiss", {"note": "Still fine."})
    applied = payment_payload(
        "7901", customer=ELM_CUSTOMER, date="2026-09-20", total="200.00", unapplied="0"
    )
    apply_payloads(t.engine, t.id, [("Payment", applied)])
    assert _run(t).resolved == 1
    row, kinds = _db(t, unapplied["id"])
    assert row.status == "resolved" and row.dismissed_at is None
    assert kinds == ["raised", "dismissed", "reopened", "dismissed", "resolved"]


# --- criteria 7, 8: assign; roles -------------------------------------------------------------


def test_assign_to_a_member_only_and_assigned_to_me(t: Tenant, seed: Seed, as_role) -> None:
    _fixture(t)
    (flag,) = _of(_queue(t), "BILLING_UNAPPROVED_CO")
    before = _rows(t)
    out = _post(t, flag["id"], "assign", {"user_id": str(seed.users["scratch"].id)}, 422)
    assert out["detail"] == NOT_MEMBER and _rows(t) == before
    pm = as_role(t, "client_pm", Role.client_pm)  # entering the company writes its audit row
    pm_id = str(seed.users["client_pm"].id)
    assert pm_id in {m["id"] for m in _queue(t)["members"]}
    before = _rows(t)
    out = _post(t, flag["id"], "assign", {"user_id": pm_id})
    assert (out["assigned_to_id"], out["assigned_to"]) == (
        pm_id,
        "client_pm",
    )
    assert (
        out["events"][-1]["kind"] == "assigned"
        and out["events"][-1]["assigned_to"] == out["assigned_to"]
    )
    rows, events, audits = before
    assert _rows(t) == (rows, events + 1, audits + 1)
    assert t.audit_actions(audits)[0].action == "exception_assigned"
    mine = _queue(t, "?mine=true", client=pm)["exceptions"]
    assert [e["id"] for e in mine] == [flag["id"]]
    assert _queue(t, "?mine=true")["exceptions"] == []  # the firm admin has none
    assert "already assigned" in _post(t, flag["id"], "assign", {"user_id": pm_id}, 409)["detail"]
    cleared = _post(t, flag["id"], "assign", {"user_id": None})
    assert cleared["assigned_to"] is None and cleared["events"][-1]["kind"] == "assigned"
    assert cleared["events"][-1]["assigned_to"] is None


def test_the_roles_as_the_owner_answered(t: Tenant, as_role) -> None:
    _fixture(t)
    (flag,) = _of(_queue(t), "BILLING_UNAPPROVED_CO")
    pm = as_role(t, "client_pm", Role.client_pm)
    viewer = as_role(t, "client_viewer", Role.client_viewer)
    for c in (pm, viewer):
        assert c.get("/api/exceptions").status_code == 200
        assert c.get("/api/exceptions").json()["can_manage"] is False
        r = c.post(f"/api/exceptions/{flag['id']}/notes", json={"text": "Seen."}, headers=CSRF)
        assert r.status_code == 200 and r.json()["events"][-1]["text"] == "Seen."
        for action, body in (
            ("dismiss", {"note": "x"}),
            ("reopen", {}),
            ("assign", {"user_id": None}),
        ):
            r = c.post(f"/api/exceptions/{flag['id']}/{action}", json=body, headers=CSRF)
            assert r.status_code == 403, (action, r.text)
    assert _db(t, flag["id"])[0].status == "open"
    # A note is kept in order with who and when; an empty note is refused.
    r = t.client.post(f"/api/exceptions/{flag['id']}/notes", json={"text": " "}, headers=CSRF)
    assert (
        r.status_code == 422 and r.json()["detail"] == "Write the note: an empty note is not kept."
    )
    notes = [
        e
        for e in _post(t, flag["id"], "notes", {"text": "Second."})["events"]
        if e["kind"] == "note"
    ]
    assert [n["text"] for n in notes] == ["Seen.", "Seen.", "Second."]
    assert all(n["actor"] for n in notes)


# --- criterion 9: one computation per subject -------------------------------------------------


def test_one_computation_the_same_strings_on_the_board_the_job_page_and_the_queue(
    t: Tenant,
) -> None:
    job = _fixture(t)
    received = payment_payload(
        "7901", customer=ELM_CUSTOMER, date="2026-09-20", total="100.00", unapplied="100.00"
    )
    apply_payloads(t.engine, t.id, [("Payment", received)])
    # An estimate with a Labor Burden line (cost code 120) carries a burden sentence (D-05).
    upload_template(
        t.client,
        build_workbook(
            estimates=[
                {"estimate_id": "EST9000001", "name": "Burden", "status": "Sold", "price": "100.00"}
            ],
            work_areas=[
                {
                    "estimate_id": "EST9000001",
                    "order": 1,
                    "kept": "Y",
                    "name": "A",
                    "price": "100.00",
                }
            ],
            costs=[
                {"estimate_id": "EST9000001", "order": 1, "cost_code": "110", "amount": "50.00"},
                {"estimate_id": "EST9000001", "order": 1, "cost_code": "120", "amount": "10.00"},
            ],
        ),
        "burden.xlsx",
    )
    run_until_quiet(t.engine)
    _run(t)
    board = next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job["id"])
    page = t.job(job["id"])
    queue = [
        e["message"]
        for e in _queue(t, "?status=open")["exceptions"]
        if e["subject"]["type"] == "job" and e["subject"]["id"] == job["id"]
    ]
    board_messages = [i["message"] for i in board["attention"]]
    assert board_messages == [i["message"] for i in page["attention"]]
    assert sorted(board_messages) == sorted(queue) and len(queue) >= 2
    home = _home(t, job["id"])
    codes = {i["code"] for i in board["attention"]}
    assert home["code"] == "payment_unapplied" and "PAYMENT_UNAPPLIED" in codes
    # The Estimates list and the estimate detail agree, burden sentences included.
    listing = t.get("/api/estimates")["estimates"]
    burden_seen = False
    for row in listing:
        detail = t.get(f"/api/estimates/{row['id']}")
        assert [i["message"] for i in row["attention"]] == [
            i["message"] for i in detail["attention"]
        ]
        burden_seen |= any(
            i["code"].startswith("EST_NO_BURDEN") or i["code"] == "EST_BURDEN_LINE"
            for i in detail["attention"]
        )
    assert burden_seen, "the fixture has no burden sentence to compare"
    estimate_queue = {
        (e["subject"]["label"], e["message"])
        for e in _queue(t, "?status=open")["exceptions"]
        if e["subject"]["type"] == "estimate"
    }
    for row in listing:
        for i in row["attention"]:
            assert (row["external_id"], i["message"]) in estimate_queue


# --- criterion 10: Home's two new needs -------------------------------------------------------


def test_home_shows_other_credits_and_an_uninvoiced_pay_application(t: Tenant) -> None:
    job = _elm(t)
    on_job = document_payload(
        "5801",
        customer=ELM_CUSTOMER,
        date="2026-09-01",
        lines=[line("300.00", WORK_ITEM)],
        balance="0",
    )
    settled = payment_payload(
        "7801",
        customer=ELM_CUSTOMER,
        date="2026-09-14",
        total="100.00",
        applied=[("300.00", "Invoice", "5801"), ("200.00", "JournalEntry", "JE1")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", on_job), ("Payment", settled)])
    home = _home(t, job["id"])
    assert (home["code"], home["message"], home["count"]) == (
        "payment_other_credit",
        "200.00 of other credits applied on 1 payment: an invoice was settled by something other "
        "than cash (D-41). Collected to date leaves it out.",
        1,
    )
    turley = t.new_job(TURLEY_ID)
    t.link(turley["id"], "turley", in_progress=True)
    t.send("POST", f"/api/jobs/{turley['id']}/work-areas/kinds/confirm-suggested")
    draft = _request(t, t.job(turley["id"]), {1: "100.00"}, on="2026-09-01", surcharge=False)
    _issue(t, turley["id"], draft["id"])
    home = _home(t, turley["id"])
    assert (home["code"], home["message"]) == (
        "payapp_not_invoiced",
        "1 issued pay application has an amount due and no invoice in QuickBooks (D-36). Key the "
        "invoice from the application.",
    )
    run_until_quiet(t.engine)
    assert len(_of(_queue(t, "?status=open"), "PAYAPP_NOT_INVOICED")) == 1
    assert len(_of(_queue(t, "?status=open"), "PAYMENT_OTHER_CREDIT")) == 1


# --- criterion 12: the firm count, one company at a time ------------------------------------


def test_the_company_picker_counts_are_each_companys_own(t: Tenant, seed: Seed, as_role) -> None:
    _fixture(t)
    counts = _queue(t)["counts"]
    mine = {c["tenant_id"]: c for c in t.get("/api/session/tenants")}
    assert mine[str(t.id)]["open_exceptions"] == counts
    assert counts["warn"] >= 10 and counts["block_close"] >= 1
    # Tenant A's count is tenant A's, not this company's.
    a = mine[str(seed.tenant_a)]["open_exceptions"]
    assert a is not None and a != counts
    # A client user sees their companies and no other's count.
    viewer = as_role(t, "client_viewer", Role.client_viewer)
    theirs = {c["tenant_id"]: c for c in viewer.get("/api/session/tenants").json()}
    # Their own companies only (other tests may have given the seed user more memberships;
    # tenant B is never one of them): each row carries that company's own count.
    assert {str(t.id), str(seed.tenant_a)} <= set(theirs) and str(seed.tenant_b) not in theirs
    assert theirs[str(t.id)]["open_exceptions"] == counts
    assert all(c["open_exceptions"] is not None for c in theirs.values())


def test_a_second_tenant_reads_none_of_the_firsts_exceptions(
    t: Tenant, login_as, seed: Seed
) -> None:
    _fixture(t)
    assert _queue(t)["total"] >= 10
    other = login_as("firm_admin", tenant=seed.tenant_b)
    body = other.get("/api/exceptions").json()
    assert all(e["subject"]["label"] != ELM_NAME for e in body["exceptions"])
    ids = {e["id"] for e in _queue(t)["exceptions"]}
    for exception_id in list(ids)[:3]:
        assert other.get(f"/api/exceptions/{exception_id}").status_code == 404
