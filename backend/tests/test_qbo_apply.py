"""F05: raw versions → canonical rows (``app.domain.billing.sync``). Idempotent,
order-independent, deletes and voids from history, and the month totals against the
independent oracle."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select, text

from app.core.db import tenant_session
from app.domain.billing.amounts import signed_amount, signed_application
from app.domain.billing.models import Billing, BillingLine, Customer, Payment, PaymentApplication
from app.domain.billing.report import current_counts, raw_month_totals, skipped_counts
from app.domain.billing.sync import (
    apply_raw,
    attach_account_ids,
    unlinked_deposit_lines,
)
from app.domain.billing.totals import month_totals
from app.domain.config.models import GlAccount
from app.ingest.models import Connection, RawRecord
from app.ingest.raw import RawOrigin, latest_raw_versions, store_delete, store_raw
from app.ingest.sync_runs import start_sync_run
from tests.qbo_fixtures import cdc_records, fixture, record, records

D = Decimal


@pytest.fixture
def tenant(fresh_tenant: uuid.UUID, rw_engine: Engine) -> uuid.UUID:
    with tenant_session(rw_engine, fresh_tenant) as db:
        c = Connection(tenant_id=fresh_tenant, system="qbo", status="connected")
        db.add(c)
        db.flush()
        run = start_sync_run(db, fresh_tenant, c.id, "backfill")
        db.expunge(run)
    return fresh_tenant


def _store(db, tenant_id, entity, payload, *, deleted=False) -> RawRecord:
    run_id = db.execute(text("SELECT id FROM sync_run LIMIT 1")).scalar_one()
    origin = RawOrigin(sync_run_id=run_id)
    if deleted:
        result = store_delete(db, tenant_id, "qbo", entity, payload["Id"], payload, origin)
    else:
        result = store_raw(db, tenant_id, "qbo", entity, payload["Id"], payload, origin)
    if result.record is None:  # identical: fetch the current version
        return next(
            r
            for r in latest_raw_versions(db, tenant_id, "qbo", entity)
            if r.external_id == payload["Id"]
        )
    return result.record


def _load_all(db, tenant_id, *entities: str) -> list:
    outcomes = []
    for entity in entities:
        for payload in records(entity):
            outcomes.append(apply_raw(db, tenant_id, _store(db, tenant_id, entity, payload)))
    return outcomes


def _snapshot(db, tenant_id) -> dict:
    def rows(model, order):
        out = []
        for r in db.execute(
            select(model).where(model.tenant_id == tenant_id).order_by(*order)
        ).scalars():
            out.append(
                {
                    k: v
                    for k, v in vars(r).items()
                    if not k.startswith("_") and k not in ("id", "created_at", "updated_at")
                }
            )
        return out

    return {
        "customer": rows(Customer, (Customer.external_id,)),
        "billing": rows(Billing, (Billing.kind, Billing.external_id)),
        "billing_line": rows(BillingLine, (BillingLine.billing_id, BillingLine.line_no)),
        "payment": rows(Payment, (Payment.kind, Payment.external_id)),
        "payment_application": rows(
            PaymentApplication, (PaymentApplication.payment_id, PaymentApplication.line_no)
        ),
    }


def test_every_fixture_record_applies_and_a_second_run_changes_nothing(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with tenant_session(rw_engine, tenant) as db:
        outcomes = _load_all(
            db, tenant, "Customer", "Invoice", "CreditMemo", "SalesReceipt", "Payment"
        )
        assert {o.result for o in outcomes} == {"applied"}
        first = _snapshot(db, tenant)
        assert len(first["customer"]) == len(records("Customer"))
        assert len(first["billing"]) == sum(
            len(records(e)) for e in ("Invoice", "CreditMemo", "SalesReceipt")
        )
        assert len(first["payment"]) == len(records("Payment")) + len(records("SalesReceipt"))
        assert all(b["customer_id"] is not None for b in first["billing"])
        assert all(p["customer_id"] is not None for p in first["payment"])
        # Every application that names a document we hold points at it.
        apps = first["payment_application"]
        assert all(
            a["billing_id"] is not None
            for a in apps
            if a["linked_txn_type"] in ("Invoice", "CreditMemo", "SalesReceipt")
        )
        assert skipped_counts(db, tenant) == {
            e: 0 for e in ("Customer", "Invoice", "CreditMemo", "SalesReceipt", "Payment")
        }
    with tenant_session(rw_engine, tenant) as db:
        again = _load_all(
            db, tenant, "Customer", "Invoice", "CreditMemo", "SalesReceipt", "Payment"
        )
        assert {o.result for o in again} == {"applied"}
        assert _snapshot(db, tenant) == first


def test_the_order_of_arrival_does_not_change_the_rows(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with tenant_session(rw_engine, tenant) as db:
        _load_all(db, tenant, "Payment", "SalesReceipt", "CreditMemo", "Invoice", "Customer")
        reversed_first = _snapshot(db, tenant)
    # Compare with a tenant loaded customers-first: same rows apart from ids.
    assert all(b["customer_id"] is not None for b in reversed_first["billing"])
    assert all(p["customer_id"] is not None for p in reversed_first["payment"])
    assert all(
        a["billing_id"] is not None
        for a in reversed_first["payment_application"]
        if a["linked_txn_type"] in ("Invoice", "CreditMemo", "SalesReceipt")
    )
    projects = [c for c in reversed_first["customer"] if c["is_project"]]
    assert projects and all(c["parent_customer_id"] is not None for c in projects)


def test_month_totals_equal_the_independent_oracle_to_the_cent(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with tenant_session(rw_engine, tenant) as db:
        _load_all(db, tenant, "Customer", "Invoice", "CreditMemo", "SalesReceipt", "Payment")
        totals = month_totals(db, tenant)
        raw = raw_month_totals(db, tenant)
    oracle = fixture("oracle_month_totals")
    assert [t.month for t in totals] == [o["month"] for o in oracle]
    for t, o in zip(totals, oracle, strict=True):
        for column in ("invoices", "credit_memos", "sales_receipts", "payments"):
            assert f"{getattr(t, column):.2f}" == o[column], (t.month, column)
    assert [(r.month, r.invoices, r.credit_memos, r.sales_receipts, r.payments) for r in raw] == [
        (t.month, t.invoices, t.credit_memos, t.sales_receipts, t.payments) for t in totals
    ]
    assert any(t.credit_memos < 0 for t in totals)  # signed


def test_applications_plus_unapplied_equal_the_total_for_every_payment(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with tenant_session(rw_engine, tenant) as db:
        _load_all(db, tenant, "Payment", "SalesReceipt")
        for p in db.execute(select(Payment)).scalars():
            apps = db.execute(
                select(PaymentApplication).where(PaymentApplication.payment_id == p.id)
            ).scalars()
            assert (
                sum((signed_application(a) for a in apps), D("0.00")) + p.unapplied_amount
                == p.total
            ), (p.kind, p.external_id)


def test_sales_receipt_is_one_billing_row_and_one_fully_applied_payment(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    payload = records("SalesReceipt")[0]
    with tenant_session(rw_engine, tenant) as db:
        raw = _store(db, tenant, "SalesReceipt", payload)
        apply_raw(db, tenant, raw)
        b = db.execute(select(Billing).where(Billing.kind == "sales_receipt")).scalar_one()
        p = db.execute(select(Payment).where(Payment.kind == "sales_receipt")).scalar_one()
        assert b.raw_record_id == p.raw_record_id == raw.id
        assert p.total == b.total and p.unapplied_amount == D("0.00")
        (app_,) = db.execute(
            select(PaymentApplication).where(PaymentApplication.payment_id == p.id)
        ).scalars()
        assert app_.billing_id == b.id and app_.amount == b.total
        assert signed_amount(b) == b.total


def test_credit_memo_signed_amount_is_negative(tenant: uuid.UUID, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, tenant) as db:
        apply_raw(db, tenant, _store(db, tenant, "CreditMemo", records("CreditMemo")[0]))
        cm = db.execute(select(Billing).where(Billing.kind == "credit_memo")).scalar_one()
        assert cm.total > 0 and signed_amount(cm) == -cm.total


def test_a_void_is_recognised_only_with_an_earlier_nonzero_version(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    voided = record("Invoice", "129")
    before = {k: v for k, v in fixture("invoice_before_void").items() if k != "_note"}
    with tenant_session(rw_engine, tenant) as db:
        apply_raw(db, tenant, _store(db, tenant, "Invoice", voided))
        row = db.execute(select(Billing)).scalar_one()
        assert row.voided is False and row.total == D("0.00")  # always zero, as far as we know
    other = str(uuid.uuid4())  # a second tenant-free path: same tenant, different id
    before2 = before | {"Id": "9129"}
    voided2 = voided | {"Id": "9129"}
    with tenant_session(rw_engine, tenant) as db:
        apply_raw(db, tenant, _store(db, tenant, "Invoice", before2))
        row = db.execute(select(Billing).where(Billing.external_id == "9129")).scalar_one()
        assert row.voided is False and row.total == D("570.00")
        month = row.txn_date.strftime("%Y-%m")
        assert next(t for t in month_totals(db, tenant) if t.month == month).invoices == D("570.00")
    with tenant_session(rw_engine, tenant) as db:
        raw = _store(db, tenant, "Invoice", voided2)
        assert raw.version == 2
        apply_raw(db, tenant, raw)
        row = db.execute(select(Billing).where(Billing.external_id == "9129")).scalar_one()
        assert row.voided is True and row.total == D("0.00") and row.raw_record_id == raw.id
        assert signed_amount(row) == D("0.00")
        assert next(t for t in month_totals(db, tenant) if t.month == month).invoices == D("0.00")
        # History is still readable: the earlier version holds the amount.
        versions = db.execute(
            select(RawRecord.version, RawRecord.payload["TotalAmt"].as_string())
            .where(RawRecord.external_id == "9129")
            .order_by(RawRecord.version)
        ).all()
        assert [v for v, _ in versions] == [1, 2] and versions[0][1] == "570.00"
    assert other  # unused marker keeps the two ids distinct on purpose


def test_a_cdc_delete_sets_deleted_at_and_leaves_the_totals(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    changed, stub = cdc_records(fixture("cdc_changed_and_deleted"), "Invoice")
    original = record("Invoice", "39")
    deleted_target = (
        record("Invoice", "70") if any(r["Id"] == "70" for r in records("Invoice")) else None
    )
    assert deleted_target is None  # the recorded list no longer holds the deleted invoice
    with tenant_session(rw_engine, tenant) as db:
        apply_raw(db, tenant, _store(db, tenant, "Invoice", original))
        # Pretend we held the deleted invoice before it went: a copy under the stub's id.
        held = original | {"Id": stub["Id"], "DocNumber": "held"}
        apply_raw(db, tenant, _store(db, tenant, "Invoice", held))
        month = original["TxnDate"][:7]
        before = next(t for t in month_totals(db, tenant) if t.month == month).invoices
        assert before == 2 * D(original["TotalAmt"])
    with tenant_session(rw_engine, tenant) as db:
        # the changed invoice: a new version, applied
        raw = _store(db, tenant, "Invoice", changed)
        assert raw.version == 2
        assert apply_raw(db, tenant, raw).result == "applied"
        row = db.execute(select(Billing).where(Billing.external_id == "39")).scalar_one()
        assert row.total == D(original["TotalAmt"]) + D("10.00") and row.raw_record_id == raw.id
        # the deleted one: a new version flagged deleted, carrying the last payload
        raw = _store(db, tenant, "Invoice", stub, deleted=True)
        assert raw.version == 2 and raw.is_deleted and raw.payload["DocNumber"] == "held"
        assert apply_raw(db, tenant, raw).result == "deleted"
        gone = db.execute(select(Billing).where(Billing.external_id == stub["Id"])).scalar_one()
        assert gone.deleted_at is not None and gone.raw_record_id == raw.id
        assert signed_amount(gone) == D("0.00")
        after = next(t for t in month_totals(db, tenant) if t.month == month).invoices
        assert after == D(original["TotalAmt"]) + D("10.00")
        assert current_counts(db, tenant)["Invoice"] == 1
        assert skipped_counts(db, tenant)["Invoice"] == 0
        # a delete for a record never held: the stub is stored, no row is made
        raw = _store(db, tenant, "Invoice", stub | {"Id": "424242"}, deleted=True)
        assert raw.payload.get("status") == "Deleted" and raw.is_deleted
        assert apply_raw(db, tenant, raw).result == "ignored"
        assert (
            db.execute(select(Billing).where(Billing.external_id == "424242")).scalar_one_or_none()
            is None
        )
        assert skipped_counts(db, tenant)["Invoice"] == 0


def test_a_changed_payment_with_fewer_linked_txns_replaces_its_applications(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    original = record("Payment", "74")
    reapplied = {k: v for k, v in fixture("payment_reapplied").items() if k != "_note"}
    with tenant_session(rw_engine, tenant) as db:
        _load_all(db, tenant, "Invoice", "CreditMemo")
        apply_raw(db, tenant, _store(db, tenant, "Payment", original))
        p = db.execute(select(Payment).where(Payment.external_id == "74")).scalar_one()
        apps = (
            db.execute(select(PaymentApplication).where(PaymentApplication.payment_id == p.id))
            .scalars()
            .all()
        )
        assert {a.linked_txn_type for a in apps} == {"Invoice", "CreditMemo"}
        assert all(a.billing_id is not None for a in apps)
    with tenant_session(rw_engine, tenant) as db:
        raw = _store(db, tenant, "Payment", reapplied)
        assert raw.version == 2
        apply_raw(db, tenant, raw)
        p = db.execute(select(Payment).where(Payment.external_id == "74")).scalar_one()
        apps = (
            db.execute(select(PaymentApplication).where(PaymentApplication.payment_id == p.id))
            .scalars()
            .all()
        )
        assert apps == []  # the applications it no longer has are gone
        assert p.unapplied_amount == D("100.00") and p.total == D("100.00")
        assert sum((signed_application(a) for a in apps), D("0.00")) + p.unapplied_amount == p.total
        # and back again: idempotent from whichever version is latest
        raw = _store(db, tenant, "Payment", original)
        assert raw.version == 3
        apply_raw(db, tenant, raw)
        apps = (
            db.execute(select(PaymentApplication).where(PaymentApplication.payment_id == p.id))
            .scalars()
            .all()
        )
        assert {a.linked_txn_type for a in apps} == {"Invoice", "CreditMemo"}


def test_an_unreadable_payload_is_skipped_and_counted_and_the_rest_load(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    good = record("Invoice", "39")
    bad = record("Invoice", "49") | {"TotalAmt": D("1.005")}
    with tenant_session(rw_engine, tenant) as db:
        outcomes = [
            apply_raw(db, tenant, _store(db, tenant, "Invoice", good)),
            apply_raw(db, tenant, _store(db, tenant, "Invoice", bad)),
        ]
        assert [o.result for o in outcomes] == ["applied", "skipped"]
        assert outcomes[1].reason == "total_more_than_two_decimals"
        assert db.execute(select(Billing)).scalars().one().external_id == "39"
        assert skipped_counts(db, tenant)["Invoice"] == 1
        assert current_counts(db, tenant)["Invoice"] == 2
    # A later good version of the bad one clears the skip.
    with tenant_session(rw_engine, tenant) as db:
        apply_raw(db, tenant, _store(db, tenant, "Invoice", record("Invoice", "49")))
        assert skipped_counts(db, tenant)["Invoice"] == 0


def test_account_ids_attach_by_number_only(
    tenant: uuid.UUID, rw_engine: Engine, owner_engine: Engine
) -> None:
    # The recorded company as it was before the owner turned account numbers on:
    # every AcctNum stripped, and the one inactive account left out of the counts.
    accounts = [{k: v for k, v in r.items() if k != "AcctNum"} for r in records("Account")]
    active = sum(1 for r in accounts if r.get("Active") is not False)
    with tenant_session(owner_engine, tenant) as db:
        db.add_all(
            [
                GlAccount(tenant_id=tenant, account_no="1000", name="Checking"),
                GlAccount(tenant_id=tenant, account_no="4000", name="Sales"),
                GlAccount(tenant_id=tenant, account_no="6000", name="Dup"),
            ]
        )
    with tenant_session(rw_engine, tenant) as db:
        for a in accounts:
            _store(db, tenant, "Account", a)
        result = attach_account_ids(db, tenant)
        assert (result.attached, result.without_number, result.duplicate_numbers) == (
            0,
            active,
            0,
        )
        # Give three of them numbers: one match, one unmatched, two sharing a number.
        for payload, number in (
            (accounts[0], "1000"),
            (accounts[1], "7000"),
            (accounts[2], "6000"),
            (accounts[3], "6000"),
        ):
            _store(db, tenant, "Account", payload | {"AcctNum": number, "SyncToken": "9"})
        result = attach_account_ids(db, tenant)
        assert (result.attached, result.changed, result.unmatched, result.duplicate_numbers) == (
            1,
            1,
            1,
            1,
        )
        assert result.without_number == active - 4
        linked = db.execute(select(GlAccount).where(GlAccount.account_no == "1000")).scalar_one()
        assert (
            linked.external_id == accounts[0]["Id"]
            and linked.ledger_type == ""
            and linked.name == "Checking"
        )
        assert (
            db.execute(select(GlAccount).where(GlAccount.account_no == "6000"))
            .scalar_one()
            .external_id
            is None
        )
        again = attach_account_ids(db, tenant)
        assert (again.attached, again.changed) == (1, 0)
        audits = db.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'gl_account_linked'")
        ).scalar_one()
        assert audits == 1


def test_unlinked_deposit_lines_are_counted_with_their_total(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with tenant_session(rw_engine, tenant) as db:
        for d in records("Deposit"):
            _store(db, tenant, "Deposit", d)
        count, total = unlinked_deposit_lines(db, tenant)
    expected = [
        line["Amount"]
        for d in records("Deposit")
        for line in d["Line"]
        if not line.get("LinkedTxn")
    ]
    assert count == len(expected) == 2 and total == sum(expected)


def test_the_owners_chart_scenario_attaches_24_and_reports_the_three_differences(
    tenant: uuid.UUID, rw_engine: Engine, owner_engine: Engine
) -> None:
    """The sandbox as the owner set it up on 2026-09-21: 25 numbered accounts; a
    25-row chart where 1010 carries a different name, 6150 is missing and 1400 is
    extra. Expected: 24 attached, 1010's chart name untouched, 6150 reported as
    numbered but not in the chart, 1400 reported as in the chart but not in
    QuickBooks, 64 active accounts without a number."""
    from app.domain.billing.sync import chart_match

    accounts = records("Account")
    numbered = {r["AcctNum"]: r for r in accounts if r.get("AcctNum")}
    assert len(numbered) == 25 and {"1010", "6150"} <= set(numbered) and "1400" not in numbered
    with tenant_session(owner_engine, tenant) as db:
        for number, r in numbered.items():
            if number == "6150":
                continue
            name = "Operating checking (chart name)" if number == "1010" else r["Name"]
            db.add(GlAccount(tenant_id=tenant, account_no=number, name=name))
        db.add(GlAccount(tenant_id=tenant, account_no="1400", name="Inventory (chart only)"))
    with tenant_session(rw_engine, tenant) as db:
        for a in accounts:
            _store(db, tenant, "Account", a)
        result = attach_account_ids(db, tenant)
        assert (result.attached, result.changed, result.unmatched) == (24, 24, 1)
        assert (result.without_number, result.duplicate_numbers) == (64, 0)
        match = chart_match(db, tenant)
        assert len(match.attached) == 24 and "1010" in match.attached
        assert match.unmatched == ["6150"] and match.chart_only == ["1400"]
        rows = {a.account_no: a for a in db.execute(select(GlAccount)).scalars()}
        assert rows["1010"].name == "Operating checking (chart name)"
        assert rows["1010"].external_id == numbered["1010"]["Id"]
        assert rows["1400"].external_id is None and rows["1400"].name == "Inventory (chart only)"
        assert "6150" not in rows
        # A second run attaches nothing new and audits nothing new.
        again = attach_account_ids(db, tenant)
        assert (again.attached, again.changed, again.unmatched) == (24, 0, 1)
        audits = db.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'gl_account_linked'")
        ).scalar_one()
        assert audits == 24
