"""The ``task`` table (D-19). Tenant-scoped like any other; RLS enabled and forced
in migration 0004. ``payload`` holds ids only, never source data. A partial unique
index on ``(tenant_id, kind, dedupe_key)`` for non-terminal rows makes a second
enqueue with the same key a no-op while the first is queued or running."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.tenancy.models import Base

TASK_STATUSES: tuple[str, ...] = ("queued", "running", "succeeded", "failed")
TASK_STATUS_CHECK = "ck_task_status"
TASK_STATUS_CHECK_SQL = "status IN ('queued','running','succeeded','failed')"
DEDUPE_INDEX = "uq_task_tenant_kind_dedupe_open"
DEDUPE_INDEX_WHERE = "status IN ('queued','running') AND dedupe_key IS NOT NULL"


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (
        Index("ix_task_tenant_id_status_run_after", "tenant_id", "status", "run_after"),
        Index(
            DEDUPE_INDEX,
            "tenant_id",
            "kind",
            "dedupe_key",
            unique=True,
            postgresql_where=text(DEDUPE_INDEX_WHERE),
        ),
        CheckConstraint(TASK_STATUS_CHECK_SQL, name=TASK_STATUS_CHECK),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(120))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
