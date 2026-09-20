"""F04 patch: import_batch status ``nothing_loaded``; import_batch.followup_outcome

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-20

Owner answers on 2792364 (2026-09-20). ``ck_import_batch_status`` gains
``nothing_loaded``: a file from which no row could be read, an end state that is not
``failed``. ``followup_outcome`` records what the batch's follow-on task did with it
(``applied``, or ``superseded`` when a newer chart had already been uploaded), so the
Imports page can say so. No existing row is rewritten by the upgrade: a batch that
ended ``loaded_with_issues`` with 0 rows loaded before this migration stays as it is.

Reversible: yes. The downgrade turns ``nothing_loaded`` rows back into
``loaded_with_issues`` (what such a batch was called before this migration; counts and
everything else are untouched) one tenant at a time with ``app.tenant_id`` set, never
disabling or unforcing RLS, then restores the old CHECK and drops the column (the
follow-on outcome is lost; the task row keeps its own status).

The CHECK texts are literals here, not imports from ``app.ingest.models``: a
migration must keep creating the constraint it created on the day it was written.
(0004 imported the model's constant and now carries its own literal for that reason.)
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_CHECK = "ck_import_batch_status"
STATUS_SQL_0004 = "status IN ('received','processing','loaded','loaded_with_issues','failed')"
STATUS_SQL_0006 = (
    "status IN ('received','processing','loaded','loaded_with_issues','nothing_loaded','failed')"
)
OUTCOME_CHECK = "ck_import_batch_followup_outcome"
OUTCOME_SQL = "followup_outcome IS NULL OR followup_outcome IN ('applied','superseded')"


def upgrade() -> None:
    op.drop_constraint(STATUS_CHECK, "import_batch", type_="check")
    op.create_check_constraint(STATUS_CHECK, "import_batch", STATUS_SQL_0006)
    op.add_column("import_batch", sa.Column("followup_outcome", sa.String(20), nullable=True))
    op.create_check_constraint(OUTCOME_CHECK, "import_batch", OUTCOME_SQL)


def _tenant_ids(bind) -> list[UUID]:
    return [r.id for r in bind.execute(sa.text("SELECT id FROM tenant")).all()]


def downgrade() -> None:
    bind = op.get_bind()
    for tenant_id in _tenant_ids(bind):
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        bind.execute(
            sa.text(
                "UPDATE import_batch SET status = 'loaded_with_issues' "
                "WHERE status = 'nothing_loaded'"
            )
        )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    op.drop_constraint(OUTCOME_CHECK, "import_batch", type_="check")
    op.drop_column("import_batch", "followup_outcome")
    op.drop_constraint(STATUS_CHECK, "import_batch", type_="check")
    op.create_check_constraint(STATUS_CHECK, "import_batch", STATUS_SQL_0004)
