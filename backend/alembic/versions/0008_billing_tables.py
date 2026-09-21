"""F05: customer, billing, billing_line, payment, payment_application

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21

The canonical billing-side tables (BLUEPRINT §7; F05 brief, shaped by spike S-01 and
the owner's answers of 2026-09-21). All tenant-scoped: ``tenant_id NOT NULL``, an index
or unique constraint leading with it, ``enable_tenant_rls``. Money is
``NUMERIC(14,2)``; ``ck_billing_total_identity`` holds
``subtotal − discount_total + tax_total = total`` on every row. No seed data.

Reversible: yes. The downgrade drops the five tables (rows are rebuilt from
``raw_record`` by the normalizers, which are idempotent).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("payment_application", "payment", "billing_line", "billing", "customer")
MONEY = sa.Numeric(14, 2)


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _tenant_id() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _fk(name: str, target: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete="RESTRICT"),
        nullable=nullable,
    )


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "customer",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("parent_external_id", sa.String(80), nullable=True),
        _fk("parent_customer_id", "customer.id"),
        sa.Column("is_project", sa.Boolean, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        _fk("raw_record_id", "raw_record.id", nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint(
            "tenant_id", "source", "external_id", name="uq_customer_tenant_source_id"
        ),
    )
    op.create_index(
        "ix_customer_tenant_id_parent_customer_id", "customer", ["tenant_id", "parent_customer_id"]
    )

    op.create_table(
        "billing",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("doc_number", sa.String(40), nullable=True),
        sa.Column("txn_date", sa.Date, nullable=False),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column("customer_external_id", sa.String(80), nullable=False),
        _fk("customer_id", "customer.id"),
        sa.Column("subtotal", MONEY, nullable=False),
        sa.Column("discount_total", MONEY, nullable=False),
        sa.Column("tax_total", MONEY, nullable=False),
        sa.Column("total", MONEY, nullable=False),
        sa.Column("balance", MONEY, nullable=False),
        sa.Column("voided", sa.Boolean, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _fk("raw_record_id", "raw_record.id", nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint(
            "tenant_id", "source", "kind", "external_id", name="uq_billing_tenant_source_kind_id"
        ),
        sa.CheckConstraint(
            "kind IN ('invoice','credit_memo','sales_receipt')", name="ck_billing_kind"
        ),
        sa.CheckConstraint(
            "subtotal - discount_total + tax_total = total", name="ck_billing_total_identity"
        ),
    )
    op.create_index("ix_billing_tenant_id_txn_date", "billing", ["tenant_id", "txn_date"])
    op.create_index("ix_billing_tenant_id_customer_id", "billing", ["tenant_id", "customer_id"])

    op.create_table(
        "billing_line",
        _uuid_pk(),
        _tenant_id(),
        _fk("billing_id", "billing.id", nullable=False),
        sa.Column("line_no", sa.Integer, nullable=False),
        sa.Column("external_line_id", sa.String(80), nullable=True),
        sa.Column("line_kind", sa.String(40), nullable=False),
        sa.Column("description", sa.String(4000), nullable=True),
        sa.Column("item_external_id", sa.String(80), nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.UniqueConstraint("tenant_id", "billing_id", "line_no", name="uq_billing_line_no"),
    )

    op.create_table(
        "payment",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("txn_date", sa.Date, nullable=False),
        sa.Column("customer_external_id", sa.String(80), nullable=False),
        _fk("customer_id", "customer.id"),
        sa.Column("total", MONEY, nullable=False),
        sa.Column("unapplied_amount", MONEY, nullable=False),
        sa.Column("deposit_account_external_id", sa.String(80), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _fk("raw_record_id", "raw_record.id", nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint(
            "tenant_id", "source", "kind", "external_id", name="uq_payment_tenant_source_kind_id"
        ),
        sa.CheckConstraint("kind IN ('payment','sales_receipt')", name="ck_payment_kind"),
    )
    op.create_index("ix_payment_tenant_id_txn_date", "payment", ["tenant_id", "txn_date"])

    op.create_table(
        "payment_application",
        _uuid_pk(),
        _tenant_id(),
        _fk("payment_id", "payment.id", nullable=False),
        sa.Column("line_no", sa.Integer, nullable=False),
        sa.Column("linked_txn_type", sa.String(40), nullable=False),
        sa.Column("linked_txn_external_id", sa.String(80), nullable=False),
        _fk("billing_id", "billing.id"),
        sa.Column("amount", MONEY, nullable=False),
        sa.UniqueConstraint("tenant_id", "payment_id", "line_no", name="uq_payment_application_no"),
    )
    op.create_index(
        "ix_payment_application_tenant_id_billing_id",
        "payment_application",
        ["tenant_id", "billing_id"],
    )

    for table in reversed(TABLES):
        enable_tenant_rls(table)


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
