"""F05.1: webhook_event (tenant-less, append-only); connection.last_webhook_at

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-22

``webhook_event`` (D-29) is the one ingestion table without ``tenant_id``: a
delivery is stored before anyone knows which tenant holds the realm, so the row
cannot carry a tenant and RLS is not enabled on it. It is insert-only
(``make_append_only``; ``APPEND_ONLY_TABLES``). A partial unique index on
``event_id`` makes a retried delivery (Intuit resends with the same CloudEvents id)
store once. ``connection.last_webhook_at`` is when a delivery last named the
company's realm, written by the dispatcher.

Reversible: yes. The downgrade drops the column and the table (its triggers go with
it; the trigger function stays, other tables use it). Stored deliveries are lost;
they are triggers, not data (the poll is the consumer).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import make_append_only

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_ID_INDEX = "uq_webhook_event_event_id"


def upgrade() -> None:
    op.create_table(
        "webhook_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("event_id", sa.String(120), nullable=True),
        sa.Column("realm_id", sa.String(40), nullable=True),
        sa.Column("event_type", sa.String(80), nullable=True),
        sa.Column("entity_id", sa.String(80), nullable=True),
        sa.Column("entity_count", sa.Integer, nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("intuit_tid", sa.String(80), nullable=True),
        sa.Column("schema_version", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_webhook_event_realm_id_received_at", "webhook_event", ["realm_id", "received_at"]
    )
    op.create_index(
        EVENT_ID_INDEX,
        "webhook_event",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("event_id IS NOT NULL"),
    )
    make_append_only("webhook_event")
    op.add_column(
        "connection", sa.Column("last_webhook_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("connection", "last_webhook_at")
    op.drop_table("webhook_event")
