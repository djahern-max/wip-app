"""F05: the pure normalizers against the recorded sandbox fixtures. No database."""

from decimal import Decimal

import pytest

from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.billing import normalize_billing
from app.integrations.qbo.normalize.customer import normalize_customer
from app.integrations.qbo.normalize.money import cents
from app.integrations.qbo.normalize.payment import normalize_payment, payment_from_sales_receipt
from tests.qbo_fixtures import fixture, record, records

D = Decimal


# --- money ---------------------------------------------------------------------------------------


def test_cents_keeps_two_decimals_exactly_and_refuses_more() -> None:
    assert (
        cents(D("100.1"), field="x") == D("100.10")
        and str(cents(D("100.1"), field="x")) == "100.10"
    )
    assert cents(7, field="x") == D("7.00")
    for bad in (D("1.005"), "1.00", 1.0, True, None):
        with pytest.raises(Unreadable):
            cents(bad, field="x")
    with pytest.raises(Unreadable) as err:
        cents(D("0.001"), field="total")
    assert err.value.reason == "total_more_than_two_decimals"


# --- customer -----------------------------------------------------------------------------------


def test_project_is_isproject_true_with_a_parent_and_a_sub_customer_is_not() -> None:
    rows = [normalize_customer(c) for c in records("Customer")]
    projects = [r for r in rows if r.is_project]
    subs = [r for r in rows if not r.is_project and r.parent_external_id]
    assert len(projects) == 1 and len(subs) == 3  # S-01 (a)
    assert projects[0].parent_external_id is not None
    parent_ids = {r.external_id for r in rows}
    assert all(r.parent_external_id in parent_ids for r in projects + subs)
    assert all(r.active for r in rows)  # the sample company has no inactive customer
    with pytest.raises(Unreadable):
        normalize_customer({"Id": "1"})


# --- billing ------------------------------------------------------------------------------------


@pytest.mark.parametrize("entity", ["Invoice", "CreditMemo", "SalesReceipt"])
def test_every_fixture_document_normalizes_and_the_total_identity_holds(entity: str) -> None:
    for payload in records(entity):
        row = normalize_billing(entity, payload)
        assert row.subtotal - row.discount_total + row.tax_total == row.total, payload["Id"]
        assert row.total == cents(payload["TotalAmt"], field="t")
        assert row.customer_external_id == payload["CustomerRef"]["value"]
        assert all(line.line_kind != "SubTotalLineDetail" for line in row.lines)
        assert [line.line_no for line in row.lines] == list(range(1, len(row.lines) + 1))


def test_discount_is_its_own_total_and_its_own_line() -> None:
    row = normalize_billing("Invoice", record("Invoice", "39"))
    assert row.discount_total > 0
    discount_lines = [line for line in row.lines if line.line_kind == "DiscountLineDetail"]
    assert len(discount_lines) == 1 and discount_lines[0].amount == row.discount_total
    items = sum(line.amount for line in row.lines if line.line_kind == "SalesItemLineDetail")
    assert items == row.subtotal  # the SubTotal line is before the discount
    assert row.subtotal - row.discount_total + row.tax_total == row.total


def test_an_invoice_with_only_a_subtotal_line_takes_the_subtotal_line_and_has_no_lines() -> None:
    row = normalize_billing("Invoice", record("Invoice", "42"))
    assert row.lines == () and row.subtotal > 0 and row.subtotal + row.tax_total == row.total


def test_with_no_lines_at_all_subtotal_is_total_less_tax() -> None:
    payload = record("Invoice", "42") | {"Line": []}
    row = normalize_billing("Invoice", payload)
    assert row.subtotal == row.total - row.tax_total and row.lines == ()


def test_void_needs_zero_total_the_note_and_an_earlier_nonzero_version() -> None:
    voided = record("Invoice", "129")
    assert normalize_billing("Invoice", voided).voided is False  # always 0.00 as far as we know
    assert normalize_billing("Invoice", voided, had_nonzero_total_before=True).voided is True
    before = fixture("invoice_before_void")
    assert normalize_billing("Invoice", before, had_nonzero_total_before=True).voided is False
    no_note = {k: v for k, v in voided.items() if k != "PrivateNote"}
    assert normalize_billing("Invoice", no_note, had_nonzero_total_before=True).voided is False


def test_credit_memo_and_sales_receipt_kinds() -> None:
    cm = normalize_billing("CreditMemo", records("CreditMemo")[0])
    assert cm.kind == "credit_memo" and cm.total > 0  # stored positive; signed elsewhere
    sr = normalize_billing("SalesReceipt", records("SalesReceipt")[0])
    assert sr.kind == "sales_receipt" and sr.deposit_account_external_id


def test_unreadable_payloads_name_a_reason_and_no_value() -> None:
    base = record("Invoice", "39")
    cases = {
        "total_more_than_two_decimals": base | {"TotalAmt": D("1.005")},
        "total_identity_fails": base | {"TotalAmt": base["TotalAmt"] + 1},
        "customer_missing": {k: v for k, v in base.items() if k != "CustomerRef"},
        "txn_date_missing": {k: v for k, v in base.items() if k != "TxnDate"},
        "foreign_currency": base | {"ExchangeRate": D("1.35")},
        "line_kind_missing": base | {"Line": [{"Amount": 1}]},
    }
    for reason, payload in cases.items():
        with pytest.raises(Unreadable) as err:
            normalize_billing("Invoice", payload)
        assert err.value.reason == reason
        assert str(base["TotalAmt"]) not in str(err.value)
    with pytest.raises(Unreadable):
        normalize_billing("Bill", base)


# --- payment ------------------------------------------------------------------------------------


def test_every_fixture_payment_normalizes_with_one_application_per_linked_txn() -> None:
    for payload in records("Payment"):
        row = normalize_payment(payload)
        links = [lt for line in payload["Line"] for lt in line.get("LinkedTxn", [])]
        assert len(row.applications) == len(links)
        assert row.total == cents(payload["TotalAmt"], field="t")


def test_a_credit_memo_application_is_stored_positive_as_given() -> None:
    row = normalize_payment(record("Payment", "74"))
    kinds = {a.linked_txn_type: a.amount for a in row.applications}
    assert kinds == {"Invoice": D("100.00"), "CreditMemo": D("100.00")}
    assert row.total == D("0.00") and row.deposit_account_external_id is None


def test_a_line_with_several_linked_txns_is_refused_not_guessed() -> None:
    payload = record("Payment", "74")
    line = dict(payload["Line"][0])
    line["LinkedTxn"] = [line["LinkedTxn"][0], {"TxnId": "1", "TxnType": "Invoice"}]
    with pytest.raises(Unreadable) as err:
        normalize_payment(payload | {"Line": [line]})
    assert err.value.reason == "line_with_several_linked_txns"


def test_sales_receipt_yields_a_fully_applied_payment_of_its_own_kind() -> None:
    receipt = normalize_billing("SalesReceipt", records("SalesReceipt")[0])
    paid = payment_from_sales_receipt(receipt)
    assert paid.kind == "sales_receipt" and paid.external_id == receipt.external_id
    assert paid.total == receipt.total and paid.unapplied_amount == D("0.00")
    assert len(paid.applications) == 1
    app_ = paid.applications[0]
    assert (app_.linked_txn_type, app_.linked_txn_external_id, app_.amount) == (
        "SalesReceipt",
        receipt.external_id,
        receipt.total,
    )
    with pytest.raises(Unreadable):
        payment_from_sales_receipt(normalize_billing("Invoice", record("Invoice", "39")))
