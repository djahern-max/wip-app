"""F07: job, job_estimate, job_alias; estimate_work_area kind confirmation

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-29

The job spine (BLUEPRINT §5; D-01, D-03, D-24, D-30, D-31; the F07 brief with the
owner's answers of 2026-09-29). All three tables are tenant-scoped: ``tenant_id NOT
NULL``, an index or unique constraint leading with it, ``enable_tenant_rls``. No
contract amount is stored: the revised contract is computed on read (D-01).

- ``job.revenue_method`` carries the five D-31 values; only ``fixed_price`` reaches
  the WIP schedule.
- ``job_estimate``: unique per estimate (an estimate is on at most one job); at most
  one ``original`` per job (partial unique index); an ``ignored`` row needs a note.
- ``job_alias``: unique per (system, external_id); a job may carry several.
- ``estimate_work_area`` gains ``kind``, ``kind_confirmed_by``, ``kind_confirmed_at``,
  nullable with no default: NULL means "not confirmed", so no existing row is
  rewritten. A CHECK keeps the three together.

The CHECK texts are literals, as 0006 explains.

Reversible: yes. The downgrade drops the three tables and the three columns. Jobs,
attachments, aliases and kind confirmations are people's decisions and are not
rebuilt from ``raw_record``; the audit log keeps every one of them (one row per
action), and the downgrade guard refuses the ``wip`` database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("job_alias", "job_estimate", "job")
REVENUE_METHOD_SQL = (
    "revenue_method IN ('fixed_price','time_and_materials','recurring_service','none','pool')"
)
STATUS_SQL = "status IN ('sold','in_progress','substantially_complete','closed','cancelled')"
ROLE_SQL = "role IN ('original','change_order','ignored')"
IGNORED_NOTE_SQL = "role <> 'ignored' OR (note IS NOT NULL AND btrim(note) <> '')"
SYSTEM_SQL = "system IN ('lmn_estimate','qbo_customer')"
KIND_CHECK = "ck_estimate_work_area_kind"
KIND_SQL = (
    "(kind IS NULL AND kind_confirmed_by IS NULL AND kind_confirmed_at IS NULL) "
    "OR (kind IN ('original','change_order') AND kind_confirmed_by IS NOT NULL "
    "AND kind_confirmed_at IS NOT NULL)"
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


def upgrade() -> None:
    op.create_table(
        "job",
        _uuid_pk(),
        _tenant_id(),
        _fk("customer_id", "customer.id"),
        sa.Column("name", sa.String(500), nullable=False),
        _fk("division_id", "division.id"),
        sa.Column("revenue_method", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("sold_on", sa.Date, nullable=False),
        sa.Column("notes", sa.String(4000), nullable=True),
        _fk("created_by", "user.id"),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(REVENUE_METHOD_SQL, name="ck_job_revenue_method"),
        sa.CheckConstraint(STATUS_SQL, name="ck_job_status"),
    )
    op.create_index("ix_job_tenant_id_status", "job", ["tenant_id", "status"])
    op.create_index("ix_job_tenant_id_customer_id", "job", ["tenant_id", "customer_id"])

    op.create_table(
        "job_estimate",
        _uuid_pk(),
        _tenant_id(),
        _fk("job_id", "job.id", nullable=False),
        _fk("estimate_id", "estimate.id", nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        _fk("attached_by", "user.id"),
        _now("attached_at"),
        sa.Column("note", sa.String(2000), nullable=True),
        sa.UniqueConstraint("tenant_id", "estimate_id", name="uq_job_estimate_estimate"),
        sa.CheckConstraint(ROLE_SQL, name="ck_job_estimate_role"),
        sa.CheckConstraint(IGNORED_NOTE_SQL, name="ck_job_estimate_ignored_note"),
    )
    op.create_index("ix_job_estimate_tenant_id_job_id", "job_estimate", ["tenant_id", "job_id"])
    op.create_index(
        "uq_job_estimate_one_original",
        "job_estimate",
        ["tenant_id", "job_id"],
        unique=True,
        postgresql_where=sa.text("role = 'original'"),
    )

    op.create_table(
        "job_alias",
        _uuid_pk(),
        _tenant_id(),
        _fk("job_id", "job.id", nullable=False),
        sa.Column("system", sa.String(30), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        _fk("linked_by", "user.id"),
        _now("linked_at"),
        sa.UniqueConstraint("tenant_id", "system", "external_id", name="uq_job_alias_system_id"),
        sa.CheckConstraint(SYSTEM_SQL, name="ck_job_alias_system"),
    )
    op.create_index("ix_job_alias_tenant_id_job_id", "job_alias", ["tenant_id", "job_id"])

    for table in reversed(TABLES):
        enable_tenant_rls(table)

    op.add_column("estimate_work_area", sa.Column("kind", sa.String(20), nullable=True))
    op.add_column("estimate_work_area", _fk("kind_confirmed_by", "user.id"))
    op.add_column(
        "estimate_work_area",
        sa.Column("kind_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(KIND_CHECK, "estimate_work_area", KIND_SQL)


def downgrade() -> None:
    op.drop_constraint(KIND_CHECK, "estimate_work_area", type_="check")
    op.drop_column("estimate_work_area", "kind_confirmed_at")
    op.drop_column("estimate_work_area", "kind_confirmed_by")
    op.drop_column("estimate_work_area", "kind")
    for table in TABLES:
        op.drop_table(table)
