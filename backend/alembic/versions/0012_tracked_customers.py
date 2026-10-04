"""F07.2: customer.tracked_at and customer.tracked_by (D-37)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-04

A person picks the QuickBooks customers and projects the platform works on (D-37). The
flag lives on ``customer`` as two nullable columns, no new table: ``tracked_at`` (NULL
means not tracked) and ``tracked_by`` (the user). A CHECK keeps ``tracked_by`` NULL
whenever ``tracked_at`` is. No default, so the schema step rewrites no row. ``customer``
already carries ``tenant_id NOT NULL``, an index leading with it and forced RLS.

Data step, per tenant with tenant context (never unforcing RLS): a row already linked
to a job (``job_alias.system = 'qbo_customer'``) is tracked as of the link, with
``tracked_at = linked_at`` and ``tracked_by = linked_by`` (the user who made the link).
No audit row is written: the person's action was the link, and its ``job_alias_linked``
row is the record; D-37 says the link tracks the row.

Reversible: yes. The downgrade drops the CHECK, the FK and the two columns. A row
tracked by the picker loses the flag; its ``customer_tracked`` audit row keeps the fact,
and the upgrade's data step restores the flag on every linked row. The downgrade guard
refuses the ``wip`` database.
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRACKED_CHECK = "ck_customer_tracked"
TRACKED_SQL = "tracked_by IS NULL OR tracked_at IS NOT NULL"
TRACK_LINKED_SQL = (
    "UPDATE customer AS c SET tracked_at = a.linked_at, tracked_by = a.linked_by "
    "FROM job_alias AS a "
    "WHERE c.tenant_id = :t AND a.tenant_id = :t "
    "AND a.system = 'qbo_customer' AND c.source = 'qbo' "
    "AND c.external_id = a.external_id AND c.tracked_at IS NULL"
)


def _tenants(bind) -> list[UUID]:
    return [r.id for r in bind.execute(sa.text("SELECT id FROM tenant")).all()]


def _set_tenant(bind, tenant_id: UUID | None) -> None:
    value = "" if tenant_id is None else str(tenant_id)
    bind.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": value})


def upgrade() -> None:
    op.add_column("customer", sa.Column("tracked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "customer",
        sa.Column(
            "tracked_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_check_constraint(TRACKED_CHECK, "customer", TRACKED_SQL)

    # --- data step: linked rows are tracked as of the link, per tenant ---------------
    bind = op.get_bind()
    for tenant_id in _tenants(bind):
        _set_tenant(bind, tenant_id)
        bind.execute(sa.text(TRACK_LINKED_SQL), {"t": str(tenant_id)})
    _set_tenant(bind, None)


def downgrade() -> None:
    op.drop_constraint(TRACKED_CHECK, "customer", type_="check")
    op.drop_column("customer", "tracked_by")
    op.drop_column("customer", "tracked_at")
