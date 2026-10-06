"""Month totals of the billing side (F05 Connections page; owner answer 4). By
calendar month of ``txn_date``: Invoices, Credit memos (signed, so negative), Sales
receipts, Payments (Payment documents only; a sales receipt's collected side is not
a Payment). Deleted and voided documents contribute 0.00. Money stays ``Decimal``
here; the API renders strings.

F08.2: summed by the database (one ``GROUP BY`` month per table), not by loading
every row into Python; the rows a month comes from are the same as before: every
``billing`` row of the tenant makes its month appear (a deleted or voided one at
0.00), a ``payment`` row only while not deleted."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.domain.billing.models import Billing, Payment

ZERO = Decimal("0.00")
COLUMNS = ("invoices", "credit_memos", "sales_receipts", "payments")
MONTH_FORMAT = "YYYY-MM"


@dataclass
class MonthTotals:
    month: str  # YYYY-MM
    invoices: Decimal = ZERO
    credit_memos: Decimal = ZERO
    sales_receipts: Decimal = ZERO
    payments: Decimal = ZERO


def month_of(column):
    """The calendar month of a date column as ``YYYY-MM`` (the key ``figures.month_key``
    gives in Python)."""
    return func.to_char(column, MONTH_FORMAT)


def decimal(value) -> Decimal:
    """A SUM from the database as ``Decimal``; NULL (no rows) is 0.00. Never a float."""
    if value is None:
        return ZERO
    if isinstance(value, float):  # pragma: no cover - the driver returns Decimal for NUMERIC
        raise TypeError("a money sum came back as float")
    return Decimal(value).quantize(Decimal("0.01"))


def billing_sign():
    return case((Billing.kind == "credit_memo", -1), else_=1)


def billing_counted():
    return and_(Billing.deleted_at.is_(None), Billing.voided.is_(False))


def month_totals(db: Session, tenant_id: UUID) -> list[MonthTotals]:
    months: dict[str, MonthTotals] = {}

    def row(month: str) -> MonthTotals:
        return months.setdefault(month, MonthTotals(month))

    column = {
        "invoice": "invoices",
        "credit_memo": "credit_memos",
        "sales_receipt": "sales_receipts",
    }
    month = month_of(Billing.txn_date)
    counted_total = case((billing_counted(), billing_sign() * Billing.total), else_=0)
    for m, kind, total in db.execute(
        select(month, Billing.kind, func.sum(counted_total))
        .where(Billing.tenant_id == tenant_id)
        .group_by(month, Billing.kind)
    ).all():
        r = row(m)
        setattr(r, column[kind], getattr(r, column[kind]) + decimal(total))
    month = month_of(Payment.txn_date)
    for m, total in db.execute(
        select(month, func.sum(Payment.total))
        .where(
            Payment.tenant_id == tenant_id,
            Payment.kind == "payment",
            Payment.deleted_at.is_(None),
        )
        .group_by(month)
    ).all():
        row(m).payments += decimal(total)
    return [months[k] for k in sorted(months)]
