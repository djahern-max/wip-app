"""F08.1 Part 2: pay_application, pay_application_line (D-36, D-39, D-42, D-43)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-07

The pay application (BLUEPRINT §5, §7, §9; D-36; the F08.1 brief, Plan answers 5 and 8,
the owner's yes of 2026-10-07). ``pay_application``: one row per application on a
fixed-price job, numbered per job (the first takes one more than the highest ``_PMT``
number on the job's invoices, owner's answer B), status ``draft``, ``issued`` or
``void``; the person's surcharge choice (D-39, no default); and, frozen at issue (the
owner's yes on answer 5): the surcharge rate that day, ``billed_before`` (the job's
billed to date before this application as QuickBooks had it, owner's answer A) and
``amount_due``. ``pay_application_line``: the schedule of values as it stood at issue,
one row per listed work area with its scheduled value (the price that day) and the
cumulative percent complete; nothing else is derived and stored (earned to date, earned
on previous applications, earned this application and balance to finish are computed
from these). No retainage column anywhere (D-43).

Both tenant-scoped (``tenant_id NOT NULL``, an index leading with it,
``enable_tenant_rls``). Not append-only: a draft is edited until issued, and issue and
void are the two updates of the one row; an issued application's lines and figures are
never changed by any route (service-enforced and tested). The CHECK texts are literals,
as 0006 explains.

Reversible: yes. The downgrade drops the two tables (the applications are people's
documents, each issue and void with its audit row; the guard refuses ``wip``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP = "pay_application"
LINE = "pay_application_line"
STATUS_SQL = "status IN ('draft','issued','void')"
ISSUED_SQL = (
    "status = 'draft' OR (issued_by IS NOT NULL AND issued_at IS NOT NULL "
    "AND billed_before IS NOT NULL AND amount_due IS NOT NULL)"
)
VOID_SQL = (
    "(status = 'void') = (voided_by IS NOT NULL AND voided_at IS NOT NULL "
    "AND void_reason IS NOT NULL AND btrim(void_reason) <> '')"
)
PERCENT_SQL = "percent_complete >= 0 AND percent_complete <= 100"


def _fk(name: str, target: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete="RESTRICT"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        APP,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _fk("tenant_id", "tenant.id", nullable=False),
        _fk("job_id", "job.id", nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("application_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("surcharge_applies", sa.Boolean(), nullable=True),
        sa.Column("surcharge_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("billed_before", sa.Numeric(14, 2), nullable=True),
        sa.Column("amount_due", sa.Numeric(14, 2), nullable=True),
        _fk("created_by", "user.id", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _fk("issued_by", "user.id"),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        _fk("voided_by", "user.id"),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.String(2000), nullable=True),
        sa.UniqueConstraint("tenant_id", "job_id", "number", name=f"uq_{APP}_number"),
        sa.CheckConstraint(STATUS_SQL, name=f"ck_{APP}_status"),
        sa.CheckConstraint(ISSUED_SQL, name=f"ck_{APP}_issued"),
        sa.CheckConstraint(VOID_SQL, name=f"ck_{APP}_void"),
    )
    op.create_index(f"ix_{APP}_tenant_id_job_id", APP, ["tenant_id", "job_id", "number"])
    op.create_table(
        LINE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _fk("tenant_id", "tenant.id", nullable=False),
        _fk("pay_application_id", f"{APP}.id", nullable=False),
        _fk("estimate_id", "estimate.id", nullable=False),
        sa.Column("order_no", sa.Integer(), nullable=False),
        sa.Column("work_area_name", sa.String(500), nullable=False),
        _fk("estimate_work_area_id", "estimate_work_area.id", nullable=False),
        sa.Column("scheduled_value", sa.Numeric(14, 2), nullable=False),
        sa.Column("percent_complete", sa.Numeric(5, 2), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "pay_application_id", "estimate_id", "order_no", name=f"uq_{LINE}_area"
        ),
        sa.CheckConstraint(PERCENT_SQL, name=f"ck_{LINE}_percent"),
    )
    op.create_index(f"ix_{LINE}_tenant_id_application", LINE, ["tenant_id", "pay_application_id"])
    enable_tenant_rls(APP)
    enable_tenant_rls(LINE)


def downgrade() -> None:
    op.drop_table(LINE)
    op.drop_table(APP)
