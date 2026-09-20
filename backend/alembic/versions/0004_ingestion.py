"""F03: connection, sync_run, import_batch, raw_record (append-only), task

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17

Decisions: D-19 (tenant-scoped worker queue), D-20 (``raw_record`` insert-only,
versioned), D-21 (object storage behind a protocol; only ``object_key`` lives here).

Every table is tenant-scoped: ``tenant_id`` NOT NULL, an index or unique
constraint leading with it, ``enable_tenant_rls``. ``raw_record`` calls
``make_append_only`` and is listed in ``APPEND_ONLY_TABLES``. ``task`` carries a
partial unique index on ``(tenant_id, kind, dedupe_key)`` for non-terminal rows.
No data step. Reversible: yes; downgrade drops the five tables (policies, indexes
and triggers go with them; the trigger function stays for the audit tables).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.ingest.models import (
    IMPORT_STATUS_CHECK,
    RAW_ORIGIN_CHECK,
    RAW_ORIGIN_CHECK_SQL,
)
from app.tenancy.rls import enable_tenant_rls, make_append_only
from app.worker.models import (
    DEDUPE_INDEX,
    DEDUPE_INDEX_WHERE,
    TASK_STATUS_CHECK,
    TASK_STATUS_CHECK_SQL,
)

# The status list as it was on 2026-09-17. A literal since 0006 changed the model's
# constant: this migration must keep creating the constraint it always created.
IMPORT_STATUS_CHECK_SQL = (
    "status IN ('received','processing','loaded','loaded_with_issues','failed')"
)

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _tenant_id() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def upgrade() -> None:
    op.create_table(
        "connection",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("system", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="disconnected"),
        _ts("last_success_at"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("access_token_key_id", sa.String(40), nullable=True),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_key_id", sa.String(40), nullable=True),
        _ts("token_expires_at"),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.UniqueConstraint("tenant_id", "system", name="uq_connection_tenant_system"),
    )
    enable_tenant_rls("connection")

    op.create_table(
        "sync_run",
        _uuid_pk(),
        _tenant_id(),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connection.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        _ts("started_at", nullable=False, now=True),
        _ts("finished_at"),
        sa.Column("outcome", sa.String(20), nullable=True),
        sa.Column("records_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_stored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
    )
    op.create_index("ix_sync_run_tenant_id_started_at", "sync_run", ["tenant_id", "started_at"])
    enable_tenant_rls("sync_run")

    op.create_table(
        "import_batch",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=True),
        sa.Column("object_key", sa.String(300), nullable=False),
        sa.Column(
            "uploaded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        _ts("uploaded_at", nullable=False, now=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("rows_loaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(500), nullable=True),
        _ts("processed_at"),
        sa.UniqueConstraint(
            "tenant_id", "source_kind", "sha256", name="uq_import_batch_tenant_source_sha256"
        ),
        sa.CheckConstraint(IMPORT_STATUS_CHECK_SQL, name=IMPORT_STATUS_CHECK),
    )
    op.create_index(
        "ix_import_batch_tenant_id_uploaded_at", "import_batch", ["tenant_id", "uploaded_at"]
    )
    enable_tenant_rls("import_batch")

    op.create_table(
        "raw_record",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("fetched_at", nullable=False, now=True),
        sa.Column(
            "import_batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("import_batch.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "sync_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sync_run.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source",
            "entity_type",
            "external_id",
            "version",
            name="uq_raw_record_key_version",
        ),
        sa.CheckConstraint(RAW_ORIGIN_CHECK_SQL, name=RAW_ORIGIN_CHECK),
    )
    op.create_index(
        "ix_raw_record_tenant_id_import_batch_id", "raw_record", ["tenant_id", "import_batch_id"]
    )
    enable_tenant_rls("raw_record")
    make_append_only("raw_record")

    op.create_table(
        "task",
        _uuid_pk(),
        _tenant_id(),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        _ts("run_after", nullable=False, now=True),
        _ts("locked_until"),
        sa.Column("locked_by", sa.String(120), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        _ts("created_at", nullable=False, now=True),
        _ts("finished_at"),
        sa.CheckConstraint(TASK_STATUS_CHECK_SQL, name=TASK_STATUS_CHECK),
    )
    op.create_index(
        "ix_task_tenant_id_status_run_after", "task", ["tenant_id", "status", "run_after"]
    )
    op.create_index(
        DEDUPE_INDEX,
        "task",
        ["tenant_id", "kind", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text(DEDUPE_INDEX_WHERE),
    )
    enable_tenant_rls("task")


def downgrade() -> None:
    for table in ("task", "raw_record", "import_batch", "sync_run", "connection"):
        op.drop_table(table)
