"""F07.4: change_order_approval (D-42)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-06

The approval history of a job's change-order work areas (BLUEPRINT §5; D-42; the F07.4
brief with the owner's answers of 2026-10-06). One row per approval or withdrawal, never
edited: tenant-scoped (``tenant_id NOT NULL``, an index leading with it,
``enable_tenant_rls``) and append-only (``make_append_only``, listed in
``APPEND_ONLY_TABLES``, D-13). Nothing about whether an approval still applies is stored:
the row keeps the work area's order number, name and price that day, and the state is
computed on read against the versions received after it (rule C).

Two CHECKs keep each action's columns with it: an approval carries the agreed date and no
reason or ``withdraws_id``; a withdrawal carries a non-blank reason and the approval it
withdraws, and none of the approval's fields. The CHECK texts are literals, as 0006
explains.

Reversible: yes. The downgrade drops the table (its triggers and policy go with it; the
append-only function stays for the other tables). Approvals are people's decisions and
are not rebuilt from anything; the audit log keeps every one of them (one row per
action), and the downgrade guard refuses the ``wip`` database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls, make_append_only

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "change_order_approval"
ACTION_SQL = "action IN ('approved','withdrawn')"
APPROVED_SQL = (
    "action <> 'approved' OR (agreed_on IS NOT NULL AND reason IS NULL AND withdraws_id IS NULL)"
)
WITHDRAWN_SQL = (
    "action <> 'withdrawn' OR (reason IS NOT NULL AND btrim(reason) <> '' "
    "AND withdraws_id IS NOT NULL AND agreed_on IS NULL AND agreed_by IS NULL "
    "AND evidence_ref IS NULL AND note IS NULL)"
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
        _fk("job_id", "job.id", nullable=False),
        _fk("estimate_id", "estimate.id", nullable=False),
        sa.Column("order_no", sa.Integer(), nullable=False),
        sa.Column("work_area_name", sa.String(500), nullable=False),
        _fk("estimate_work_area_id", "estimate_work_area.id", nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("price", sa.Numeric(14, 2), nullable=False),
        sa.Column("agreed_on", sa.Date(), nullable=True),
        sa.Column("agreed_by", sa.String(200), nullable=True),
        sa.Column("evidence_ref", sa.String(500), nullable=True),
        sa.Column("note", sa.String(2000), nullable=True),
        sa.Column("reason", sa.String(2000), nullable=True),
        _fk("withdraws_id", f"{TABLE}.id"),
        _fk("recorded_by", "user.id", nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(ACTION_SQL, name=f"ck_{TABLE}_action"),
        sa.CheckConstraint(APPROVED_SQL, name=f"ck_{TABLE}_approved"),
        sa.CheckConstraint(WITHDRAWN_SQL, name=f"ck_{TABLE}_withdrawn"),
    )
    op.create_index(f"ix_{TABLE}_tenant_id_job_id", TABLE, ["tenant_id", "job_id", "recorded_at"])
    enable_tenant_rls(TABLE)
    make_append_only(TABLE)


def downgrade() -> None:
    op.drop_table(TABLE)
