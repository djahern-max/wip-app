"""Canonical billing tables (F05; BLUEPRINT §7). All tenant-scoped: ``tenant_id``
NOT NULL, a leading index, RLS enabled and forced in migration 0008. Money is
``NUMERIC(14,2)``; accounting dates are ``date``.

Every row points at the raw version it was built from (``raw_record_id``). A
normalizer run is idempotent: the same latest raw version produces the same row.

- ``customer``: a QuickBooks Customer. A project is a row with ``is_project`` (from
  ``IsProject``, S-01 (a)) and a parent; a sub-customer has a parent and
  ``is_project = false``.
- ``billing``: Invoice, CreditMemo or SalesReceipt header. Amounts are stored
  positive as QuickBooks gives them; ``signed_amount`` (``amounts.py``) makes a
  credit memo negative. ``subtotal − discount_total + tax_total = total`` on every
  row (owner answer, 2026-09-21). ``voided`` is by convention (S-01 (d)).
- ``billing_line``: the document's lines; ``line_kind`` is QuickBooks' ``DetailType``.
- ``payment``: a Payment, or the collected side of a SalesReceipt (``kind``).
- ``payment_application``: one per ``LinkedTxn`` on a payment line; ``billing_id``
  is set when the linked document is a ``billing`` row we hold. A line that applies
  a credit memo arrives positive (S-01 extra 2); ``signed_application`` makes it
  negative.

``customer_id`` and ``billing_id`` are nullable and filled in whichever order the
records arrive: normalizing a customer fills the documents that named it, and
normalizing a document fills the applications that named it.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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

BILLING_KINDS: tuple[str, ...] = ("invoice", "credit_memo", "sales_receipt")
BILLING_KIND_CHECK = "ck_billing_kind"
BILLING_KIND_CHECK_SQL = "kind IN ('invoice','credit_memo','sales_receipt')"
BILLING_TOTAL_CHECK = "ck_billing_total_identity"
BILLING_TOTAL_CHECK_SQL = "subtotal - discount_total + tax_total = total"
PAYMENT_KINDS: tuple[str, ...] = ("payment", "sales_receipt")
PAYMENT_KIND_CHECK = "ck_payment_kind"
PAYMENT_KIND_CHECK_SQL = "kind IN ('payment','sales_receipt')"

# QuickBooks entity → billing kind, and the LinkedTxn type a payment names.
BILLING_KIND_BY_ENTITY: dict[str, str] = {
    "Invoice": "invoice",
    "CreditMemo": "credit_memo",
    "SalesReceipt": "sales_receipt",
}

MONEY = Numeric(14, 2)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _tenant_id() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )


def _raw_record_id() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_record.id", ondelete="RESTRICT"), nullable=False
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Customer(Base):
    __tablename__ = "customer"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "external_id", name="uq_customer_tenant_source_id"),
        Index("ix_customer_tenant_id_parent_customer_id", "tenant_id", "parent_customer_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    parent_external_id: Mapped[str | None] = mapped_column(String(80))
    parent_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer.id", ondelete="RESTRICT")
    )
    is_project: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw_record_id: Mapped[uuid.UUID] = _raw_record_id()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Billing(Base):
    __tablename__ = "billing"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "kind", "external_id", name="uq_billing_tenant_source_kind_id"
        ),
        Index("ix_billing_tenant_id_txn_date", "tenant_id", "txn_date"),
        Index("ix_billing_tenant_id_customer_id", "tenant_id", "customer_id"),
        CheckConstraint(BILLING_KIND_CHECK_SQL, name=BILLING_KIND_CHECK),
        CheckConstraint(BILLING_TOTAL_CHECK_SQL, name=BILLING_TOTAL_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    doc_number: Mapped[str | None] = mapped_column(String(40))
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    customer_external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer.id", ondelete="RESTRICT")
    )
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    discount_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0.00"))
    tax_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    voided: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_record_id: Mapped[uuid.UUID] = _raw_record_id()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class BillingLine(Base):
    __tablename__ = "billing_line"
    __table_args__ = (
        UniqueConstraint("tenant_id", "billing_id", "line_no", name="uq_billing_line_no"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    billing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing.id", ondelete="RESTRICT"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    external_line_id: Mapped[str | None] = mapped_column(String(80))
    line_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4000))
    item_external_id: Mapped[str | None] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class Payment(Base):
    __tablename__ = "payment"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "kind", "external_id", name="uq_payment_tenant_source_kind_id"
        ),
        Index("ix_payment_tenant_id_txn_date", "tenant_id", "txn_date"),
        CheckConstraint(PAYMENT_KIND_CHECK_SQL, name=PAYMENT_KIND_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    customer_external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer.id", ondelete="RESTRICT")
    )
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unapplied_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    deposit_account_external_id: Mapped[str | None] = mapped_column(String(80))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_record_id: Mapped[uuid.UUID] = _raw_record_id()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class PaymentApplication(Base):
    __tablename__ = "payment_application"
    __table_args__ = (
        UniqueConstraint("tenant_id", "payment_id", "line_no", name="uq_payment_application_no"),
        Index("ix_payment_application_tenant_id_billing_id", "tenant_id", "billing_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment.id", ondelete="RESTRICT"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    linked_txn_type: Mapped[str] = mapped_column(String(40), nullable=False)
    linked_txn_external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    billing_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing.id", ondelete="RESTRICT")
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
