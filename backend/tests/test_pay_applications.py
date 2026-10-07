"""F08.1 Part 2 (D-26, D-36, D-39, D-42, D-43): the billing request, the pay application
with its schedule of values, the summary, the surcharge choice, the exceptions, issue
and void, the invoice tie and the PDF. Through the API on the reviewed 67 Elm Street and
EST6120638 workbooks with constructed documents (nothing here is a copy of a real
invoice). Every figure is computed on read; only the schedule as it stood at issue and
the three frozen figures are stored."""

import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, inspect, select

from app.core.db import tenant_session
from app.domain.billing.models import PayApplication, PayApplicationLine
from app.domain.billing.pay_application_pdf import Heading, application_pdf
from app.domain.billing.pay_applications import ApplicationView, LineView, Summary
from app.domain.config.policy import CHANGE_ORDER_EVIDENCE
from app.tenancy.models import Membership, Role
from tests.billing_helpers import (
    DEPOSIT_ITEM,
    FUEL_ITEM,
    apply_payloads,
    billing_policy,
    document_payload,
    line,
)
from tests.conftest import CSRF, Seed
from tests.job_helpers import ELM_ID, TURLEY_ID, Tenant, make_tenant, policy
from tests.test_invoice_lines import _confirm_all, _ledge, _pmt2, sales_line
from tests.test_zz_response_scan import _walk

D = Decimal
ELM_CUSTOMER = "201"
CONFIRM_KINDS = "work-areas/kinds/confirm-suggested"
FOUR_EARNED = "166294.48"


@pytest.fixture
def as_role(seed: Seed, owner_engine: Engine, login_as):
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


def _elm(t: Tenant) -> dict:
    job = t.new_job(ELM_ID)
    t.link(job["id"], "elm", in_progress=True)
    return t.send("POST", f"/api/jobs/{job['id']}/{CONFIRM_KINDS}")


