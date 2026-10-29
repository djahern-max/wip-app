"""The one cost table (F10; BLUEPRINT §7 "Cost"; D-20, D-22, D-51; migration 0017).
Tenant-scoped: ``tenant_id`` NOT NULL, an index leading with it, RLS enabled and forced.

``ledger_line``: one row per QuickBooks line that posts to an **expense-side** account
(a chart account whose ledger type is Cost of Goods Sold or Expense, any account with a
confirmed ``account_map`` row, or a QuickBooks account of those types that the chart does
not hold), from a Bill, a Vendor Credit, a Purchase, a Journal Entry or a Deposit. Lines to
balance-sheet accounts (A/P, a bank, accrued payroll) are never stored.

- ``amount`` is **signed** as QuickBooks posted it: a debit to the account is positive, a
  credit negative (a Vendor Credit, a Purchase with ``Credit = true``, a Journal Entry
  credit, a Deposit refund). Cents exactly, never rounded: every figure is a sum.
- ``job_id`` is the job aliased (``job_alias``, system ``qbo_customer``) to the **line's**
  ``CustomerRef`` and nothing else (D-51); null is **unassigned**. It is filled when the
  row is written and again by the next cost run after a link or an unlink.
- ``gl_account_id`` is the chart row matched by QuickBooks id (F05.1 attached ids); null
  with ``account_external_id`` set is "Not in the chart", null with none is "No account"
  (an item-based line whose item has no expense account).
- What is **not** stored: division, cost category, whether the account is job cost and
  whether its category is in the WIP basis. All four are read from ``account_map`` and
  ``tenant_policy`` when a figure is computed (D-22, D-51), so confirming a mapping later
  changes every figure at once with no re-sync.
- ``deleted_at``: a CDC delete stub or a void (D-20); the raw versions stay.
- Unique on ``(tenant_id, source, txn_type, txn_external_id, line_external_id)``; a
  document that returns with fewer lines loses the lines it no longer has (the F05 rule).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.tenancy.models import Base

SOURCE = "qbo"
TXN_TYPES: tuple[str, ...] = ("Bill", "VendorCredit", "Purchase", "JournalEntry", "Deposit")
TXN_TYPE_LABELS: dict[str, str] = {
    "Bill": "Bill",
    "VendorCredit": "Vendor credit",
    "Purchase": "Expense",
    "JournalEntry": "Journal entry",
    "Deposit": "Deposit",
}
TXN_TYPE_CHECK_SQL = "txn_type IN ('Bill','VendorCredit','Purchase','JournalEntry','Deposit')"
MONEY = Numeric(14, 2)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _fk(target: str, *, nullable: bool = True):
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(target, ondelete="RESTRICT"), nullable=nullable
    )


class LedgerLine(Base):
    __tablename__ = "ledger_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source",
            "txn_type",
            "txn_external_id",
            "line_external_id",
            name="uq_ledger_line_tenant_source_txn_line",
        ),
        Index("ix_ledger_line_tenant_id_txn_date", "tenant_id", "txn_date"),
        Index("ix_ledger_line_tenant_id_job_id", "tenant_id", "job_id"),
        Index("ix_ledger_line_tenant_id_gl_account_id", "tenant_id", "gl_account_id"),
        CheckConstraint(TXN_TYPE_CHECK_SQL, name="ck_ledger_line_txn_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _fk("tenant.id", nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    txn_type: Mapped[str] = mapped_column(String(20), nullable=False)
    txn_external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    # The line's ``Id``; its ``LineNum`` where QuickBooks gives none.
    line_external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    doc_number: Mapped[str | None] = mapped_column(String(40))
    gl_account_id: Mapped[uuid.UUID | None] = _fk("gl_account.id")
    account_external_id: Mapped[str | None] = mapped_column(String(80))
    vendor_external_id: Mapped[str | None] = mapped_column(String(80))
    customer_external_id: Mapped[str | None] = mapped_column(String(80))
    job_id: Mapped[uuid.UUID | None] = _fk("job.id")
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    description: Mapped[str | None] = mapped_column(String(4000))
    memo: Mapped[str | None] = mapped_column(String(4000))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_record_id: Mapped[uuid.UUID] = _fk("raw_record.id", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
