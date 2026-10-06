"""F08 tie-out (BLUEPRINT §8.5 item 1, billing side; owner's point 2, 2026-10-06: built on
the Intuit sandbox fixture, D-25). The whole sample company through the worker, then
for every month: jobs + not on a job = the Connections month totals for billed
(invoices − credit memos + sales receipts) and for collected (payments + sales
receipts), to the cent; the same after a customer is linked to a job, and after a
document is moved to an untracked row."""

import uuid
from collections.abc import Callable
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.core.db import tenant_session
from app.domain.billing.board import load_board, month_sums, not_on_a_job, on_a_job, tie_out
from app.domain.billing.figures import months_billed, months_collected
from app.domain.billing.models import Billing
from app.domain.jobs import service as jobs
from tests.billing_helpers import billing_policy
from tests.conftest import CSRF, Seed
from tests.job_helpers import ELM_ID, make_tenant
from tests.qbo_fixtures import fixture, record

D = Decimal


# The F07 helpers seed one 1,000.00 invoice dated 2026-08-31 on the untracked 1701 Ocean
# project (``seed_customers``); it is outside the oracle and is money not on a job.
SEED_ROW = ("2026-08", D("1000.00"))


def _oracle() -> dict[str, tuple[Decimal, Decimal]]:
    out = {}
    for o in fixture("oracle_month_totals"):
        billed = D(o["invoices"]) + D(o["credit_memos"]) + D(o["sales_receipts"])
        collected = D(o["payments"]) + D(o["sales_receipts"])
        out[o["month"]] = (billed, collected)
    month, amount = SEED_ROW
    billed, collected = out.get(month, (D("0.00"), D("0.00")))
    out[month] = (billed + amount, collected)
    return out


def _check(engine: Engine, tenant_id: uuid.UUID) -> list:
    with tenant_session(engine, tenant_id) as s:
        board = load_board(s, tenant_id, jobs.list_jobs(s, tenant_id))
        rows = tie_out(s, tenant_id)
        # F08.2: the tie-out's month sums come from the database; they must equal the
        # per-job sums the pure figures keep, and the not-on-a-job row's own sums.
        jobs_b, jobs_c = month_sums(s, on_a_job)
        other_b, other_c = month_sums(s, not_on_a_job)
    assert board.not_on_a_job is not None

    def nonzero(d: dict) -> dict:
        return {m: v for m, v in d.items() if v != D("0.00")}

    assert jobs_b == months_billed(list(board.per_job.values()))
    assert jobs_c == nonzero(months_collected(list(board.per_job.values())))
    assert other_b == board.not_on_a_job.billed_by_month
    assert other_c == nonzero(board.not_on_a_job.collected_by_month)
    oracle = _oracle()
    assert [r.month for r in rows] == sorted(oracle)
    for r in rows:
        billed, collected = oracle[r.month]
        assert r.jobs_billed + r.other_billed == billed == r.ledger_billed, r.month
        assert r.jobs_collected + r.other_collected == collected == r.ledger_collected, r.month
        assert r.balanced
    return rows


