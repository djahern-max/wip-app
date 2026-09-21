"""Invoice, CreditMemo, SalesReceipt → ``billing`` + ``billing_line`` (F05; S-01 (d)
and extra 1; owner answers of 2026-09-21).

- ``subtotal``: QuickBooks' SubTotal line when present; else the sum of the sales-item
  lines; else (no lines at all) ``total − tax_total``.
- ``discount_total``: the sum of ``DiscountLineDetail`` lines, which arrive positive.
- ``subtotal − discount_total + tax_total = total`` must hold, or the payload is unreadable.
- Lines: every line except the SubTotal line, in document order, ``line_kind`` =
  ``DetailType``. A line without an amount (description only) is 0.00.
- ``voided``: ``TotalAmt = 0`` and ``PrivateNote`` starts with ``Voided`` **and** an
  earlier raw version had a non-zero total (the caller knows the history). A record
  that was always 0.00 is an ordinary zero document.
- Amounts are stored positive as QuickBooks gives them; ``app.domain.billing.amounts``
  signs a credit memo.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.money import (
    ZERO,
    cents,
    cents_or_zero,
    external_id,
    home_currency_only,
    iso_date,
    ref_value,
)

KIND_BY_ENTITY: dict[str, str] = {
    "Invoice": "invoice",
    "CreditMemo": "credit_memo",
    "SalesReceipt": "sales_receipt",
}
SUBTOTAL = "SubTotalLineDetail"
SALES_ITEM = "SalesItemLineDetail"
DISCOUNT = "DiscountLineDetail"
VOID_NOTE = "Voided"


@dataclass(frozen=True)
class BillingLineRow:
    line_no: int
    external_line_id: str | None
    line_kind: str
    description: str | None
    item_external_id: str | None
    amount: Decimal


@dataclass(frozen=True)
class BillingRow:
    kind: str
    external_id: str
    doc_number: str | None
    txn_date: date
    due_date: date | None
    customer_external_id: str
    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    total: Decimal
    balance: Decimal
    voided: bool
    deposit_account_external_id: str | None  # sales receipts only
    lines: tuple[BillingLineRow, ...]


def is_voided(payload: dict, total: Decimal, *, had_nonzero_total_before: bool) -> bool:
    note = payload.get("PrivateNote")
    return (
        total == ZERO
        and isinstance(note, str)
        and note.startswith(VOID_NOTE)
        and had_nonzero_total_before
    )


def normalize_billing(
    entity: str, payload: dict, *, had_nonzero_total_before: bool = False
) -> BillingRow:
    if entity not in KIND_BY_ENTITY:
        raise Unreadable("not_a_billing_entity")
    if not isinstance(payload, dict):
        raise Unreadable("payload_not_an_object")
    home_currency_only(payload)
    total = cents(payload.get("TotalAmt"), field="total")
    tax_detail = payload.get("TxnTaxDetail")
    tax_total = cents_or_zero(
        tax_detail.get("TotalTax") if isinstance(tax_detail, dict) else None, field="tax_total"
    )
    raw_lines = payload.get("Line") or []
    if not isinstance(raw_lines, list):
        raise Unreadable("lines_unreadable")
    subtotal_line: Decimal | None = None
    item_sum = ZERO
    saw_item = False
    discount_total = ZERO
    lines: list[BillingLineRow] = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise Unreadable("line_unreadable")
        kind = raw.get("DetailType")
        if not isinstance(kind, str) or not kind:
            raise Unreadable("line_kind_missing")
        amount = cents_or_zero(raw.get("Amount"), field="line_amount")
        if kind == SUBTOTAL:
            subtotal_line = amount
            continue
        if kind == SALES_ITEM:
            item_sum += amount
            saw_item = True
        elif kind == DISCOUNT:
            discount_total += amount
        detail = raw.get(kind)
        item = None
        if isinstance(detail, dict):
            item = ref_value(detail.get("ItemRef"), field="item", required=False)
        description = raw.get("Description")
        lines.append(
            BillingLineRow(
                line_no=len(lines) + 1,
                external_line_id=str(raw["Id"]) if raw.get("Id") is not None else None,
                line_kind=kind[:40],
                description=description[:4000] if isinstance(description, str) else None,
                item_external_id=item,
                amount=amount,
            )
        )
    if subtotal_line is not None:
        subtotal = subtotal_line
    elif saw_item:
        subtotal = item_sum
    else:
        subtotal = total - tax_total
    if subtotal - discount_total + tax_total != total:
        raise Unreadable("total_identity_fails")
    doc_number = payload.get("DocNumber")
    return BillingRow(
        kind=KIND_BY_ENTITY[entity],
        external_id=external_id(payload),
        doc_number=doc_number[:40] if isinstance(doc_number, str) else None,
        txn_date=iso_date(payload.get("TxnDate"), field="txn_date"),
        due_date=(
            iso_date(payload["DueDate"], field="due_date") if payload.get("DueDate") else None
        ),
        customer_external_id=ref_value(payload.get("CustomerRef"), field="customer", required=True),
        subtotal=subtotal,
        discount_total=discount_total,
        tax_total=tax_total,
        total=total,
        balance=cents_or_zero(payload.get("Balance"), field="balance"),
        voided=is_voided(payload, total, had_nonzero_total_before=had_nonzero_total_before),
        deposit_account_external_id=ref_value(
            payload.get("DepositToAccountRef"), field="deposit_account", required=False
        ),
        lines=tuple(lines),
    )
