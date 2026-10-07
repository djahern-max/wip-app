"""F08.1 Part 1: billing_line_work_area (D-45)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-07

A person's assignment of an earlier invoice, credit-memo or sales-receipt line to a
work area (BLUEPRINT §5, §7; D-45; the F08.1 brief, Plan answers 2 and 8, the owner's
yes of 2026-10-07). One row per event, never edited: ``assigned`` or ``cleared``; the
latest row for a line is its current state. Tenant-scoped (``tenant_id NOT NULL``, an
index leading with it, ``enable_tenant_rls``) and append-only (``make_append_only``,
listed in ``APPEND_ONLY_TABLES``, D-13). The tie applies by ``(estimate_id, order_no)``
and follows the number through later versions, as a "#n" line does (owner's answer 5);
the stored name and the row pressed are the record. Nothing derived is stored: billed
to date per work area is computed on read.

One CHECK keeps the four work-area columns all set for ``assigned`` and all NULL for
``cleared``. The CHECK texts are literals, as 0006 explains. No ``job_id``: a line belongs
to a job through its document's customer row, and the read is by the job's line ids.

Reversible: yes. The downgrade drops the table (its triggers and policy go with it; the
append-only function stays for the other tables). Assignments are people's decisions,
each with its audit row, and the downgrade guard refuses the ``wip`` database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls, make_append_only

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "billing_line_work_area"
ACTION_SQL = "action IN ('assigned','cleared')"
COLUMNS_SQL = (
    "(action = 'assigned' AND estimate_id IS NOT NULL AND order_no IS NOT NULL "
    "AND work_area_name IS NOT NULL AND estimate_work_area_id IS NOT NULL) "
    "OR (action = 'cleared' AND estimate_id IS NULL AND order_no IS NULL "
    "AND work_area_name IS NULL AND estimate_work_area_id IS NULL)"
)


def _fk(name: str, target: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete="RESTRICT"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _fk("tenant_id", "tenant.id", nullable=False),
        _fk("billing_line_id", "billing_line.id", nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        _fk("estimate_id", "estimate.id"),
        sa.Column("order_no", sa.Integer(), nullable=True),
        sa.Column("work_area_name", sa.String(500), nullable=True),
        _fk("estimate_work_area_id", "estimate_work_area.id"),
        sa.Column("note", sa.String(2000), nullable=True),
        _fk("recorded_by", "user.id", nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(ACTION_SQL, name=f"ck_{TABLE}_action"),
        sa.CheckConstraint(COLUMNS_SQL, name=f"ck_{TABLE}_columns"),
    )
    op.create_index(
        f"ix_{TABLE}_tenant_id_line", TABLE, ["tenant_id", "billing_line_id", "recorded_at"]
    )
    enable_tenant_rls(TABLE)
    make_append_only(TABLE)


def downgrade() -> None:
    op.drop_table(TABLE)
