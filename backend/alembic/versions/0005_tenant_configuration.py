"""F04: division, cost_category, gl_account, account_map, account_suggest_rule,
tenant_policy, burden_rate; import_batch.followup_task_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-18

Decisions: D-22, D-23. Every new table is tenant-scoped (``tenant_id`` NOT NULL, a
leading index or unique constraint, ``enable_tenant_rls``). No seed data: cost
categories are seeded per tenant by the audited service, never by a cross-tenant
statement. ``import_batch.followup_task_id`` (owner amendment C) links a batch to
the task enqueued when it loaded. Reversible: yes; the downgrade drops the column
and the seven tables (policies, indexes and constraints go with them).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.domain.config.models import (
    BURDEN_PERIOD_CHECK,
    BURDEN_PERIOD_CHECK_SQL,
    MAP_STATUS_CHECK,
    MAP_STATUS_CHECK_SQL,
    PATTERN_MAX_LENGTH,
    RULE_CATEGORY_CHECK,
    RULE_CATEGORY_CHECK_SQL,
    RULE_DIVISION_CHECK,
    RULE_DIVISION_CHECK_SQL,
)
from app.tenancy.rls import enable_tenant_rls

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "burden_rate",
    "tenant_policy",
    "account_suggest_rule",
    "account_map",
    "gl_account",
    "cost_category",
    "division",
)


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


def _bool(name: str, default: str) -> sa.Column:
    return sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    op.create_table(
        "division",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("code_digit", sa.String(1), nullable=True),
        _bool("active", "true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        _now("created_at"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_division_tenant_code"),
    )
    op.create_index(
        "uq_division_tenant_code_digit",
        "division",
        ["tenant_id", "code_digit"],
        unique=True,
        postgresql_where=sa.text("code_digit IS NOT NULL"),
    )
    enable_tenant_rls("division")

    op.create_table(
        "cost_category",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("slot", sa.String(2), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        _bool("active", "true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        _now("created_at"),
        sa.UniqueConstraint("tenant_id", "slot", name="uq_cost_category_tenant_slot"),
    )
    enable_tenant_rls("cost_category")

    op.create_table(
        "gl_account",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("account_no", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("ledger_type", sa.String(60), nullable=False, server_default=""),
        _bool("active", "true"),
        sa.Column("source", sa.String(20), nullable=False, server_default="file"),
        sa.Column("external_id", sa.String(80), nullable=True),
        _fk("raw_record_id", "raw_record.id"),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("tenant_id", "account_no", name="uq_gl_account_tenant_no"),
    )
    enable_tenant_rls("gl_account")

    op.create_table(
        "account_map",
        _uuid_pk(),
        _tenant_id(),
        _fk("gl_account_id", "gl_account.id", nullable=False),
        _fk("division_id", "division.id"),
        _fk("cost_category_id", "cost_category.id"),
        sa.Column("in_job_cost", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="suggested"),
        sa.Column("suggested_by_rule", sa.String(100), nullable=True),
        _fk("confirmed_by", "user.id"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        _now("updated_at"),
        sa.UniqueConstraint("tenant_id", "gl_account_id", name="uq_account_map_tenant_account"),
        sa.CheckConstraint(MAP_STATUS_CHECK_SQL, name=MAP_STATUS_CHECK),
    )
    enable_tenant_rls("account_map")

    op.create_table(
        "account_suggest_rule",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("pattern", sa.String(PATTERN_MAX_LENGTH), nullable=False),
        _fk("division_id", "division.id"),
        sa.Column("division_from_digit", sa.Integer(), nullable=True),
        _fk("cost_category_id", "cost_category.id"),
        _bool("cost_category_from_slot", "false"),
        sa.Column("in_job_cost", sa.Boolean(), nullable=False),
        _bool("active", "true"),
        _now("created_at"),
        sa.UniqueConstraint("tenant_id", "sort_order", name="uq_account_suggest_rule_tenant_order"),
        sa.CheckConstraint(RULE_DIVISION_CHECK_SQL, name=RULE_DIVISION_CHECK),
        sa.CheckConstraint(RULE_CATEGORY_CHECK_SQL, name=RULE_CATEGORY_CHECK),
    )
    enable_tenant_rls("account_suggest_rule")

    op.create_table(
        "tenant_policy",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        _fk("decided_by", "user.id"),
        _now("decided_at"),
        sa.Column("decision_ref", sa.String(200), nullable=False),
        sa.UniqueConstraint("tenant_id", "key", name="uq_tenant_policy_tenant_key"),
    )
    enable_tenant_rls("tenant_policy")

    op.create_table(
        "burden_rate",
        _uuid_pk(),
        _tenant_id(),
        _fk("division_id", "division.id"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("rate", sa.Numeric(7, 4), nullable=False),
        sa.Column("basis_note", sa.String(500), nullable=True),
        _bool("active", "true"),
        _now("created_at"),
        sa.CheckConstraint(BURDEN_PERIOD_CHECK_SQL, name=BURDEN_PERIOD_CHECK),
    )
    op.create_index(
        "ix_burden_rate_tenant_id_effective_from", "burden_rate", ["tenant_id", "effective_from"]
    )
    enable_tenant_rls("burden_rate")

    op.add_column(
        "import_batch",
        sa.Column(
            "followup_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("import_batch", "followup_task_id")
    for table in TABLES:
        op.drop_table(table)
