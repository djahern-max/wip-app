"""F05: connection gains the QuickBooks company, token bookkeeping and the pending
OAuth state; one company per tenant; sync_run.detail

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20

``connection``: ``realm_id``, ``environment``, ``company_name``,
``refresh_token_expires_at``, ``tokens_refreshed_at`` and the three
``oauth_state_*`` columns (the SHA-256 of the pending ``state``, the user who
started it, its expiry). A partial unique index on ``(system, realm_id)`` makes one
external company belong to one tenant: a unique index is enforced whatever RLS
shows the session, which is the only way the application role can be refused a
company another tenant holds. The table keeps its tenant-leading unique constraint
and its one ``tenant_isolation`` policy; no policy is added.

``sync_run.detail`` (JSONB): what a run found, as entity names, counts and amounts
only (the nightly drift check writes it). Never a payload.

No table is created here: ``customer``, ``billing``, ``billing_line``, ``payment``
and ``payment_application`` follow in their own migration once spike S-01 has
confirmed their shape (F05 brief, order of work).

Reversible: yes. The downgrade drops the index and the columns; their contents
(company link, pending state, run detail) are lost, tokens and runs are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REALM_INDEX = "uq_connection_system_realm_id"
STATE_USER_FK = "fk_connection_oauth_state_user_id_user"


def _connection_columns() -> list[sa.Column]:
    return [
        sa.Column("realm_id", sa.String(40), nullable=True),
        sa.Column("environment", sa.String(20), nullable=True),
        sa.Column("company_name", sa.String(200), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tokens_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oauth_state_sha256", sa.String(64), nullable=True),
        sa.Column("oauth_state_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("oauth_state_expires_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    for column in _connection_columns():
        op.add_column("connection", column)
    op.create_foreign_key(
        STATE_USER_FK, "connection", "user", ["oauth_state_user_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(
        REALM_INDEX,
        "connection",
        ["system", "realm_id"],
        unique=True,
        postgresql_where=sa.text("realm_id IS NOT NULL"),
    )
    op.add_column("sync_run", sa.Column("detail", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_run", "detail")
    op.drop_index(REALM_INDEX, table_name="connection")
    op.drop_constraint(STATE_USER_FK, "connection", type_="foreignkey")
    for column in reversed(_connection_columns()):
        op.drop_column("connection", column.name)
