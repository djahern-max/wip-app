"""F08.2 item 2: the board's reads do not grow with the tenant's history. The number
of SQL statements ``GET /api/jobs``, ``GET /api/jobs/{id}``, ``GET /api/jobs/tie-out``
and the two exports issue is the same with 10 and with 1,000 documents and payments
on untracked rows, and the figures summed by the database equal what the rows hold.
The rows are inserted directly (what is under test is the count, not the normalizer)."""

import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

from app.core.db import tenant_session
from app.domain.billing.models import Billing, BillingLine, Payment, PaymentApplication
from tests.billing_helpers import billing_policy
from tests.conftest import Seed
from tests.job_helpers import ELM_ID, Tenant, _raw, make_tenant

D = Decimal
OCEAN_SEED = D("1000.00")  # the F07 helpers' invoice on the untracked 1701 Ocean row
MONTHS = [(y, m) for y in range(2001, 2027) for m in range(1, 13)][:308]


def _seed(engine: Engine, tenant_id: uuid.UUID, customer: dict, n: int, start: int) -> None:
    with tenant_session(engine, tenant_id) as s:
        raw_id = _raw(s, tenant_id)
        for i in range(start, start + n):
            y, m = MONTHS[i % len(MONTHS)]
            when = date(y, m, 15)
            b = Billing(
                tenant_id=tenant_id,
                kind="invoice",
                source="qbo",
                external_id=f"L{i}",
                txn_date=when,
                customer_external_id=customer["external_id"],
                customer_id=customer["id"],
                subtotal=D("100.00"),
                discount_total=D("0.00"),
                tax_total=D("0.00"),
                total=D("100.00"),
                balance=D("0.00"),
                voided=False,
                raw_record_id=raw_id,
            )
            s.add(b)
            s.flush()
            s.add(
                BillingLine(
                    tenant_id=tenant_id,
                    billing_id=b.id,
                    line_no=1,
                    line_kind="SalesItemLineDetail",
                    item_external_id="1",
                    amount=D("100.00"),
                )
            )
            p = Payment(
                tenant_id=tenant_id,
                kind="payment",
                source="qbo",
                external_id=f"LP{i}",
                txn_date=when,
                customer_external_id=customer["external_id"],
                customer_id=customer["id"],
                total=D("100.00"),
                unapplied_amount=D("0.00"),
                raw_record_id=raw_id,
            )
            s.add(p)
            s.flush()
            s.add(
                PaymentApplication(
                    tenant_id=tenant_id,
                    payment_id=p.id,
                    line_no=1,
                    linked_txn_type="Invoice",
                    linked_txn_external_id=f"L{i}",
                    billing_id=b.id,
                    amount=D("100.00"),
                )
            )


def _statements(client: TestClient, path: str) -> int:
    count = 0

    def on_exec(conn, cursor, statement, parameters, context, executemany):
        nonlocal count
        count += 1

    event.listen(Engine, "before_cursor_execute", on_exec)
    try:
        r = client.get(path)
    finally:
        event.remove(Engine, "before_cursor_execute", on_exec)
    assert r.status_code == 200, (path, r.text[:200])
    return count


def test_statement_counts_are_the_same_at_10_and_at_1000_documents(
    seed: Seed, rw_engine: Engine, login_as: Callable[..., TestClient], fresh_tenant: uuid.UUID
) -> None:
    t: Tenant = make_tenant(seed, rw_engine, login_as, fresh_tenant)
    billing_policy(rw_engine, seed, fresh_tenant)
    job = t.new_job(ELM_ID)
    t.link(job["id"], "elm", in_progress=True)
    untracked = t.customers["ocean"]
    paths = ("/api/jobs", f"/api/jobs/{job['id']}", "/api/jobs/tie-out", "/api/jobs/export.xlsx")

    _seed(rw_engine, fresh_tenant, untracked, 10, 0)
    at_10 = {p: _statements(t.client, p) for p in paths}
    body = t.get("/api/jobs")
    assert body["not_on_a_job"]["billed_to_date"] == str(OCEAN_SEED + D("1000.00"))
    assert body["not_on_a_job"]["collected_to_date"] == "1000.00"

    _seed(rw_engine, fresh_tenant, untracked, 990, 10)
    at_1000 = {p: _statements(t.client, p) for p in paths}
    assert at_1000 == at_10, (at_10, at_1000)
    body = t.get("/api/jobs")
    assert body["not_on_a_job"]["billed_to_date"] == str(OCEAN_SEED + D("100000.00"))
    assert body["not_on_a_job"]["collected_to_date"] == "100000.00"
    assert body["not_on_a_job"]["open_ar"] == str(OCEAN_SEED)  # the seed row is unpaid
    tie = t.get("/api/jobs/tie-out")
    assert tie["balanced"] and tie["months"] == 308  # 2026-08's seed row is inside the span
    # The job's own figures are untouched by rows on other customers.
    row = next(j for j in body["jobs"] if j["id"] == job["id"])
    assert (
        row["billing"]["billed_to_date"] == "0.00" and row["billing"]["collected_to_date"] == "0.00"
    )
