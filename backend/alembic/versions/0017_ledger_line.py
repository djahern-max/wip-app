"""F10: ledger_line; the exception subject may be the tenant (D-20, D-22, D-46, D-51)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-29

The cost side of the ledger (BLUEPRINT §7 "Cost", §8.5 check 2; the F10 brief, the one
table it approves; D-51). ``ledger_line``: one row per QuickBooks line that posts to an
expense-side account, from a Bill, a Vendor Credit, a Purchase, a Journal Entry or a
Deposit, with the document's type, id, date and number, the line's id and position, the
account (the chart row by QuickBooks id, and the QuickBooks id itself for an account the
chart does not hold), the vendor, the line's customer reference and the job it aliases to
(null is unassigned, D-51), the signed amount as QuickBooks posted it, the description, the
document's memo, ``deleted_at`` for a source delete or a void, and the raw version it was
built from. Division, cost category, whether the account is job cost and whether its
category is in the WIP basis are not stored: they are read from ``account_map`` and
``tenant_policy`` when a figure is computed. Tenant-scoped (``tenant_id NOT NULL``, an index
leading with it, ``enable_tenant_rls``). Not append-only: a changed document rewrites its
lines (the F05 rule). The CHECK text is a literal, as 0006 explains.

Also: ``exception.subject_type`` may now be ``tenant`` (``COST_UNASSIGNED`` on a line coded
to no customer is about the company, not a row; the F10 brief), so the subject check of
0016 is replaced.

Reversible: yes. The downgrade drops ``ledger_line`` (the lines are a copy of QuickBooks and
are rebuilt by the next normalize run) and puts the 0016 subject check back ``NOT VALID``:
an exception raised with the tenant as its subject keeps its append-only history (D-13), so
such rows, if any, stay and the narrower check applies to new rows only. The guard refuses
``wip``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINE = "ledger_line"
TXN_TYPE_SQL = "txn_type IN ('Bill','VendorCredit','Purchase','JournalEntry','Deposit')"
EXC = "exception"
SUBJECT_CHECK = f"ck_{EXC}_subject"
SUBJECT_SQL_0016 = "subject_type IN ('job','estimate','customer')"
SUBJECT_SQL = "subject_type IN ('job','estimate','customer','tenant')"


def _fk(name: str, target: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete="RESTRICT"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        LINE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _fk("tenant_id", "tenant.id", nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("txn_type", sa.String(20), nullable=False),
        sa.Column("txn_external_id", sa.String(80), nullable=False),
        sa.Column("line_external_id", sa.String(80), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("doc_number", sa.String(40), nullable=True),
        _fk("gl_account_id", "gl_account.id"),
        sa.Column("account_external_id", sa.String(80), nullable=True),
        sa.Column("vendor_external_id", sa.String(80), nullable=True),
        sa.Column("customer_external_id", sa.String(80), nullable=True),
        _fk("job_id", "job.id"),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("description", sa.String(4000), nullable=True),
        sa.Column("memo", sa.String(4000), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _fk("raw_record_id", "raw_record.id", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source",
            "txn_type",
            "txn_external_id",
            "line_external_id",
            name=f"uq_{LINE}_tenant_source_txn_line",
        ),
        sa.CheckConstraint(TXN_TYPE_SQL, name=f"ck_{LINE}_txn_type"),
    )
    op.create_index(f"ix_{LINE}_tenant_id_txn_date", LINE, ["tenant_id", "txn_date"])
    op.create_index(f"ix_{LINE}_tenant_id_job_id", LINE, ["tenant_id", "job_id"])
    op.create_index(f"ix_{LINE}_tenant_id_gl_account_id", LINE, ["tenant_id", "gl_account_id"])
    enable_tenant_rls(LINE)
    op.drop_constraint(SUBJECT_CHECK, EXC, type_="check")
    op.create_check_constraint(SUBJECT_CHECK, EXC, SUBJECT_SQL)


def downgrade() -> None:
    op.drop_constraint(SUBJECT_CHECK, EXC, type_="check")
    op.execute(
        f'ALTER TABLE "{EXC}" ADD CONSTRAINT {SUBJECT_CHECK} CHECK ({SUBJECT_SQL_0016}) NOT VALID'
    )
    op.drop_table(LINE)
