"""F08.4: billing_line_work_area.amount, a split across work areas (D-52)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-30

An earlier invoice line may be split across work areas by amount (D-52; the F08.4 brief,
the one table it changes). ``billing_line_work_area`` gains ``amount``
(``NUMERIC(14,2) NOT NULL``): the line's signed amount at the event, or the part of it the
row ties to its work area when the line is split; a credit memo's line is negative. The
rows one request writes share ``recorded_at`` (one event; Postgres ``now()`` is the
transaction's start) and the latest event's rows are the line's current rows; the unique
index ``uq_billing_line_work_area_event`` keeps a work area to one row per event, so one
current row per line and work area (the brief's Plan answer 1). The table stays
tenant-scoped, RLS forced and append-only; nothing else about it changes.

Data step, per tenant with tenant context (never unforcing RLS), one statement: every
existing row, ``cleared`` rows included, takes its line's amount with the document's sign.
A row's line always exists (``billing_line_id`` is ``ON DELETE RESTRICT``); a voided
document's lines carry what the void left (0.00), which no figure reads. The step reads
``billing_line`` and ``billing`` and writes only the new column; it never touches
``billing_line``. The table is append-only (D-13: its triggers refuse UPDATE for every
role, the owner included), so the step suspends the two triggers of this one table for the
migration's transaction and enables them again before the column is made NOT NULL, the
mechanism ``scripts/delete_tenant.py`` uses (D-28); the operator runs both on the server
as the owner, and the application role never can. Nothing a person recorded changes: the
rows keep their work area, note, user and time, and gain the amount their line carried.

Reversible: yes, while no line is split. The downgrade counts, per tenant, the lines whose
latest event has more than one row and refuses in one sentence naming the count (the table
is append-only; a split is ended through the application first), else drops the index and
the column. The downgrade guard refuses the ``wip`` database.
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

from app.tenancy.rls import append_only_trigger_names

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "billing_line_work_area"
EVENT_INDEX = "uq_billing_line_work_area_event"
EVENT_INDEX_WHERE = "action = 'assigned'"
BACKFILL_SQL = (
    f"UPDATE {TABLE} AS w "
    "SET amount = CASE WHEN b.kind = 'credit_memo' THEN -l.amount ELSE l.amount END "
    "FROM billing_line AS l JOIN billing AS b ON b.id = l.billing_id "
    "WHERE l.id = w.billing_line_id AND w.tenant_id = :t AND w.amount IS NULL"
)
SPLIT_LINES_SQL = (
    "SELECT count(*) FROM ("
    f"SELECT w.billing_line_id FROM {TABLE} AS w "
    f"WHERE w.tenant_id = :t AND w.recorded_at = ("
    f"SELECT max(x.recorded_at) FROM {TABLE} AS x "
    "WHERE x.tenant_id = :t AND x.billing_line_id = w.billing_line_id) "
    "GROUP BY w.billing_line_id HAVING count(*) > 1) AS split"
)


def _tenants(bind) -> list[UUID]:
    return [r.id for r in bind.execute(sa.text("SELECT id FROM tenant")).all()]


def _set_tenant(bind, tenant_id: UUID | None) -> None:
    value = "" if tenant_id is None else str(tenant_id)
    bind.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": value})


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("amount", sa.Numeric(14, 2), nullable=True))
    bind = op.get_bind()
    triggers = append_only_trigger_names(TABLE)
    for trigger in triggers:
        bind.execute(sa.text(f'ALTER TABLE "{TABLE}" DISABLE TRIGGER {trigger}'))
    for tenant_id in _tenants(bind):
        _set_tenant(bind, tenant_id)
        bind.execute(sa.text(BACKFILL_SQL), {"t": str(tenant_id)})
    _set_tenant(bind, None)
    for trigger in triggers:
        bind.execute(sa.text(f'ALTER TABLE "{TABLE}" ENABLE TRIGGER {trigger}'))
    op.alter_column(TABLE, "amount", nullable=False)
    op.create_index(
        EVENT_INDEX,
        TABLE,
        ["tenant_id", "billing_line_id", "recorded_at", "estimate_work_area_id"],
        unique=True,
        postgresql_where=sa.text(EVENT_INDEX_WHERE),
    )


def downgrade() -> None:
    bind = op.get_bind()
    split = 0
    for tenant_id in _tenants(bind):
        _set_tenant(bind, tenant_id)
        split += bind.execute(sa.text(SPLIT_LINES_SQL), {"t": str(tenant_id)}).scalar_one()
    _set_tenant(bind, None)
    if split:
        noun = "line is" if split == 1 else "lines are"
        raise RuntimeError(
            f"{split} invoice {noun} split across work areas; the amount column cannot be "
            "dropped while a split exists (end the splits in the application first; nothing "
            "was changed)."
        )
    op.drop_index(EVENT_INDEX, table_name=TABLE)
    op.drop_column(TABLE, "amount")