def test_jobs_plus_not_on_a_job_equal_the_month_totals_for_every_month(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant: uuid.UUID
) -> None:
    from tests.qbo_helpers import FakeIntuit, FixtureCompany, connect_directly, installed
    from tests.test_qbo_tasks import _drain

    t = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    fake = FakeIntuit()
    FixtureCompany().serve(fake)
    with installed(fake):
        connect_directly(rw_engine, fresh_tenant, fake)
        assert t.client.post("/api/qbo/backfill", headers=CSRF).status_code == 200
        _drain(rw_engine, fresh_tenant)
    # The sandbox items are the fixture's own; two of them stand in for the Rye Beach
    # deposit and fuel items (the tie-out reads whole documents; the items do not matter).
    billing_policy(rw_engine, seed, fresh_tenant, deposit=["5"], surcharge=["8"])

    # No job yet: everything is not on a job and the tie holds.
    rows = _check(rw_engine, fresh_tenant)
    assert all(r.jobs_billed == D("0.00") and r.jobs_collected == D("0.00") for r in rows)
    body = t.get("/api/jobs")
    assert "tie_out" not in body  # F08.2: its own request
    tie = t.get("/api/jobs/tie-out")
    assert tie["balanced"] and tie["months"] == len(rows) and tie["months_off"] == []
    assert tie["status"].startswith("Ties to the cent")
    assert body["jobs"] == []

    # Link the sandbox customer with the most invoices to a job: its money moves to the
    # job's row and the tie still holds, month by month.
    invoices = fixture("Invoice")
    counts: dict[str, int] = {}
    for inv in invoices:
        counts[inv["CustomerRef"]["value"]] = counts.get(inv["CustomerRef"]["value"], 0) + 1
    busiest = max(counts, key=lambda k: counts[k])
    assert record("Customer", busiest)  # it is a real sandbox row
    job = t.new_job(ELM_ID)
    t.send(
        "POST",
        f"/api/jobs/{job['id']}/aliases",
        {"system": "qbo_customer", "external_id": busiest, "set_in_progress": True},
    )
    rows = _check(rw_engine, fresh_tenant)
    assert any(r.jobs_billed != D("0.00") for r in rows)
    row = next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job["id"])
    with tenant_session(rw_engine, fresh_tenant) as s:
        held = [
            (b.kind, b.balance)
            for b in s.execute(
                select(Billing).where(Billing.customer_external_id == busiest)
            ).scalars()
            if b.deleted_at is None and not b.voided
        ]
    open_ar = sum((bal if kind != "credit_memo" else -bal for kind, bal in held), D(0))
    assert D(row["billing"]["open_ar"]) == open_ar
    assert t.get("/api/jobs/tie-out")["balanced"]

    # Move one of its documents to an untracked customer: the amount moves to not on a
    # job and the tie still holds.
    moved = dict(next(inv for inv in invoices if inv["CustomerRef"]["value"] == busiest))
    other = next(
        c["Id"] for c in fixture("Customer") if c["Id"] != busiest and not c.get("ParentRef")
    )
    moved["CustomerRef"] = {"value": other}
    from tests.billing_helpers import apply_payloads

    apply_payloads(rw_engine, fresh_tenant, [("Invoice", moved)])
    after = next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job["id"])
    assert D(after["billing"]["billed_to_date"]) <= D(row["billing"]["billed_to_date"])
    _check(rw_engine, fresh_tenant)

    # F08.2 item 1 (D-41), the constructed case: the cause found on rye-beach, a
    # journal-entry credit applied to the job's invoice through a zero payment. Before
    # D-41 the month was off by twice the credit (the first commit's diagnostic test);
    # now the payment adds its cash, 0.00, the credit is "other credits applied" on the
    # job, and every month still ties on both sides.
    from tests.billing_helpers import payment_payload

    target = next(
        inv
        for inv in invoices
        if inv["CustomerRef"]["value"] == busiest and inv["Id"] != moved["Id"]
    )
    credit = payment_payload(
        "9901",
        customer=busiest,
        date=target["TxnDate"],
        total="0",
        applied=[("25.00", "Invoice", target["Id"]), ("25.00", "JournalEntry", "JE9901")],
    )
    apply_payloads(rw_engine, fresh_tenant, [("Payment", credit)])
    rows = _check(rw_engine, fresh_tenant)
    assert all(r.balanced for r in rows)
    later = next(j for j in t.get("/api/jobs")["jobs"] if j["id"] == job["id"])
    assert D(later["billing"]["collected_to_date"]) == D(after["billing"]["collected_to_date"])
    assert later["billing"]["other_credits_applied"] == "25.00"
    assert [i["code"] for i in later["attention"] if i["code"] == "PAYMENT_OTHER_CREDIT"] == [
        "PAYMENT_OTHER_CREDIT"
    ]
    assert t.get("/api/jobs/tie-out")["balanced"]
