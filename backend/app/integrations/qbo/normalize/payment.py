"""Payment → ``payment`` + ``payment_application`` (F05; S-01 extra 2). One
application per ``LinkedTxn`` on a payment line, ``amount`` as QuickBooks gives it
(a line that applies a credit memo arrives positive; ``app.domain.billing.amounts``
signs it). A line with more than one ``LinkedTxn`` has no per-link amount and is
refused rather than guessed. A SalesReceipt is billed and collected in one document:
``payment_from_sales_receipt`` makes its collected side, fully applied to itself
(owner answer 4)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.billing import BillingRow
from app.integrations.qbo.normalize.money import (
    ZERO,
    cents,
    cents_or_zero,
    external_id,
    home_currency_only,
    iso_date,
    ref_value,
)


@dataclass(frozen=True)
class ApplicationRow:
    line_no: int
    linked_txn_type: str
    linked_txn_external_id: str
    amount: Decimal


@dataclass(frozen=True)
class PaymentRow:
    kind: str
    external_id: str
    txn_date: date
    customer_external_id: str
    total: Decimal
    unapplied_amount: Decimal
    deposit_account_external_id: str | None
    applications: tuple[ApplicationRow, ...]


def normalize_payment(payload: dict) -> PaymentRow:
    if not isinstance(payload, dict):
        raise Unreadable("payload_not_an_object")
    home_currency_only(payload)
    applications: list[ApplicationRow] = []
    raw_lines = payload.get("Line") or []
    if not isinstance(raw_lines, list):
        raise Unreadable("lines_unreadable")
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise Unreadable("line_unreadable")
        links = raw.get("LinkedTxn") or []
        if not isinstance(links, list):
            raise Unreadable("linked_txn_unreadable")
        if not links:
            continue
        if len(links) > 1:
            raise Unreadable("line_with_several_linked_txns")
        link = links[0]
        txn_type, txn_id = link.get("TxnType"), link.get("TxnId")
        if not isinstance(txn_type, str) or not isinstance(txn_id, str) or not txn_id:
            raise Unreadable("linked_txn_unreadable")
        applications.append(
            ApplicationRow(
                line_no=len(applications) + 1,
                linked_txn_type=txn_type[:40],
                linked_txn_external_id=txn_id,
                amount=cents(raw.get("Amount"), field="line_amount"),
            )
        )
    return PaymentRow(
        kind="payment",
        external_id=external_id(payload),
        txn_date=iso_date(payload.get("TxnDate"), field="txn_date"),
        customer_external_id=ref_value(payload.get("CustomerRef"), field="customer", required=True),
        total=cents(payload.get("TotalAmt"), field="total"),
        unapplied_amount=cents_or_zero(payload.get("UnappliedAmt"), field="unapplied"),
        deposit_account_external_id=ref_value(
            payload.get("DepositToAccountRef"), field="deposit_account", required=False
        ),
        applications=tuple(applications),
    )


def payment_from_sales_receipt(receipt: BillingRow) -> PaymentRow:
    """The collected side of a sales receipt: same date, customer and total, fully
    applied to the receipt itself. A voided or zero receipt collects 0.00."""
    if receipt.kind != "sales_receipt":
        raise Unreadable("not_a_sales_receipt")
    return PaymentRow(
        kind="sales_receipt",
        external_id=receipt.external_id,
        txn_date=receipt.txn_date,
        customer_external_id=receipt.customer_external_id,
        total=receipt.total,
        unapplied_amount=ZERO,
        deposit_account_external_id=receipt.deposit_account_external_id,
        applications=(
            ApplicationRow(
                line_no=1,
                linked_txn_type="SalesReceipt",
                linked_txn_external_id=receipt.external_id,
                amount=receipt.total,
            ),
        ),
    )
