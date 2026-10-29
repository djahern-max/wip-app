"""Bill, VendorCredit, Purchase, JournalEntry, Deposit → the cost lines of one document
(F10; D-51; the F10 brief). Pure: the payload and what the caller knows about accounts in,
a ``CostDoc`` out. No database, no clock.

One line per payload line that posts to an **expense-side account** (``AccountSide``: the
QuickBooks account ids the caller found to be Cost of Goods Sold or Expense, or mapped in
``account_map``). Lines to balance-sheet accounts (A/P, a bank, accrued payroll, a credit
card) are not lines here. An ``ItemBasedExpenseLineDetail`` line takes its account from
the item's ``ExpenseAccountRef`` (``AccountSide.item_accounts``); an item with none, or an
item the copy does not hold, leaves the account ``None`` and the line is kept ("No
account"); an item whose expense account is a balance-sheet account is not a cost line.

**Sign** (the brief): a Bill line is positive; a VendorCredit line negative; a Purchase
line positive, negative when ``Credit = true``; a JournalEntry line positive when
``PostingType = Debit``, negative when ``Credit``; a Deposit line negative (a refund
reduces the expense). Amounts are the payload's numbers as the codec gives them (``int``
or ``Decimal``), cents exactly and never rounded (``money.cents``).

**References** (D-51: the line's own, by id, nothing else): the customer is the line's
``CustomerRef.value``; a JournalEntry or Deposit line names its party in ``Entity``, read
as the customer when its type is Customer and as the vendor when it is Vendor (Plan answer
3). The document's vendor is ``VendorRef`` (Bill, VendorCredit) or ``EntityRef`` of type
Vendor (Purchase); a line's vendor is its own when it has one, else the document's.

**Voided**: QuickBooks keeps a voided document with ``PrivateNote`` starting "Voided" and
every line at 0.00; the caller marks its lines deleted (D-20).

A payload that cannot be read raises ``Unreadable`` with a short reason; the caller counts
it and moves on.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.money import (
    ZERO,
    cents_or_zero,
    external_id,
    home_currency_only,
    iso_date,
    ref_value,
)

COST_ENTITIES: tuple[str, ...] = ("Bill", "VendorCredit", "Purchase", "JournalEntry", "Deposit")
ACCOUNT_BASED = "AccountBasedExpenseLineDetail"
ITEM_BASED = "ItemBasedExpenseLineDetail"
JOURNAL = "JournalEntryLineDetail"
DEPOSIT = "DepositLineDetail"
VOID_NOTE = "Voided"


def is_cost_entity(entity: str) -> bool:
    return entity in COST_ENTITIES


@dataclass(frozen=True)
class AccountSide:
    """What the caller knows about accounts: which QuickBooks account ids are expense-side,
    and each held item's expense account id (``None``: the item has none)."""

    expense_side: frozenset[str]
    item_accounts: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class CostLineRow:
    line_external_id: str
    line_no: int
    account_external_id: str | None  # None: no account (an item without one, or not held)
    vendor_external_id: str | None
    customer_external_id: str | None
    amount: Decimal  # signed
    description: str | None
    item_external_id: str | None = None


@dataclass(frozen=True)
class CostDoc:
    txn_type: str
    external_id: str
    txn_date: date
    doc_number: str | None
    vendor_external_id: str | None
    memo: str | None
    voided: bool
    lines: tuple[CostLineRow, ...]


