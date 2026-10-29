"""F10: the pure cost normalizer against the constructed fixture (its README is the oracle).
No database: the expense-side accounts and the items' expense accounts are handed in."""

from decimal import Decimal

import pytest

from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.cost import AccountSide, is_cost_entity, normalize_cost
from tests.cost_helpers import SIDE, fixture, record

D = Decimal


def _lines(entity: str, external_id: str, side: AccountSide = SIDE):
    return normalize_cost(entity, record(entity, external_id), side).lines


def test_the_five_entities_are_cost_entities_and_nothing_else_is() -> None:
    assert all(
        is_cost_entity(e) for e in ("Bill", "VendorCredit", "Purchase", "JournalEntry", "Deposit")
    )
    assert not any(
        is_cost_entity(e) for e in ("Invoice", "Payment", "Item", "Account", "TimeActivity")
    )
    with pytest.raises(Unreadable) as err:
        normalize_cost("Invoice", {"Id": "1"}, SIDE)
    assert err.value.reason == "not_a_cost_entity"


def test_a_bill_line_is_positive_with_its_account_vendor_and_line_customer() -> None:
    doc = normalize_cost("Bill", record("Bill", "98892"), SIDE)
    assert (doc.txn_type, doc.external_id, doc.doc_number) == ("Bill", "98892", "A-1041")
    assert doc.txn_date.isoformat() == "2026-09-04"
    assert doc.vendor_external_id == "7001" and doc.memo == "Ramp bill; receipt in Ramp"
    assert not doc.voided
    assert [
        (
            ln.line_external_id,
            ln.line_no,
            ln.account_external_id,
            ln.customer_external_id,
            ln.amount,
        )
        for ln in doc.lines
    ] == [
        ("1", 1, "304", "201", D("1250.00")),
        ("2", 2, "303", "201", D("480.50")),
    ]
    assert doc.lines[0].description == "Pour and finish, east apron"
    assert all(ln.vendor_external_id == "7001" for ln in doc.lines)
    assert all(
        str(ln.amount) == format(ln.amount, "f") and ln.amount == ln.amount.quantize(D("0.01"))
        for ln in doc.lines
    )


def test_a_line_with_no_customer_ref_keeps_no_customer() -> None:
    (ln,) = _lines("Bill", "98893")
    assert (
        ln.customer_external_id is None
        and ln.account_external_id == "304"
        and ln.amount == D("2000.00")
    )


def test_the_pool_bill_carries_the_pool_project_on_every_line() -> None:
    rows = _lines("Bill", "98890")
    assert len(rows) == 7 and all(ln.customer_external_id == "205" for ln in rows)
    assert sum((ln.amount for ln in rows), D("0.00")) == D("8120.00")
    assert [ln.line_no for ln in rows] == [1, 2, 3, 4, 5, 6, 7]


def test_an_item_based_line_takes_the_items_expense_account_or_no_account() -> None:
    stone, service = _lines("Bill", "98894")
    assert (stone.account_external_id, stone.amount, stone.customer_external_id) == (
        "303",
        D("640.00"),
        "201",
    )
    assert (service.account_external_id, service.amount, service.customer_external_id) == (
        None,
        D("90.00"),
        "201",
    )
    assert service.item_external_id == "602"
    # An item the copy does not hold: no account either, the line is kept.
    unknown = AccountSide(expense_side=SIDE.expense_side, item_accounts={})
    rows = _lines("Bill", "98894", unknown)
    assert [ln.account_external_id for ln in rows] == [None, None]
    # An item whose expense account is a balance-sheet account: not a cost line.
    asset = AccountSide(expense_side=SIDE.expense_side, item_accounts={"601": "308", "602": None})
    assert [ln.item_external_id for ln in _lines("Bill", "98894", asset)] == ["602"]


def test_a_vendor_credit_line_is_negative() -> None:
    doc = normalize_cost("VendorCredit", record("VendorCredit", "7701"), SIDE)
    (ln,) = doc.lines
    assert (ln.amount, ln.account_external_id, ln.customer_external_id) == (
        D("-150.00"),
        "304",
        "201",
    )
    assert doc.vendor_external_id == "7001" and doc.doc_number == "VC-11"


def test_a_purchase_is_positive_and_negative_when_credit_is_true() -> None:
    card = normalize_cost("Purchase", record("Purchase", "5001"), SIDE)
    assert card.vendor_external_id == "7002" and card.doc_number is None  # "" is no number
    assert [(ln.amount, ln.account_external_id, ln.customer_external_id) for ln in card.lines] == [
        (D("315.75"), "303", "201")
    ]
    check = normalize_cost("Purchase", record("Purchase", "5002"), SIDE)
    assert [(ln.amount, ln.account_external_id) for ln in check.lines] == [(D("-400.00"), "304")]
    assert check.doc_number == "1077"