def _deposit(t: Tenant) -> None:
    dep = document_payload(
        "5201",
        customer=ELM_CUSTOMER,
        date="2026-06-30",
        doc_number="EST6115758_DEP",
        lines=[line("149800.00", DEPOSIT_ITEM, "Deposit")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", dep)])


def _areas(job: dict) -> dict[int, str]:
    return {w["order_no"]: w["id"] for w in job["work_areas"]}


def _path(job_id: str) -> str:
    return f"/api/jobs/{job_id}/pay-applications"


def _request(
    t: Tenant,
    job: dict,
    percents: dict[int, str],
    *,
    on: str = "2026-08-21",
    surcharge: bool | None = None,
    apply_all: str | None = None,
    status: int = 201,
    client: TestClient | None = None,
) -> dict:
    ids = _areas(job)
    body = {
        "application_date": on,
        "surcharge_applies": surcharge,
        "percents": [{"estimate_work_area_id": ids[n], "percent": p} for n, p in percents.items()],
    }
    if apply_all is not None:
        body["apply_all"] = apply_all
    c = client or t.client
    r = c.post(_path(job["id"]), json=body, headers=CSRF)
    assert r.status_code == status, r.text
    return r.json()


def _issue(t: Tenant, job_id: str, app_id: str, status: int = 200, client=None) -> dict:
    r = (client or t.client).post(f"{_path(job_id)}/{app_id}/issue", headers=CSRF)
    assert r.status_code == status, r.text
    return r.json()


def _void(t: Tenant, job_id: str, app_id: str, reason: str | None, status: int = 200) -> dict:
    body = {} if reason is None else {"reason": reason}
    r = t.client.post(f"{_path(job_id)}/{app_id}/void", json=body, headers=CSRF)
    assert r.status_code == status, r.text
    return r.json()


def _lines(app: dict) -> dict[int, dict]:
    return {ln["order_no"]: ln for ln in app["lines"]}


def _summary(app: dict) -> tuple:
    s = app["summary"]
    return (s["earned_to_date"], s["billed_before"], s["amount_due"])


def _attention(t: Tenant, job_id: str, code: str) -> list[str]:
    return [i["message"] for i in t.job(job_id)["attention"] if i["code"] == code]


def _rows(t: Tenant) -> tuple[int, int]:
    with tenant_session(t.engine, t.id) as s:
        return (
            s.execute(select(func.count()).select_from(PayApplication)).scalar_one(),
            s.execute(select(func.count()).select_from(PayApplicationLine)).scalar_one(),
        )


# --- criteria 11 to 13: 67 Elm Street's first application --------------------------------------


def test_the_first_application_on_the_deposit_and_the_surcharge_choice(t: Tenant) -> None:
    job = _elm(t)
    _deposit(t)
    since = t.audit_rows()
    app = _request(t, job, {n: "100.00" for n in (1, 2, 3, 4)})
    assert (app["number"], app["status"], app["status_words"]) == (1, "draft", "Draft")
    assert _summary(app) == (FOUR_EARNED, "149800.00", "16494.48")
    lines = _lines(app)
    assert len(lines) == 16  # every kept original work area, the unnamed ones at 0.00
    assert (
        lines[1]["scheduled_value"],
        lines[1]["percent_complete"],
        lines[1]["earned_to_date"],
    ) == (
        "5500.00",
        "100.00",
        "5500.00",
    )
    assert (
        lines[1]["earned_previous"],
        lines[1]["earned_this_application"],
        lines[1]["balance_to_finish"],
    ) == (
        "0.00",
        "5500.00",
        "0.00",
    )
    assert lines[5]["percent_complete"] == "0.00" and lines[5]["balance_to_finish"] == "73049.39"
    assert 18 not in lines  # an unapproved change order is not listed
    assert app["summary"]["surcharge"] is None and app["summary"]["total_to_invoice"] == "16494.48"
    assert app["invoice_number"] == "EST6115758_PMT1" and app["issues"] == []
    # The surcharge choice is unanswered: issue is refused in words, nothing changes.
    r = t.client.post(f"{_path(job['id'])}/{app['id']}/issue", headers=CSRF)
    assert r.status_code == 422 and "fuel surcharge applies" in r.json()["detail"]
    assert t.get(f"{_path(job['id'])}/{app['id']}")["status"] == "draft"
    # The same request with the surcharge: the open draft is replaced, number unchanged.
    app = _request(t, job, {n: "100.00" for n in (1, 2, 3, 4)}, surcharge=True)
    assert (app["number"], _rows(t)) == (1, (1, 16))
    s = app["summary"]
    assert (s["surcharge_label"], s["surcharge"], s["total_to_invoice"]) == (
        "Fuel surcharge (5.00%)",
        "824.72",
        "17319.20",
    )
    # The rate undecided: refused in words; decided again: issued, with the figures frozen.
    billing_policy(t.engine, t.seed, t.id, rate=None)
    r = t.client.post(f"{_path(job['id'])}/{app['id']}/issue", headers=CSRF)
    assert r.status_code == 422 and "fuel surcharge rate" in r.json()["detail"]
    assert "rate" in " ".join(t.get(f"{_path(job['id'])}/{app['id']}")["notes"])
    billing_policy(t.engine, t.seed, t.id)
    issued = _issue(t, job["id"], app["id"])
    assert issued["status"] == "issued" and issued["status_words"].startswith("Issued on ")
    assert issued["issued_by"] == "rotate_me" and _summary(issued) == (
        FOUR_EARNED,
        "149800.00",
        "16494.48",
    )
    events = [a for a in t.audit_actions(since) if a.action.startswith("pay_application")]
    assert [a.action for a in events] == [
        "pay_application_drafted",
        "pay_application_drafted",
        "pay_application_issued",
    ]
    frozen = events[-1].detail["after"]
    assert (frozen["billed_before"], frozen["amount_due"], frozen["surcharge_rate"]) == (
        "149800.00",
        "16494.48",
        "0.0500",
    )
    with tenant_session(t.engine, t.id) as s:
        row = s.execute(select(PayApplication)).scalar_one()
        assert (row.billed_before, row.amount_due, row.surcharge_rate) == (
            D("149800.00"),
            D("16494.48"),
            D("0.0500"),
        )
    # Issued and not yet keyed: the review sentence; the invoice keyed from it ties.
    assert _attention(t, job["id"], "PAYAPP_NOT_INVOICED") == [
        'Pay application 1 on job "67 Elm Street | Parking Lot", issued for 2026-08-21, is due '
        "17,319.20 and no invoice EST6115758_PMT1 is in QuickBooks; key it from the application "
        "(D-36)."
    ]
    listing = t.get(_path(job["id"]))
    assert [a["number"] for a in listing["applications"]] == [1]
    assert listing["next_number"] == 2 and listing["surcharge_percent"] == "5.00"
    assert {a["order_no"]: a["previous_percent"] for a in listing["schedule"]}[1] == "100.00"
    # The same request entered again earns nothing more once the invoice is in.
    pmt1 = document_payload(
        "5211",
        customer=ELM_CUSTOMER,
        date="2026-08-21",
        doc_number="EST6115758_PMT1",
        lines=[
            sales_line("16494.48", "Pay application 1"),
            sales_line("824.72", "Fuel surcharge", item=FUEL_ITEM),
        ],
    )
    apply_payloads(t.engine, t.id, [("Invoice", pmt1)])
    assert _attention(t, job["id"], "PAYAPP_NOT_INVOICED") == []
    held = t.get(f"{_path(job['id'])}/{app['id']}")
    assert (held["invoice_held"], held["invoice_tied"]) == ("EST6115758_PMT1", True)
    again = _request(t, job, {n: "100.00" for n in (1, 2, 3, 4)}, surcharge=False)
    assert again["number"] == 2
    assert all(ln["earned_this_application"] == "0.00" for ln in again["lines"])
    assert [_lines(again)[n]["earned_previous"] for n in (1, 2, 3, 4)] == [
        "5500.00",
        "10830.68",
        "17142.92",
        "132820.88",
    ]
    assert _summary(again) == (FOUR_EARNED, FOUR_EARNED, "0.00")
    assert again["summary"]["no_invoice_due"] is True and again["invoice_number"] is None
    assert again["summary"]["billed_ahead"] is None
    # The invoice's lines are tied by the application on the job page (D-45's first slot).
    job_now = t.job(job["id"])
    tied = [ln for ln in job_now["invoice_lines"]["lines"] if ln["doc_number"] == "EST6115758_PMT1"]
    assert [(ln["how"], ln["how_label"]) for ln in tied] == [
        ("pay_application", "Pay application 1"),
        ("none", "Not assigned"),  # the fuel surcharge line is outside billed to date
    ]
    assert {w["order_no"]: w["billed_to_date"] for w in job_now["work_areas"]}[4] == "132820.88"
    assert (
        job_now["invoice_lines"]["not_assigned_to_work_area"] == "0.00"
    )  # the deposit is absorbed


# --- criterion 14: the schedule of values and change orders -----------------------------------


def test_an_approved_change_order_is_listed_only_from_its_agreed_date(t: Tenant, as_role) -> None:
    job = _elm(t)
    pm = as_role(t, "client_pm", Role.client_pm)
    area18 = _areas(job)[18]
    r = pm.post(
        f"/api/jobs/{job['id']}/work-areas/{area18}/approval",
        json={"agreed_on": "2026-09-14"},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text
    app = _request(t, job, {18: "100.00"}, on="2026-10-01", surcharge=False)
    assert _lines(app)[18]["scheduled_value"] == "5475.00" and _lines(app)[18]["label"] == "#18"
    assert len(app["lines"]) == 17
    before = _request(t, job, {}, on="2026-09-01", surcharge=False)
    assert 18 not in _lines(before) and len(before["lines"]) == 16
    r = pm.post(
        f"/api/jobs/{job['id']}/work-areas/{area18}/approval/withdraw",
        json={"reason": "not agreed after all"},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text
    after = _request(t, job, {}, on="2026-10-01", surcharge=False)
    assert 18 not in _lines(after)
    # A percent for a change order that is not approved is refused in one sentence.
    r = t.client.post(
        _path(job["id"]),
        json={
            "application_date": "2026-10-01",
            "percents": [{"estimate_work_area_id": area18, "percent": "100.00"}],
        },
        headers=CSRF,
    )
    assert r.status_code == 422 and "not on the schedule of values" in r.json()["detail"]


# --- criterion 15: EST6120638 and the four exceptions -----------------------------------------


def test_the_turley_instruction_and_the_four_exceptions(t: Tenant, as_role) -> None:
    job = t.new_job(TURLEY_ID)
    job = t.send("POST", f"/api/jobs/{job['id']}/{CONFIRM_KINDS}")
    pm = as_role(t, "client_pm", Role.client_pm)
    ids = _areas(job)
    for n in (25, 26, 31, 32):
        r = pm.post(
            f"/api/jobs/{job['id']}/work-areas/{ids[n]}/approval",
            json={"agreed_on": "2026-09-10"},
            headers=CSRF,
        )
        assert r.status_code == 200, r.text
    app = _request(
        t, job, {n: "100.00" for n in (3, 5, 9, 25, 26, 31, 32)}, on="2026-09-17", surcharge=False
    )
    at_100 = [ln["order_no"] for ln in app["lines"] if ln["percent_complete"] == "100.00"]
    assert at_100 == [3, 5, 9, 25, 31, 32]
    assert 26 not in _lines(app)
    assert [i["code"] for i in app["issues"]] == ["BILLING_UNPRICED_CO"]
    assert app["issues"][0]["message"] == (
        'Work area #26 "C/O INSTALLATION OF LOW VOLT LIGHTING" is priced 0.00 and cannot be '
        "billed by percent; it is left off this application (D-26)."
    )
    # Over 100, an omitted work area, and a percent below the previous application's.
    over = _request(t, job, {3: "150.00", 6: "10.00"}, on="2026-09-17", surcharge=False)
    assert sorted(i["code"] for i in over["issues"]) == ["BILLING_OMITTED_AREA", "BILLING_OVER_100"]
    messages = {i["code"]: i["message"] for i in over["issues"]}
    assert messages["BILLING_OVER_100"].startswith(
        'Work area #3 "TEMP IRRIGATION 6-24-26" was requested at 150.00%, above 100.00%'
    )
    assert messages["BILLING_OMITTED_AREA"].startswith(
        'Work area #6 "TERRACE" is omitted from the estimate'
    )
    assert 3 not in _lines(over) and 6 not in _lines(over)
    half = _request(t, job, {3: "50.00"}, on="2026-09-17", surcharge=False)
    _issue(t, job["id"], half["id"])
    lower = _request(t, job, {3: "40.00"}, on="2026-09-18", surcharge=False)
    assert [i["code"] for i in lower["issues"]] == ["BILLING_NEGATIVE"]
    assert lower["issues"][0]["message"] == (
        'Work area #3 "TEMP IRRIGATION 6-24-26" was requested at 40.00%, below the 50.00% on '
        "pay application 1; it is left off this application (D-26)."
    )
    assert 3 not in _lines(lower)
    # "Apply n% to every listed work area" (D-26) fills the unnamed ones.
    everything = _request(t, job, {}, on="2026-09-18", surcharge=False, apply_all="60.00")
    assert {ln["percent_complete"] for ln in everything["lines"]} == {"60.00"}
    assert [i["code"] for i in everything["issues"]] == [
        "BILLING_UNPRICED_CO"
    ]  # #26, requested at 60


# --- criterion 16: the tie ---------------------------------------------------------------------


def test_the_invoice_ties_to_its_application_or_says_how_it_differs(t: Tenant) -> None:
    job = _elm(t)
    _deposit(t)
    app = _request(t, job, {n: "100.00" for n in (1, 2, 3, 4)}, surcharge=False)
    _issue(t, job["id"], app["id"])
    assert len(_attention(t, job["id"], "PAYAPP_NOT_INVOICED")) == 1
    # Keyed for a different amount: the mismatch names the difference.
    wrong = document_payload(
        "5211",
        customer=ELM_CUSTOMER,
        date="2026-08-22",
        doc_number="EST6115758_PMT1",
        lines=[sales_line("17000.00", "Pay application 1")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", wrong)])
    assert _attention(t, job["id"], "PAYAPP_NOT_INVOICED") == []
    assert _attention(t, job["id"], "PAYAPP_INVOICE_MISMATCH") == [
        'Invoice EST6115758_PMT1 on job "67 Elm Street | Parking Lot" does not tie to pay '
        "application 1: the invoice less its fuel surcharge lines is 17,000.00 against an amount "
        "due of 16,494.48, a difference of 505.52 (D-39)."
    ]
    assert t.get(f"{_path(job['id'])}/{app['id']}")["invoice_tied"] is False
    # Corrected in QuickBooks (a new raw version): it ties.
    right = dict(wrong)
    right["Line"] = [sales_line("16494.48", "Pay application 1"), *wrong["Line"][1:]]
    right["Line"][-1] = {"DetailType": "SubTotalLineDetail", "Amount": D("16494.48")}
    right["TotalAmt"] = right["Balance"] = D("16494.48")
    apply_payloads(t.engine, t.id, [("Invoice", right)])
    assert _attention(t, job["id"], "PAYAPP_INVOICE_MISMATCH") == []
    assert t.get(f"{_path(job['id'])}/{app['id']}")["invoice_tied"] is True
    # An invoice with no application: raised when dated on or after the first issued
    # application, not before (the format changes job by job, D-36).
    later = document_payload(
        "5212",
        customer=ELM_CUSTOMER,
        date="2026-09-01",
        doc_number="4001",
        lines=[sales_line("100.00", "Extra")],
    )
    earlier = document_payload(
        "5213",
        customer=ELM_CUSTOMER,
        date="2026-07-15",
        doc_number="3999",
        lines=[sales_line("50.00", "Early")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", later), ("Invoice", earlier)])
    assert _attention(t, job["id"], "INVOICE_NO_PAYAPP") == [
        'Invoice 4001 on job "67 Elm Street | Parking Lot", dated 2026-09-01, is not keyed from '
        "a pay application; this job bills by pay application since 2026-08-21 (D-36)."
    ]
    # The board row carries the same sentences.
    row = next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job["id"])
    assert [i["code"] for i in row["attention"] if i["code"].startswith(("PAYAPP", "INVOICE"))] == [
        "INVOICE_NO_PAYAPP"
    ]
    # The surcharge part of the tie (D-39): the printed surcharge against the fuel lines.
    second = _request(
        t,
        job,
        {5: "100.00", **{n: "100.00" for n in (1, 2, 3, 4)}},
        on="2026-09-15",
        surcharge=True,
    )
    # Answer A: billed before is the whole billed to date, invoices 4001 and 3999 included
    # (166,444.48), so the amount due is 239,343.87 less that.
    assert (second["summary"]["billed_before"], second["summary"]["amount_due"]) == (
        "166444.48",
        "72899.39",
    )
    assert second["summary"]["surcharge"] == "3644.97"
    _issue(t, job["id"], second["id"])
    pmt2 = document_payload(
        "5214",
        customer=ELM_CUSTOMER,
        date="2026-09-15",
        doc_number="EST6115758_PMT2",
        lines=[
            sales_line("72899.39", "Pay application 2"),
            sales_line("3600.00", "Fuel", item=FUEL_ITEM),
        ],
    )
    apply_payloads(t.engine, t.id, [("Invoice", pmt2)])
    assert _attention(t, job["id"], "PAYAPP_INVOICE_MISMATCH") == [
        'Invoice EST6115758_PMT2 on job "67 Elm Street | Parking Lot" does not tie to pay '
        "application 2: its fuel surcharge lines are 3,600.00 against 3,644.97 printed (D-39)."
    ]


# --- criterion 17: void, and an issued application is never edited --------------------------------


def test_void_needs_a_reason_and_the_next_application_ignores_it(t: Tenant) -> None:
    job = _elm(t)
    _deposit(t)
    app = _request(t, job, {n: "100.00" for n in (1, 2, 3, 4)}, surcharge=False)
    _issue(t, job["id"], app["id"])
    since = t.audit_rows()
    _void(t, job["id"], app["id"], None, status=422)
    _void(t, job["id"], app["id"], "   ", status=422)
    voided = _void(t, job["id"], app["id"], "keyed on the wrong job")
    assert voided["status"] == "void" and voided["void_reason"] == "keyed on the wrong job"
    assert (
        voided["status_words"].startswith("Void. Voided on ") and voided["voided_by"] == "rotate_me"
    )
    assert [a.action for a in t.audit_actions(since)] == ["pay_application_voided"]
    _void(t, job["id"], app["id"], "again", status=409)
    assert _attention(t, job["id"], "PAYAPP_NOT_INVOICED") == []
    # The next application's "previous" figures ignore the void one; its number moves on.
    nxt = _request(t, job, {n: "100.00" for n in (1, 2, 3, 4)}, surcharge=False)
    assert nxt["number"] == 2
    assert [_lines(nxt)[n]["earned_previous"] for n in (1, 2)] == ["0.00", "0.00"]
    assert _summary(nxt) == (FOUR_EARNED, "149800.00", "16494.48")
    # An issued application cannot be edited through any route: a new request opens a new
    # draft, the issued rows stand, and there is no PUT or PATCH on an application.
    issued = _issue(t, job["id"], nxt["id"])
    with tenant_session(t.engine, t.id) as s:
        before = [
            (ln.order_no, ln.percent_complete)
            for ln in s.execute(
                select(PayApplicationLine)
                .where(PayApplicationLine.pay_application_id == uuid.UUID(issued["id"]))
                .order_by(PayApplicationLine.order_no)
            ).scalars()
        ]
    third = _request(t, job, {1: "50.00"}, surcharge=False)
    assert third["number"] == 3 and third["id"] != issued["id"]
    assert t.get(f"{_path(job['id'])}/{issued['id']}")["status"] == "issued"
    with tenant_session(t.engine, t.id) as s:
        after = [
            (ln.order_no, ln.percent_complete)
            for ln in s.execute(
                select(PayApplicationLine)
                .where(PayApplicationLine.pay_application_id == uuid.UUID(issued["id"]))
                .order_by(PayApplicationLine.order_no)
            ).scalars()
        ]
    assert after == before and before[0] == (1, D("100.00"))
    for method in ("PUT", "PATCH", "DELETE"):
        r = t.client.request(method, f"{_path(job['id'])}/{issued['id']}", json={}, headers=CSRF)
        assert r.status_code == 405, method
    _issue(t, job["id"], issued["id"], status=409)


# --- criterion 18: the PDF ----------------------------------------------------------------------


def test_the_pdf_carries_the_screen_figures_and_the_agreed_words(t: Tenant) -> None:
    job = _elm(t)
    _deposit(t)
    app = _request(t, job, {n: "100.00" for n in (1, 2, 3, 4)}, surcharge=True)
    r = t.client.get(f"{_path(job['id'])}/{app['id']}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert (
        r.content.startswith(b"%PDF")
        and 'filename="pay-application-EST6115758_PMT1.pdf"' in r.headers["content-disposition"]
    )
    text = r.content.decode("latin-1")
    for needle in (
        "Pay application 1",
        f"Customer: {t.job(job['id'])['customer_name']}",
        "Job: 67 Elm Street | Parking Lot",
        "Estimate: EST6115758",
        "Application date: 2026-08-21",
        "Draft",
        "Schedule of values",
        "Scheduled value",
        "Percent complete to date",
        "Earned on previous applications",
        "Balance to finish",
        "Mobilization",
        "132,820.88",
        "100.00%",
        "Total earned to date",
        "166,294.48",
        "Less billed to date before this application",
        "149,800.00",
        "Amount due this application",
        "16,494.48",
        "Fuel surcharge \\(5.00%\\)",  # the content stream escapes the parentheses
        "824.72",
        "Total to invoice",
        "17,319.20",
        "Invoice EST6115758_PMT1",
        "Page 1 of 1",
    ):
        assert needle in text, needle
    assert "legend" not in text.lower() and "jobcost" not in text.lower()
    tenant_name = t.get("/api/jobs")["tenant_name"]
    assert tenant_name in text
    issued = _issue(t, job["id"], app["id"])
    text = t.client.get(f"{_path(job['id'])}/{app['id']}/pdf").content.decode("latin-1")
    assert f"Issued on {issued['issued_at'][:10]}" in text
    # Billed ahead: the two agreed sentences, no surcharge line, no invoice line.
    ahead_view = ApplicationView(
        id="x",
        number=2,
        estimate_number="EST6115758",
        application_date=date(2026, 10, 7),
        status="draft",
        surcharge_applies=True,
        lines=(),
        summary=Summary(
            D("166294.48"), D("215569.48"), D("0.00"), D("49275.00"), True, D("0.0500"), None, None
        ),
        billed_before_undecided=False,
        rate_undecided=False,
        created_by=None,
        created_at=datetime(2026, 10, 7).isoformat(),
        issued_by=None,
        issued_at=None,
        voided_by=None,
        voided_at=None,
        void_reason=None,
    )
    text = application_pdf(ahead_view, Heading("Tenant", "Job", "Cust", "EST6115758")).decode(
        "latin-1"
    )
    assert "Billed ahead by 49,275.00" in text and "No invoice is due" in text
    assert "Fuel surcharge" not in text and "Invoice EST" not in text


def test_the_schedule_repeats_its_header_on_a_second_page() -> None:
    lines = tuple(
        LineView(
            "e",
            "EST1",
            "original",
            f"w{n}",
            n,
            f"Work area {n}",
            D("100.00"),
            D("50.00"),
            D("50.00"),
            D("0.00"),
            D("50.00"),
            D("50.00"),
        )
        for n in range(1, 91)
    )
    view = ApplicationView(
        id="x",
        number=1,
        estimate_number="EST1",
        application_date=date(2026, 10, 7),
        status="draft",
        surcharge_applies=False,
        lines=lines,
        summary=Summary(
            D("4500.00"), D("0.00"), D("4500.00"), None, False, None, None, D("4500.00")
        ),
        billed_before_undecided=False,
        rate_undecided=False,
        created_by=None,
        created_at=datetime(2026, 10, 7).isoformat(),
        issued_by=None,
        issued_at=None,
        voided_by=None,
        voided_at=None,
        void_reason=None,
    )
    text = application_pdf(view, Heading("Tenant", "Job", None, "EST1")).decode("latin-1")
    assert "Page 1 of 3" in text and "Page 3 of 3" in text  # 90 rows run to three pages
    assert text.count("Earned on previous applications") >= 2  # the header on both pages


# --- the owner's two criteria (2026-10-07) on the ledge fixture ------------------------------


def test_the_owners_two_criteria_on_the_ledge_fixture(t: Tenant) -> None:
    job = _elm(t)
    _pmt2(t)
    _ledge(t)
    job = t.job(job["id"])
    _confirm_all(t, job)  # the four _PMT2 lines to #1 to #4 (Part 1)
    assert t.job(job["id"])["billing"]["billed_to_date"] == "215569.48"
    five = _request(
        t, job, {n: "100.00" for n in (1, 2, 3, 4, 5)}, on="2026-10-07", surcharge=False
    )
    assert _summary(five) == ("239343.87", "215569.48", "23774.39")
    assert [_lines(five)[n]["earned_this_application"] for n in (1, 2, 3, 4, 5)] == [
        "0.00",
        "0.00",
        "0.00",
        "0.00",
        "73049.39",
    ]
    assert five["summary"]["billed_ahead"] is None and five["invoice_number"] == "EST6115758_PMT3"
    four = _request(t, job, {n: "100.00" for n in (1, 2, 3, 4)}, on="2026-10-07", surcharge=False)
    assert _summary(four) == (FOUR_EARNED, "215569.48", "0.00")
    s = four["summary"]
    assert (s["billed_ahead"], s["no_invoice_due"], s["total_to_invoice"], s["surcharge"]) == (
        "49275.00",
        True,
        None,
        None,
    )
    assert four["invoice_number"] is None
    issued = _issue(t, job["id"], four["id"])
    assert (issued["status"], issued["summary"]["amount_due"], issued["invoice_number"]) == (
        "issued",
        "0.00",
        None,
    )
    assert _attention(t, job["id"], "PAYAPP_NOT_INVOICED") == []
    assert _attention(t, job["id"], "INVOICE_NO_PAYAPP") == []  # the ledge invoices predate it


# --- criteria 19 and 20: money, the tables, roles, isolation, no retainage ---------------------


def test_roles_isolation_money_strings_and_no_retainage(
    t: Tenant, seed: Seed, as_role, login_as, rw_engine: Engine
) -> None:
    job = _elm(t)
    _deposit(t)
    pm = as_role(t, "client_pm", Role.client_pm)
    viewer = as_role(t, "client_viewer", Role.client_viewer)
    app = _request(
        t, job, {1: "100.00"}, surcharge=False, client=pm
    )  # answer C: client_pm requests
    _issue(t, job["id"], app["id"], status=403, client=pm)
    assert viewer.get(_path(job["id"])).status_code == 200
    assert viewer.get(f"{_path(job['id'])}/{app['id']}/pdf").status_code == 200
    assert (
        viewer.post(
            _path(job["id"]), json={"application_date": "2026-08-21"}, headers=CSRF
        ).status_code
        == 403
    )
    other = login_as("firm_admin", tenant=seed.tenant_b)
    assert other.get(_path(job["id"])).status_code == 404
    assert other.post(f"{_path(job['id'])}/{app['id']}/issue", headers=CSRF).status_code == 404
    with tenant_session(t.engine, seed.tenant_b) as s:
        assert s.execute(select(func.count()).select_from(PayApplication)).scalar_one() == 0
    problems: list[str] = []
    _walk(t.get(_path(job["id"])), "list", problems)
    _walk(app, "draft", problems)
    assert problems == []
    assert not any(c["name"] == "retainage_pct" for c in inspect(rw_engine).get_columns("job"))
    assert (
        "retainage"
        not in t.client.get(f"{_path(job['id'])}/{app['id']}/pdf").content.decode("latin-1").lower()
    )
    # Only a fixed-price job has pay applications (D-24).
    turley = t.new_job(TURLEY_ID)
    t.send("PATCH", f"/api/jobs/{turley['id']}", {"revenue_method": "time_and_materials"})
    listing = t.get(_path(turley["id"]))
    assert (
        listing["fixed_price"] is False
        and listing["applications"] == []
        and listing["schedule"] == []
    )
    r = t.client.post(_path(turley["id"]), json={"application_date": "2026-08-21"}, headers=CSRF)
    assert r.status_code == 422 and "fixed-price" in r.json()["detail"]