def _text(value, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _entity(detail: dict) -> tuple[str | None, str | None]:
    """(customer id, vendor id) from a JournalEntry or Deposit line's ``Entity``."""
    entity = detail.get("Entity")
    if not isinstance(entity, dict):
        return None, None
    kind = entity.get("Type") or entity.get("type")
    ref = entity.get("EntityRef") if "EntityRef" in entity else entity
    value = ref_value(ref, field="entity", required=False) if isinstance(ref, dict) else None
    if value is None:
        return None, None
    if kind == "Customer":
        return value, None
    if kind == "Vendor":
        return None, value
    return None, None


def _document_vendor(entity: str, payload: dict) -> str | None:
    if entity in ("Bill", "VendorCredit"):
        return ref_value(payload.get("VendorRef"), field="vendor", required=False)
    if entity == "Purchase":
        ref = payload.get("EntityRef")
        if isinstance(ref, dict) and ref.get("type") not in (None, "Vendor"):
            return None
        return ref_value(ref, field="vendor", required=False)
    return None


def _document_sign(entity: str, payload: dict) -> int:
    if entity == "VendorCredit" or entity == "Deposit":
        return -1
    if entity == "Purchase" and payload.get("Credit") is True:
        return -1
    return 1


def _line(
    entity: str, raw: dict, position: int, doc_sign: int, doc_vendor: str | None, side: AccountSide
) -> CostLineRow | None:
    """The cost line of one payload line, or ``None`` when it posts to no expense-side
    account (a balance-sheet line, a payment banked, an income line)."""
    kind = raw.get("DetailType")
    if not isinstance(kind, str) or not kind:
        # QuickBooks sends a Deposit line that banks a payment (``LinkedTxn``) with no
        # ``DetailType`` (the sandbox, S-01): it posts to no account and is not a cost line.
        if entity == "Deposit" and (raw.get("LinkedTxn") or DEPOSIT in raw):
            kind = DEPOSIT
        else:
            raise Unreadable("line_kind_missing")
    detail = raw.get(kind)
    if not isinstance(detail, dict):
        detail = {}
    account: str | None = None
    item: str | None = None
    customer: str | None = None
    vendor: str | None = None
    sign = doc_sign
    if kind == ACCOUNT_BASED:
        account = ref_value(detail.get("AccountRef"), field="account", required=False)
        customer = ref_value(detail.get("CustomerRef"), field="customer", required=False)
        if account not in side.expense_side:
            return None
    elif kind == ITEM_BASED:
        item = ref_value(detail.get("ItemRef"), field="item", required=False)
        customer = ref_value(detail.get("CustomerRef"), field="customer", required=False)
        if item is None:
            return None
        account = side.item_accounts.get(item)
        if account is not None and account not in side.expense_side:
            return None  # the item's expense account is a balance-sheet account
    elif kind == JOURNAL and entity == "JournalEntry":
        account = ref_value(detail.get("AccountRef"), field="account", required=False)
        if account not in side.expense_side:
            return None
        posting = detail.get("PostingType")
        if posting not in ("Debit", "Credit"):
            raise Unreadable("posting_type_unreadable")
        sign = 1 if posting == "Debit" else -1
        customer, vendor = _entity(detail)
    elif kind == DEPOSIT and entity == "Deposit":
        account = ref_value(detail.get("AccountRef"), field="account", required=False)
        if account not in side.expense_side:
            return None
        customer, vendor = _entity(detail)
    else:
        return None  # a description-only, subtotal or other line
    amount = cents_or_zero(raw.get("Amount"), field="line_amount")
    line_id = raw.get("Id")
    if line_id is None or line_id == "":
        line_id = raw.get("LineNum")
    line_external_id = str(line_id) if line_id is not None and line_id != "" else f"n{position}"
    return CostLineRow(
        line_external_id=line_external_id[:80],
        line_no=position,
        account_external_id=account,
        vendor_external_id=vendor or doc_vendor,
        customer_external_id=customer,
        amount=sign * amount if amount != ZERO else ZERO,
        description=_text(raw.get("Description"), 4000),
        item_external_id=item,
    )


def normalize_cost(entity: str, payload, side: AccountSide) -> CostDoc:
    if not is_cost_entity(entity):
        raise Unreadable("not_a_cost_entity")
    if not isinstance(payload, dict):
        raise Unreadable("payload_not_an_object")
    home_currency_only(payload)
    doc_id = external_id(payload)
    txn_date = iso_date(payload.get("TxnDate"), field="txn_date")
    raw_lines = payload.get("Line") or []
    if not isinstance(raw_lines, list):
        raise Unreadable("lines_unreadable")
    doc_vendor = _document_vendor(entity, payload)
    doc_sign = _document_sign(entity, payload)
    lines: list[CostLineRow] = []
    for position, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, dict):
            raise Unreadable("line_unreadable")
        row = _line(entity, raw, position, doc_sign, doc_vendor, side)
        if row is not None:
            lines.append(row)
    note = payload.get("PrivateNote")
    voided = (
        isinstance(note, str)
        and note.startswith(VOID_NOTE)
        and bool(lines)
        and all(ln.amount == ZERO for ln in lines)
    )
    return CostDoc(
        txn_type=entity,
        external_id=doc_id,
        txn_date=txn_date,
        doc_number=_text(payload.get("DocNumber"), 40),
        vendor_external_id=doc_vendor,
        memo=_text(note, 4000),
        voided=voided,
        lines=tuple(lines),
    )