def test_a_journal_entry_posts_debits_positive_credits_negative_and_keeps_only_expense_lines() -> (
    None
):
    doc = normalize_cost("JournalEntry", record("JournalEntry", "9001"), SIDE)
    assert doc.vendor_external_id is None and doc.memo == "Payroll accrual"
    assert [
        (ln.line_external_id, ln.account_external_id, ln.amount, ln.customer_external_id)
        for ln in doc.lines
    ] == [
        ("0", "301", D("12500.00"), None),
        ("1", "302", D("9800.00"), None),
    ]  # the credit to accrued payroll (306) is a balance-sheet line and is not stored
    # A credit to an expense account is negative; the vendor is the line's Entity when it
    # is a vendor, the customer when it is a customer (Plan answer 3).
    payload = record("JournalEntry", "9001")
    payload["Line"][0]["JournalEntryLineDetail"]["PostingType"] = "Credit"
    payload["Line"][0]["JournalEntryLineDetail"]["Entity"] = {
        "Type": "Vendor",
        "EntityRef": {"value": "7001"},
    }
    payload["Line"][1]["JournalEntryLineDetail"]["Entity"] = {
        "Type": "Customer",
        "EntityRef": {"value": "201"},
    }
    doc = normalize_cost("JournalEntry", payload, SIDE)
    assert doc.lines[0].amount == D("-12500.00") and doc.lines[0].vendor_external_id == "7001"
    assert doc.lines[0].customer_external_id is None
    assert doc.lines[1].customer_external_id == "201" and doc.lines[1].vendor_external_id is None


def test_a_deposit_line_to_an_expense_account_is_negative_and_the_bank_line_is_never_read() -> None:
    doc = normalize_cost("Deposit", record("Deposit", "3301"), SIDE)
    (ln,) = doc.lines
    assert (ln.amount, ln.account_external_id, ln.customer_external_id, ln.line_external_id) == (
        D("-275.00"),
        "304",
        "201",
        "1",
    )
    # A deposit line that banks a payment (``LinkedTxn``, no account) is not a cost line;
    # one to an income or equity account is not either.
    payload = record("Deposit", "3301")
    payload["Line"] = [
        {
            "Amount": 500,
            "DetailType": "DepositLineDetail",
            "LinkedTxn": [{"TxnId": "1", "TxnType": "Payment"}],
            "DepositLineDetail": {},
        },
        {
            "Amount": 10,
            "DetailType": "DepositLineDetail",
            "LineNum": 2,
            "DepositLineDetail": {"AccountRef": {"value": "79"}},
        },
    ]
    assert normalize_cost("Deposit", payload, SIDE).lines == ()


def test_the_sandboxs_deposits_read_with_their_payment_lines_left_out() -> None:
    """The recorded sandbox Deposits (S-01): a line that banks a payment carries ``LinkedTxn``
    and no ``DetailType``; it is not a cost line and never makes the document unreadable."""
    from tests.qbo_fixtures import records

    side = AccountSide(expense_side=frozenset({"80"}), item_accounts={})
    docs = [normalize_cost("Deposit", payload, side) for payload in records("Deposit")]
    assert len(docs) == 5 and all(doc.lines == () for doc in docs)


def test_a_line_without_an_id_takes_its_line_number_and_an_unreadable_payload_is_refused() -> None:
    payload = record("Bill", "98893")
    del payload["Line"][0]["Id"]
    (ln,) = normalize_cost("Bill", payload, SIDE).lines
    assert ln.line_external_id == "1"  # LineNum
    del payload["Line"][0]["LineNum"]
    (ln,) = normalize_cost("Bill", payload, SIDE).lines
    assert ln.line_external_id == "n1"  # the position, the last resort
    for broken, reason in (
        ({**record("Bill", "98893"), "TxnDate": None}, "txn_date_missing"),
        ({**record("Bill", "98893"), "ExchangeRate": 1.3}, "foreign_currency"),
        ({**record("Bill", "98893"), "Id": None}, "id_missing"),
    ):
        with pytest.raises(Unreadable) as err:
            normalize_cost("Bill", broken, SIDE)
        assert err.value.reason == reason
    bad_amount = record("Bill", "98893")
    bad_amount["Line"][0]["Amount"] = D("1.005")
    with pytest.raises(Unreadable) as err:
        normalize_cost("Bill", bad_amount, SIDE)
    assert err.value.reason == "line_amount_more_than_two_decimals"


def test_a_voided_document_is_one_whose_note_says_so_and_whose_lines_are_zero() -> None:
    payload = record("Bill", "98892")
    payload["PrivateNote"] = "Voided"
    assert not normalize_cost("Bill", payload, SIDE).voided  # the lines still carry amounts
    for ln in payload["Line"]:
        ln["Amount"] = 0
    payload["TotalAmt"] = 0
    doc = normalize_cost("Bill", payload, SIDE)
    assert doc.voided and [ln.amount for ln in doc.lines] == [D("0.00"), D("0.00")]


def test_every_fixture_document_normalizes_and_no_balance_sheet_line_is_kept() -> None:
    kept = 0
    for entity in ("Bill", "VendorCredit", "Purchase", "JournalEntry", "Deposit"):
        for payload in fixture(entity):
            doc = normalize_cost(entity, payload, SIDE)
            assert all(
                ln.account_external_id in SIDE.expense_side or ln.account_external_id is None
                for ln in doc.lines
            )
            kept += len(doc.lines)
    # 2 + 1 + 7 + 2 + 1 + 1 (bills) + 1 (vendor credit) + 2 (purchases) + 2 (journal) + 1 (deposit)
    assert kept == 20
