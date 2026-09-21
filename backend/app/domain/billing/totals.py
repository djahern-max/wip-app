"""Month totals of the billing side (F05 Connections page; owner answer 4). By
calendar month of ``txn_date``: Invoices, Credit memos (signed, so negative), Sales
receipts, Payments (Payment documents only; a sales receipt's collected side is not
a Payment). Deleted and voided documents contribute 0.00. Money stays ``Decimal``
here; the API renders strings."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.billing.amounts import signed_amount
from app.domain.billing.models import Billing, Payment

ZERO = Decimal("0.00")
COLUMNS = ("invoices", "credit_memos", "sales_receipts", "payments")


@dataclass
class MonthTotals:
    month: str  # YYYY-MM
    invoices: Decimal = ZERO
    credit_memos: Decimal = ZERO
    sales_receipts: Decimal = ZERO
    payments: Decimal = ZERO


def month_totals(db: Session, tenant_id: UUID) -> list[MonthTotals]:
    months: dict[str, MonthTotals] = {}

    def row(month: str) -> MonthTotals:
        return months.setdefault(month, MonthTotals(month))

    column = {
        "invoice": "invoices",
        "credit_memo": "credit_memos",
        "sales_receipt": "sales_receipts",
    }
    for b in db.execute(select(Billing).where(Billing.tenant_id == tenant_id)).scalars():
        m = row(b.txn_date.strftime("%Y-%m"))
        setattr(m, column[b.kind], getattr(m, column[b.kind]) + signed_amount(b))
    for p in db.execute(
        select(Payment).where(Payment.tenant_id == tenant_id, Payment.kind == "payment")
    ).scalars():
        if p.deleted_at is None:
            m = row(p.txn_date.strftime("%Y-%m"))
            m.payments += p.total
    return [months[k] for k in sorted(months)]
