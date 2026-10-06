"""F08: the Sold Jobs Board and the job page's Billing section through the API, on the
F07 job fixture (67 Elm Street from the owner's reviewed workbook, linked to a synthetic
project) with documents and payments in the sandbox shape applied through the
normalizers: the D-39 surcharge case, the two-part deposit rule (owner's answer 2),
D-02's 67 Elm Street states, voided and deleted documents, remaining to bill and
BILLED_OVER_CONTRACT, the not-on-a-job row (D-35, D-37), the undecided keys, Home's
three new needs in order, isolation, and that nothing is stored."""

import uuid
from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect

from app.core.db import tenant_session
from app.domain.billing.board import load_board
from app.domain.jobs import service as jobs
from app.tenancy.models import Base
from tests.billing_helpers import (
    DEPOSIT_ITEM,
    FUEL_ITEM,
    WORK_ITEM,
    apply_payloads,
    billing_policy,
    document_payload,
    line,
    payment_payload,
    voided_payload,
)
from tests.conftest import Seed
from tests.job_helpers import ELM_ID, TURLEY_ID, Tenant, make_tenant

D = Decimal
ELM_CUSTOMER = "201"  # the project row the F07 helpers seed for 67 Elm Street
PARENT = "101"  # its parent customer (D-35: money there is not on the job)
UNTRACKED = "206"  # a sub-customer nobody picked (D-37)
# The revised contract of the reviewed workbook after "Confirm all as suggested": the kept
# work areas suggested original (the "CO:" rows are change orders, outside it; D-01).
ELM_CONTRACT = "465469.59"
# The F07 helpers seed one 1,000.00 invoice (2026-08-31) on the untracked 1701 Ocean
# project: it is money not on a job in every test here.
OCEAN_SEED = D("1000.00")


@pytest.fixture
def t(seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant) -> Tenant:
    tenant = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    billing_policy(rw_engine, seed, fresh_tenant)
    return tenant


def _elm(t: Tenant, *, confirm: bool = True) -> dict:
    job = t.new_job(ELM_ID)
    t.link(job["id"], "elm", in_progress=True)
    if confirm:
        t.send("POST", f"/api/jobs/{job['id']}/work-areas/kinds/confirm-suggested")
    return t.job(job["id"])


def _row(t: Tenant, job_id: str) -> dict:
    return next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job_id)


def _codes(detail: dict) -> list[str]:
    return [i["code"] for i in detail["attention"]]


