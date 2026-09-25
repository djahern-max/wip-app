"""F06: estimate, estimate_version, estimate_work_area, estimate_cost; import_batch.issues

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-25

The estimate tables of the spine (BLUEPRINT §7; D-01, D-32; the F06 brief as
re-planned 2026-09-25). All tenant-scoped: ``tenant_id NOT NULL``, an index or unique
constraint leading with it, ``enable_tenant_rls``. Money is ``NUMERIC(14,2)``, hours
``NUMERIC(9,2)``. ``estimate_cost`` hangs off ``estimate_work_area`` (D-32) and keeps
the cost code as text beside the resolved division and category, so an unknown code
is loaded and reported rather than lost.

``import_batch.issues`` (JSONB, nullable) holds the sentences for rows a file could
not load and the file-level ``EST_*`` facts (plan question 5); the follow-up outcome
CHECK gains ``unchanged`` (a file whose contents were already loaded).

Reversible: yes. The downgrade drops the four tables (their rows are rebuilt from
``raw_record`` by the normalizer, which is idempotent), drops ``issues`` and, one
tenant at a time with ``app.tenant_id`` set and RLS untouched, turns ``unchanged``
outcomes into ``applied`` before restoring the 0006 CHECK. The CHECK texts are
literals, as 0006 explains.
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("estimate_cost", "estimate_work_area", "estimate_version", "estimate")
MONEY = sa.Numeric(14, 2)
HOURS = sa.Numeric(9, 2)
OUTCOME_CHECK = "ck_import_batch_followup_outcome"
OUTCOME_SQL_0006 = "followup_outcome IS NULL OR followup_outcome IN ('applied','superseded')"
OUTCOME_SQL_0010 = (
    "followup_outcome IS NULL OR followup_outcome IN ('applied','superseded','unchanged')"
)
STATUS_NORM_SQL = "status_norm IS NULL OR status_norm IN ('pending','sold','lost')"


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
        "estimate",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("estimator", sa.String(200), nullable=True),
        sa.Column("client_name", sa.String(500), nullable=True),
        sa.Column("jobsite", sa.String(500), nullable=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("status_norm", sa.String(20), nullable=True),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("estimate_date", sa.Date, nullable=True),
        _fk("raw_record_id", "raw_record.id"),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint(
            "tenant_id", "source", "external_id", name="uq_estimate_tenant_source_id"
        ),
        sa.CheckConstraint(STATUS_NORM_SQL, name="ck_estimate_status_norm"),
    )
    op.create_index("ix_estimate_tenant_id_status_norm", "estimate", ["tenant_id", "status_norm"])
    op.create_index("ix_estimate_tenant_id_estimator", "estimate", ["tenant_id", "estimator"])

    op.create_table(
        "estimate_version",
        _uuid_pk(),
        _tenant_id(),
        _fk("estimate_id", "estimate.id", nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False),
        _fk("import_batch_id", "import_batch.id", nullable=False),
        _fk("header_raw_record_id", "raw_record.id"),
        _fk("work_areas_raw_record_id", "raw_record.id"),
        _fk("cost_lines_raw_record_id", "raw_record.id"),
        _now("received_at"),
        sa.Column("status_norm", sa.String(20), nullable=True),
        sa.Column("kept_total", MONEY, nullable=True),
        sa.Column("is_baseline", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.UniqueConstraint(
            "tenant_id", "estimate_id", "version_no", name="uq_estimate_version_no"
        ),
    )
    op.create_index(
        "uq_estimate_version_baseline",
        "estimate_version",
        ["tenant_id", "estimate_id"],
        unique=True,
        postgresql_where=sa.text("is_baseline"),
    )

    op.create_table(
        "estimate_work_area",
        _uuid_pk(),
        _tenant_id(),
        _fk("estimate_version_id", "estimate_version.id", nullable=False),
        sa.Column("order_no", sa.Integer, nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("kept", sa.Boolean, nullable=False),
        sa.Column("change_order_suggested", sa.Boolean, nullable=False),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("notes", sa.String(2000), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "estimate_version_id", "order_no", name="uq_estimate_work_area_order"
        ),
    )

    op.create_table(
        "estimate_cost",
        _uuid_pk(),
        _tenant_id(),
        _fk("estimate_work_area_id", "estimate_work_area.id", nullable=False),
        _fk("cost_category_id", "cost_category.id"),
        _fk("division_id", "division.id"),
        sa.Column("cost_code", sa.String(20), nullable=False),
        sa.Column("hours", HOURS, nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("notes", sa.String(2000), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "estimate_work_area_id", "cost_code", name="uq_estimate_cost_code"
        ),
    )
    op.create_index(
        "ix_estimate_cost_tenant_id_division_id", "estimate_cost", ["tenant_id", "division_id"]
    )

    for table in reversed(TABLES):
        enable_tenant_rls(table)

    op.add_column("import_batch", sa.Column("issues", postgresql.JSONB(), nullable=True))
    op.drop_constraint(OUTCOME_CHECK, "import_batch", type_="check")
    op.create_check_constraint(OUTCOME_CHECK, "import_batch", OUTCOME_SQL_0010)


def _tenant_ids(bind) -> list[UUID]:
    return [r.id for r in bind.execute(sa.text("SELECT id FROM tenant")).all()]


def downgrade() -> None:
    bind = op.get_bind()
    for tenant_id in _tenant_ids(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        bind.execute(
            sa.text(
                "UPDATE import_batch SET followup_outcome = 'applied' "
                "WHERE followup_outcome = 'unchanged'"
            )
        )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    op.drop_constraint(OUTCOME_CHECK, "import_batch", type_="check")
    op.create_check_constraint(OUTCOME_CHECK, "import_batch", OUTCOME_SQL_0006)
    op.drop_column("import_batch", "issues")
    for table in TABLES:
        op.drop_table(table)
