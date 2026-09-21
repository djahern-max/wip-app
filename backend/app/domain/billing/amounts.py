"""Signs (F05). Rows store amounts positive as QuickBooks gives them; these are the
one place a sign is decided."""

from decimal import Decimal

from app.domain.billing.models import Billing, PaymentApplication


def signed_amount(row: Billing) -> Decimal:
    """A credit memo reduces what was billed. Deleted and voided documents are 0.00."""
    if row.deleted_at is not None or row.voided:
        return Decimal("0.00")
    return -row.total if row.kind == "credit_memo" else row.total


def signed_application(row: PaymentApplication) -> Decimal:
    """A payment line that applies a credit memo arrives positive (S-01 extra 2) and
    counts against the invoice lines: applications + unapplied = total only with it
    negative."""
    return -row.amount if row.linked_txn_type == "CreditMemo" else row.amount