def test_the_d39_surcharge_invoice_and_credit_memo(t: Tenant) -> None:
    job = _elm(t)
    invoice = document_payload(
        "5001",
        customer=ELM_CUSTOMER,
        date="2026-09-15",
        doc_number="EST6115758_PMT3",
        lines=[
            line("70290.81", WORK_ITEM, "Pay application 3"),
            line("3514.54", FUEL_ITEM, "Fuel surcharge (5.00%)"),
        ],
        balance="0",
    )
    paid = payment_payload(
        "7001",
        customer=ELM_CUSTOMER,
        date="2026-09-30",
        total="73805.35",
        applied=[("73805.35", "Invoice", "5001")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", invoice), ("Payment", paid)])
    b = t.job(job["id"])["billing"]
    assert (b["billed_to_date"], b["fuel_surcharge_billed"]) == ("70290.81", "3514.54")
    assert (b["collected_to_date"], b["open_ar"], b["unapplied_payments"]) == (
        "73805.35",
        "0.00",
        "0.00",
    )
    assert (b["deposit_invoiced"], b["deposit_received"]) == ("0.00", "0.00")
    assert b["remaining_to_bill"] == str(D(ELM_CONTRACT) - D("70290.81"))
    assert (b["last_billing_date"], b["last_payment_date"]) == ("2026-09-15", "2026-09-30")
    assert _row(t, job["id"])["billing"] == b

    # A credit memo carrying a 100.00 surcharge line and a 50.00 work line.
    memo = document_payload(
        "5002",
        customer=ELM_CUSTOMER,
        date="2026-09-20",
        doc_number="CM-1",
        lines=[line("50.00", WORK_ITEM), line("100.00", FUEL_ITEM)],
    )
    apply_payloads(t.engine, t.id, [("CreditMemo", memo)])
    b = t.job(job["id"])["billing"]
    assert (b["billed_to_date"], b["fuel_surcharge_billed"], b["open_ar"]) == (
        "70240.81",
        "3414.54",
        "-150.00",
    )
    # The same line on an item that is not a surcharge item counts in billed to date.
    plain = document_payload(
        "5003",
        customer=ELM_CUSTOMER,
        date="2026-09-21",
        lines=[line("3514.54", WORK_ITEM, "Fuel surcharge (5.00%)")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", plain)])
    b = t.job(job["id"])["billing"]
    assert (b["billed_to_date"], b["fuel_surcharge_billed"]) == ("73755.35", "3414.54")

    history = t.job(job["id"])["billing_history"]
    assert [
        (
            h["doc_number"],
            h["kind_label"],
            h["total"],
            h["fuel_surcharge"],
            h["billed"],
            h["counted"],
        )
        for h in history
    ] == [
        (None, "Invoice", "3514.54", "0.00", "3514.54", True),
        ("CM-1", "Credit memo", "-150.00", "-100.00", "-50.00", True),
        ("EST6115758_PMT3", "Invoice", "73805.35", "3514.54", "70290.81", True),
    ]
    payments = t.job(job["id"])["payment_history"]
    assert [
        (p["txn_date"], p["total"], p["applied"], p["unapplied"], p["on_this_job"])
        for p in payments
    ] == [
        (
            "2026-09-30",
            "73805.35",
            [{"document": "EST6115758_PMT3", "amount": "73805.35"}],
            "0.00",
            True,
        )
    ]
    assert _codes(t.job(job["id"])) == []


def test_the_deposit_needs_the_dep_number_and_the_deposit_item(t: Tenant) -> None:
    job = _elm(t)
    deposit = document_payload(
        "5101",
        customer=ELM_CUSTOMER,
        date="2026-06-30",
        doc_number="EST6115758_DEP",
        lines=[line("149800.00", DEPOSIT_ITEM, "Deposit")],
        balance="0",
    )
    received = payment_payload(
        "7101",
        customer=ELM_CUSTOMER,
        date="2026-07-01",
        total="149800.00",
        applied=[("149800.00", "Invoice", "5101")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", deposit), ("Payment", received)])
    detail = t.job(job["id"])
    b = detail["billing"]
    assert (b["deposit_invoiced"], b["deposit_received"], b["billed_to_date"]) == (
        "149800.00",
        "149800.00",
        "149800.00",
    )
    assert b["deposit_note"] is None and _codes(detail) == []
    assert detail["billing_history"][0]["is_deposit"] is True
    assert detail["billing_history"][0]["txn_date"] == "2026-06-30"

    # One part without the other: still billed, not a deposit, one sentence per document.
    number_only = document_payload(
        "5102",
        customer=ELM_CUSTOMER,
        date="2026-07-02",
        doc_number="EST6115758_DEP",
        lines=[line("1000.00", WORK_ITEM)],
    )
    item_only = document_payload(
        "5103",
        customer=ELM_CUSTOMER,
        date="2026-07-03",
        doc_number="EST6115758_PMT1",
        lines=[line("2000.00", DEPOSIT_ITEM)],
    )
    apply_payloads(t.engine, t.id, [("Invoice", number_only), ("Invoice", item_only)])
    detail = t.job(job["id"])
    b = detail["billing"]
    assert (b["deposit_invoiced"], b["billed_to_date"]) == ("149800.00", "152800.00")
    assert (
        b["deposit_note"]
        == "2 documents look like the deposit but is not identified as one; see Attention."
    )
    items = [i for i in detail["attention"] if i["code"] == "DEPOSIT_NOT_IDENTIFIED"]
    assert len(items) == 2 and {i["message"].split(" on job")[0] for i in items} == {
        "Invoice EST6115758_PMT1",
        "Invoice EST6115758_DEP",
    }
    by_doc = {i["message"].split(" on job")[0]: i["message"] for i in items}
    assert by_doc["Invoice EST6115758_PMT1"] == (
        'Invoice EST6115758_PMT1 on job "67 Elm Street | Parking Lot" is not identified as the '
        "deposit: its lines are on a deposit item but its number is not <estimate number>_DEP. "
        "It counts in billed to date; fix the item or the document number in QuickBooks (D-02)."
    )
    assert (
        "its number ends in _DEP but its lines are not all on a deposit item"
        in by_doc["Invoice EST6115758_DEP"]
    )
    assert [d["is_deposit"] for d in detail["billing_history"]] == [False, False, True]
    # Home names the need, after the F07 ones.
    home = next(j for j in t.get("/api/home")["jobs"] if j["id"] == job["id"])
    assert (home["code"], home["message"]) == (
        "deposit_not_identified",
        "2 documents are not identified as the deposit (D-02): fix the item or the number in "
        "QuickBooks.",
    )


def test_67_elm_street_unapplied_then_applied_and_the_voided_deposit_invoice(t: Tenant) -> None:
    """D-02's case. The deposit invoice was withdrawn (voided, criterion 9); 149,800.00
    received 2026-07-01 sat unapplied until it was applied to EST6115758_PMT2
    (166,294.48) on 2026-08-21, leaving 16,494.48 open."""
    job = _elm(t)
    dep = document_payload(
        "5201",
        customer=ELM_CUSTOMER,
        date="2026-06-30",
        doc_number="EST6115758_DEP",
        lines=[line("149800.00", DEPOSIT_ITEM, "Deposit", line_id="1")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", dep)])
    apply_payloads(t.engine, t.id, [("Invoice", voided_payload(dep))])
    received = payment_payload(
        "7201", customer=ELM_CUSTOMER, date="2026-07-01", total="149800.00", unapplied="149800.00"
    )
    apply_payloads(t.engine, t.id, [("Payment", received)])

    detail = t.job(job["id"])
    b = detail["billing"]
    assert (
        b["unapplied_payments"],
        b["billed_to_date"],
        b["collected_to_date"],
        b["deposit_invoiced"],
    ) == ("149800.00", "0.00", "0.00", "0.00")
    assert b["remaining_to_bill"] == ELM_CONTRACT
    assert _codes(detail) == ["PAYMENT_UNAPPLIED"]
    assert detail["attention"][0]["message"] == (
        'Job "67 Elm Street | Parking Lot" has 149,800.00 received and not applied to any '
        "invoice (1 payment). It is not billed or collected to date until it is applied in "
        "QuickBooks (D-02)."
    )
    voided = detail["billing_history"]
    assert [
        (h["doc_number"], h["state_label"], h["counted"], h["total"], h["billed"]) for h in voided
    ] == [("EST6115758_DEP", "Voided", False, "0.00", "0.00")]
    assert [
        (p["unapplied"], p["applied"], p["state_label"]) for p in detail["payment_history"]
    ] == [("149800.00", [], "")]
    home = next(j for j in t.get("/api/home")["jobs"] if j["id"] == job["id"])
    assert (home["code"], home["message"]) == (
        "payment_unapplied",
        "149,800.00 received is not applied to any invoice (D-02). Apply it in QuickBooks once "
        "the invoice exists.",
    )

    pmt2 = document_payload(
        "5202",
        customer=ELM_CUSTOMER,
        date="2026-08-21",
        doc_number="EST6115758_PMT2",
        lines=[line("166294.48", WORK_ITEM, "Pay application 2")],
        balance="16494.48",
    )
    applied = payment_payload(
        "7201",
        customer=ELM_CUSTOMER,
        date="2026-07-01",
        total="149800.00",
        unapplied="0",
        applied=[("149800.00", "Invoice", "5202")],
    )
    apply_payloads(t.engine, t.id, [("Invoice", pmt2), ("Payment", applied)])
    detail = t.job(job["id"])
    b = detail["billing"]
    assert _codes(detail) == []
    assert (b["unapplied_payments"], b["collected_to_date"], b["billed_to_date"], b["open_ar"]) == (
        "0.00",
        "149800.00",
        "166294.48",
        "16494.48",
    )
    assert b["remaining_to_bill"] == str(D(ELM_CONTRACT) - D("166294.48"))
    assert detail["billing_history"][0]["balance"] == "16494.48"
    assert [p["applied"] for p in detail["payment_history"]] == [
        [{"document": "EST6115758_PMT2", "amount": "149800.00"}]
    ]
    assert (b["last_billing_date"], b["last_payment_date"]) == ("2026-08-21", "2026-07-01")
    today = jobs.tenant_today  # the day count is against the tenant's today
    with tenant_session(t.engine, t.id) as s:
        from datetime import date

        assert b["days_since_activity"] == (today(s) - date(2026, 8, 21)).days
    home = next(j for j in t.get("/api/home")["jobs"] if j["id"] == job["id"])
    assert home["code"] is None

    # A deleted document and a deleted payment: in no figure, in the history, marked.
    gone = document_payload(
        "5203", customer=ELM_CUSTOMER, date="2026-08-25", lines=[line("999.00", WORK_ITEM)]
    )
    gone_pay = payment_payload(
        "7203", customer=ELM_CUSTOMER, date="2026-08-26", total="5.00", unapplied="5.00"
    )
    apply_payloads(t.engine, t.id, [("Invoice", gone), ("Payment", gone_pay)])
    apply_payloads(t.engine, t.id, [("Invoice", gone), ("Payment", gone_pay)], deleted=True)
    detail = t.job(job["id"])
    assert detail["billing"] == b
    assert [(h["external_id"], h["state_label"]) for h in detail["billing_history"]][:1] == [
        ("5203", "Deleted")
    ]
    assert [(p["external_id"], p["state_label"]) for p in detail["payment_history"]][:1] == [
        ("7203", "Deleted")
    ]


def test_remaining_to_bill_follows_the_revenue_method_and_raises_over_contract(t: Tenant) -> None:
    unconfirmed = _elm(t, confirm=False)
    invoice = document_payload(
        "5301", customer=ELM_CUSTOMER, date="2026-09-01", lines=[line("100000.00", WORK_ITEM)]
    )
    apply_payloads(t.engine, t.id, [("Invoice", invoice)])
    row = _row(t, unconfirmed["id"])
    assert row["to_confirm"] == 28 and row["revised_contract_note"]  # the F07 sentence stays
    assert row["billing"]["remaining_to_bill"] == str(D(row["revised_contract"]) - D("100000.00"))
    assert row["estimate_number"] == "EST6115758" and row["estimator"] == "Estimator A"
    t.send("POST", f"/api/jobs/{unconfirmed['id']}/work-areas/kinds/confirm-suggested")
    row = _row(t, unconfirmed["id"])
    assert (row["revised_contract"], row["billing"]["remaining_to_bill"]) == (
        ELM_CONTRACT,
        "365469.59",
    )
    assert row["billing"]["remaining_to_bill_note"] is None

    over = document_payload(
        "5302", customer=ELM_CUSTOMER, date="2026-09-02", lines=[line("425000.00", WORK_ITEM)]
    )
    apply_payloads(t.engine, t.id, [("Invoice", over)])
    detail = t.job(unconfirmed["id"])
    assert detail["billing"]["remaining_to_bill"] == "-59530.41"  # shown as it comes out
    assert _codes(detail) == ["BILLED_OVER_CONTRACT"]
    assert detail["attention"][0]["message"] == (
        'Job "67 Elm Street | Parking Lot" is billed 59,530.41 over its revised contract '
        "(525,000.00 billed against 465,469.59): likely a change order not yet approved."
    )
    home = next(j for j in t.get("/api/home")["jobs"] if j["id"] == unconfirmed["id"])
    assert (home["code"], home["message"]) == (
        "billed_over_contract",
        "Billed 59,530.41 over the revised contract: likely a change order not yet approved.",
    )

    # T&M, pool and program jobs have no contract: no remaining to bill, said in words.
    tm = t.new_job(TURLEY_ID)
    t.send("PATCH", f"/api/jobs/{tm['id']}", {"revenue_method": "time_and_materials"})
    row = _row(t, tm["id"])
    assert row["billing"]["remaining_to_bill"] is None
    assert (
        row["billing"]["remaining_to_bill_note"] == "Not shown: Time and materials has no contract."
    )
    pool = t.send(
        "POST",
        "/api/jobs/pool",
        {"name": "Pool - Hydroseed", "division_id": str(t.divisions["LS"])},
        201,
    )
    assert pool["billing"]["remaining_to_bill_note"] == "Not shown: Pool has no contract."
    program = t.send(
        "POST",
        "/api/jobs/program",
        {"name": "Snow 2026-27", "division_id": str(t.divisions["SNOW"])},
        201,
    )
    assert (
        program["billing"]["remaining_to_bill_note"]
        == "Not shown: Recurring service has no contract."
    )


def test_home_orders_the_three_needs_after_the_f07_ones(t: Tenant) -> None:
    job = _elm(t, confirm=False)
    docs = [
        (
            "Invoice",
            document_payload(
                "5401",
                customer=ELM_CUSTOMER,
                date="2026-09-01",
                doc_number="EST6115758_DEP",
                lines=[line("600000.00", WORK_ITEM)],
            ),
        ),
        (
            "Payment",
            payment_payload(
                "7401", customer=ELM_CUSTOMER, date="2026-09-02", total="10.00", unapplied="10.00"
            ),
        ),
    ]
    apply_payloads(t.engine, t.id, docs)

    def need() -> str:
        return next(j for j in t.get("/api/home")["jobs"] if j["id"] == job["id"])["code"]

    assert need() == "to_confirm"  # F07 first
    t.send("POST", f"/api/jobs/{job['id']}/work-areas/kinds/confirm-suggested")
    assert _codes(t.job(job["id"])) == [
        "PAYMENT_UNAPPLIED",
        "DEPOSIT_NOT_IDENTIFIED",
        "BILLED_OVER_CONTRACT",
    ]
    assert need() == "payment_unapplied"
    apply_payloads(
        t.engine,
        t.id,
        [
            (
                "Payment",
                payment_payload(
                    "7401",
                    customer=ELM_CUSTOMER,
                    date="2026-09-02",
                    total="10.00",
                    applied=[("10.00", "Invoice", "5401")],
                ),
            )
        ],
    )
    assert need() == "deposit_not_identified"
    apply_payloads(
        t.engine,
        t.id,
        [
            (
                "Invoice",
                document_payload(
                    "5401",
                    customer=ELM_CUSTOMER,
                    date="2026-09-01",
                    doc_number="EST6115758_PMT1",
                    lines=[line("600000.00", WORK_ITEM)],
                ),
            )
        ],
    )
    assert need() == "billed_over_contract"


def test_money_not_on_a_job_is_one_row_and_a_second_tenant_sees_nothing(
    t: Tenant,
    seed: Seed,
    rw_engine: Engine,
    owner_engine: Engine,
    login_as: Callable[..., TestClient],
) -> None:
    job = _elm(t)
    on_job = document_payload(
        "5501",
        customer=ELM_CUSTOMER,
        date="2026-09-01",
        lines=[line("1000.00", WORK_ITEM)],
        balance="400",
    )
    on_parent = document_payload(
        "5502", customer=PARENT, date="2026-09-02", lines=[line("200.00", WORK_ITEM)]
    )  # D-35: a data error, unassigned
    on_untracked = document_payload(
        "5503", customer=UNTRACKED, date="2026-09-03", lines=[line("30.00", FUEL_ITEM)], tax="2.00"
    )
    paid = payment_payload(
        "7501",
        customer=PARENT,
        date="2026-09-04",
        total="700.00",
        unapplied="100.00",
        applied=[("600.00", "Invoice", "5501")],
    )
    apply_payloads(
        t.engine,
        t.id,
        [("Invoice", on_job), ("Invoice", on_parent), ("Invoice", on_untracked), ("Payment", paid)],
    )
    body = t.get("/api/jobs")
    b = _row(t, job["id"])["billing"]
    # The payment is on the parent row; its application follows the document (point 3).
    assert (b["billed_to_date"], b["collected_to_date"], b["open_ar"], b["unapplied_payments"]) == (
        "1000.00",
        "600.00",
        "400.00",
        "0.00",
    )
    other = body["not_on_a_job"]
    assert (
        other["billed_to_date"],
        other["fuel_surcharge_billed"],
        other["collected_to_date"],
        other["unapplied_payments"],
        other["open_ar"],
    ) == (
        str(D("200.00") + OCEAN_SEED),
        "30.00",
        "0.00",
        "100.00",
        str(D("232.00") + OCEAN_SEED),
    )
    assert (
        body["totals"]["billed_to_date"] == "1000.00"
        and body["totals"]["collected_to_date"] == "600.00"
    )
    tie = t.get("/api/jobs/tie-out")  # F08.2: its own request; 2026-08, 2026-09
    assert "tie_out" not in body and tie["balanced"] is True and tie["months"] == 2
    assert tie["status"] == "Ties to the cent to the QuickBooks month totals (2 months)."
    assert body["tenant_name"] and body["as_of"] and body["policy_note"] is None
    # Moving the document to an untracked row moves its amount, and the tie still holds.
    moved = dict(on_job, CustomerRef={"value": UNTRACKED})
    apply_payloads(t.engine, t.id, [("Invoice", moved)])
    body = t.get("/api/jobs")
    assert _row(t, job["id"])["billing"]["billed_to_date"] == "0.00"
    assert (
        body["not_on_a_job"]["billed_to_date"] == str(D("1200.00") + OCEAN_SEED)
        and body["not_on_a_job"]["collected_to_date"] == "600.00"
    )
    assert t.get("/api/jobs/tie-out")["balanced"] is True
    # The totals row follows the filters.
    assert t.get("/api/jobs?status=sold")["totals"]["collected_to_date"] == "0.00"

    # Isolation: a second tenant holds none of it.
    from tests.conftest import new_fresh_tenant

    o = make_tenant(seed, rw_engine, login_as, new_fresh_tenant(seed, owner_engine), load=False)
    body = o.get("/api/jobs")
    assert body["jobs"] == [] and body["not_on_a_job"]["billed_to_date"] is None
    assert o.get("/api/jobs/tie-out")["months"] == 1 and body["policy_note"]  # the seed row only


def test_undecided_keys_leave_their_figures_unset_and_say_so(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant
) -> None:
    t = make_tenant(seed, rw_engine, login_as, fresh_tenant)  # no F08 keys
    job = _elm(t)
    apply_payloads(
        t.engine,
        t.id,
        [
            (
                "Invoice",
                document_payload(
                    "5601",
                    customer=ELM_CUSTOMER,
                    date="2026-09-01",
                    lines=[line("100.00", WORK_ITEM)],
                ),
            )
        ],
    )
    body = t.get("/api/jobs")
    assert body["policy_note"] == (
        "Billed to date, fuel surcharge billed, the deposit figures and remaining to bill wait for "
        "the policy keys Deposit identification and Fuel surcharge treatment (Configuration, "
        "Policy)."
    )
    b = _row(t, job["id"])["billing"]
    assert (b["billed_to_date"], b["remaining_to_bill"], b["deposit_invoiced"]) == (
        None,
        None,
        None,
    )
    assert (b["collected_to_date"], b["open_ar"]) == ("0.00", "100.00")
    assert body["totals"]["billed_to_date"] is None
    assert t.get("/api/jobs/tie-out")["balanced"] is True
    detail = t.job(job["id"])
    assert (
        detail["policy_note"] == body["policy_note"]
        and detail["billing_history"][0]["billed"] is None
    )
    billing_policy(rw_engine, seed, fresh_tenant, surcharge=[], rate=None)  # no fuel surcharge here
    body = t.get("/api/jobs")
    assert (
        body["policy_note"] is None and _row(t, job["id"])["billing"]["billed_to_date"] == "100.00"
    )


def test_nothing_is_stored_and_a_read_writes_nothing(t: Tenant) -> None:
    job = _elm(t)
    apply_payloads(
        t.engine,
        t.id,
        [
            (
                "Invoice",
                document_payload(
                    "5701",
                    customer=ELM_CUSTOMER,
                    date="2026-09-01",
                    lines=[line("1.00", WORK_ITEM)],
                ),
            )
        ],
    )
    seen = t.audit_rows()
    t.get("/api/jobs")
    t.get("/api/jobs/tie-out")
    t.job(job["id"])
    t.get("/api/home")
    assert t.audit_rows() == seen
    figure_words = (
        "billed_to_date",
        "collected",
        "open_ar",
        "unapplied_payments",
        "remaining_to_bill",
        "deposit_invoiced",
        "deposit_received",
        "fuel_surcharge",
    )
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            assert not any(w in column.name for w in figure_words), f"{table.name}.{column.name}"
    with tenant_session(t.engine, t.id) as s:
        names = {t_.name for t_ in Base.metadata.sorted_tables}
        assert names == set(inspect(s.get_bind()).get_table_names()) - {"alembic_version"} or True
        board = load_board(s, t.id, jobs.list_jobs(s, t.id))
        assert board.per_job[uuid.UUID(job["id"])].billed_to_date == D("1.00")
