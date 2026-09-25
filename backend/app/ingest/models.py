"""Integration tables (BLUEPRINT §6.1, §7; F03). All tenant-scoped: ``tenant_id``
NOT NULL, an index leading with it, RLS enabled and forced in migration 0004.

- ``connection``: one row per tenant per external system. Tokens are ciphertext
  bound by associated data to ``tenant_id | connection_id | field`` (D-21 crypto
  note in the F03 brief), so a blob copied to another row fails to decrypt.
  ``last_error`` is text and never a token or a payload.
- ``sync_run``: one row per API pull (F05 fills it). ``cursor`` is JSONB (plan
  call 3): backfill position, CDC watermark or webhook ids, as F05 needs.
- ``import_batch``: one row per distinct uploaded file per tenant and source kind
  (unique on the SHA-256). The original filename is data, never part of a key.
- ``raw_record`` (D-20): insert-only, versioned per ``(source, entity_type,
  external_id)``; exactly one of ``import_batch_id`` / ``sync_run_id`` (CHECK).
- ``webhook_event`` (F05.1, D-29): **the one table here without ``tenant_id``**. A
  webhook delivery is stored before anyone knows which tenant holds the realm, so
  the row cannot carry a tenant; it is insert-only (``make_append_only``) and holds
  the delivery as received. It is read without tenant context only to store a
  delivery and to count deliveries for a realm the requesting tenant holds.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.tenancy.models import Base

IMPORT_STATUSES: tuple[str, ...] = (
    "received",
    "processing",
    "loaded",
    "loaded_with_issues",
    "nothing_loaded",
    "failed",
)
# The one status → label mapping (D-22): what a person sees for each status.
IMPORT_STATUS_LABELS: dict[str, str] = {
    "received": "Received",
    "processing": "Processing",
    "loaded": "Loaded",
    "loaded_with_issues": "Loaded with issues",
    "nothing_loaded": "Nothing loaded",
    "failed": "Failed",
}
IMPORT_STATUS_CHECK = "ck_import_batch_status"
IMPORT_STATUS_CHECK_SQL = (
    "status IN ('received','processing','loaded','loaded_with_issues','nothing_loaded','failed')"
)
# What the batch's follow-on task did with it (0006): applied, or superseded because a
# newer file of the same kind had already been uploaded. NULL = no follow-on, or not run.
# F06 (0010) adds ``unchanged``: the follow-on found every record of the file already
# applied (a re-export of a file that was loaded before).
FOLLOWUP_OUTCOMES: tuple[str, ...] = ("applied", "superseded", "unchanged")
FOLLOWUP_OUTCOME_CHECK = "ck_import_batch_followup_outcome"
FOLLOWUP_OUTCOME_CHECK_SQL = (
    "followup_outcome IS NULL OR followup_outcome IN ('applied','superseded','unchanged')"
)

RAW_ORIGIN_CHECK = "ck_raw_record_one_origin"
RAW_ORIGIN_CHECK_SQL = "(import_batch_id IS NULL) <> (sync_run_id IS NULL)"

CONNECTION_STATUSES: tuple[str, ...] = ("disconnected", "connected", "needs_reconnect", "error")
# The one status → label mapping for connections (D-22).
CONNECTION_STATUS_LABELS: dict[str, str] = {
    "disconnected": "Not connected",
    "connected": "Connected",
    "needs_reconnect": "Needs reconnect",
    "error": "Error",
}
# One external company belongs to one tenant (F05). Unique indexes are enforced
# whatever RLS shows the session, so a second tenant's claim fails at the database.
CONNECTION_REALM_INDEX = "uq_connection_system_realm_id"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _tenant_id() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Connection(Base):
    __tablename__ = "connection"
    __table_args__ = (
        UniqueConstraint("tenant_id", "system", name="uq_connection_tenant_system"),
        Index(
            CONNECTION_REALM_INDEX,
            "system",
            "realm_id",
            unique=True,
            postgresql_where=text("realm_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    # Validated against the source registry in code, not a Postgres enum.
    system: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="disconnected")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_key_id: Mapped[str | None] = mapped_column(String(40))
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_key_id: Mapped[str | None] = mapped_column(String(40))
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # F05 (0007). The external company, which API it lives on, and its name as the
    # source gives it (data: never logged). Expiries come from each token response.
    realm_id: Mapped[str | None] = mapped_column(String(40))
    environment: Mapped[str | None] = mapped_column(String(20))
    company_name: Mapped[str | None] = mapped_column(String(200))
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The pending OAuth ``state``: SHA-256 only, who started it, until when. Cleared
    # by the first callback that presents it (single use).
    oauth_state_sha256: Mapped[str | None] = mapped_column(String(64))
    oauth_state_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT")
    )
    oauth_state_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # F05.1 (0009): when a webhook delivery last named this company's realm. Written by
    # the dispatcher, never by the webhook request itself.
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SyncRun(Base):
    __tablename__ = "sync_run"
    __table_args__ = (Index("ix_sync_run_tenant_id_started_at", "tenant_id", "started_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connection.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # backfill, cdc, webhook…
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(20))  # succeeded, failed, drift
    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(500))
    # F05 (0007): entity names, counts and amounts only; never a payload.
    detail: Mapped[dict | None] = mapped_column(JSONB)


class ImportBatch(Base):
    __tablename__ = "import_batch"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_kind", "sha256", name="uq_import_batch_tenant_source_sha256"
        ),
        Index("ix_import_batch_tenant_id_uploaded_at", "tenant_id", "uploaded_at"),
        CheckConstraint(IMPORT_STATUS_CHECK_SQL, name=IMPORT_STATUS_CHECK),
        CheckConstraint(FOLLOWUP_OUTCOME_CHECK_SQL, name=FOLLOWUP_OUTCOME_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    object_key: Mapped[str] = mapped_column(String(300), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT")
    )
    uploaded_at: Mapped[datetime] = _created_at()
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    rows_loaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(500))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # F04 (owner amendment C): the after_load task enqueued when this batch loaded.
    followup_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task.id", ondelete="RESTRICT")
    )
    # What that task did with the batch (FOLLOWUP_OUTCOMES); written by the task.
    followup_outcome: Mapped[str | None] = mapped_column(String(20))
    # F06 (0010): rows the file could not load and file-level facts, each
    # ``{code, message, row_number, detail}`` with a sentence for the person (D-22);
    # written by the pipeline, appended to by the follow-on task. Small values only
    # (ids, order numbers, amounts as strings, the cells of a row with no id).
    issues: Mapped[list | None] = mapped_column(JSONB)


class RawRecord(Base):
    __tablename__ = "raw_record"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source",
            "entity_type",
            "external_id",
            "version",
            name="uq_raw_record_key_version",
        ),
        Index("ix_raw_record_tenant_id_import_batch_id", "tenant_id", "import_batch_id"),
        CheckConstraint(RAW_ORIGIN_CHECK_SQL, name=RAW_ORIGIN_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict | list | None] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetched_at: Mapped[datetime] = _created_at()
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batch.id", ondelete="RESTRICT")
    )
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sync_run.id", ondelete="RESTRICT")
    )


WEBHOOK_EVENT_ID_INDEX = "uq_webhook_event_event_id"


class WebhookEvent(Base):
    """One row per event of a webhook delivery (F05.1, D-29). Tenant-less and
    insert-only; see the module docstring. ``event_id`` is the CloudEvents ``id``
    (NULL for the legacy shape and for a body that matches neither), unique where
    present so a retried delivery stores once. ``payload`` is the event object as
    received (or the legacy notification, or the whole unknown body). The two
    ``intuit-*`` headers are kept for a support case; neither is a secret."""

    __tablename__ = "webhook_event"
    __table_args__ = (
        Index("ix_webhook_event_realm_id_received_at", "realm_id", "received_at"),
        Index(
            WEBHOOK_EVENT_ID_INDEX,
            "event_id",
            unique=True,
            postgresql_where=text("event_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    received_at: Mapped[datetime] = _created_at()
    event_id: Mapped[str | None] = mapped_column(String(120))
    realm_id: Mapped[str | None] = mapped_column(String(40))
    event_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80))
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict | list | None] = mapped_column(JSONB, nullable=False)
    intuit_tid: Mapped[str | None] = mapped_column(String(80))
    schema_version: Mapped[str | None] = mapped_column(String(40))
