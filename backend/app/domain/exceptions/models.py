"""The two tables of migration 0016 (F09; D-13, D-46). ``exception`` is the current
state of one exception, keyed by its identity; ``exception_event`` is its history and
is append-only. The ORM class for the table ``exception`` is ``ReviewException``
because ``Exception`` is Python's builtin; the API and the screens say "exception"."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.tenancy.models import Base

SEVERITIES: tuple[str, ...] = ("info", "warn", "block_close")
SEVERITY_LABELS: dict[str, str] = {
    "info": "For information",
    "warn": "Needs attention",
    "block_close": "Blocks period close",
}
SUBJECT_TYPES: tuple[str, ...] = ("job", "estimate", "customer")
STATUSES: tuple[str, ...] = ("open", "resolved", "dismissed")
STATUS_LABELS: dict[str, str] = {"open": "Open", "resolved": "Resolved", "dismissed": "Dismissed"}
EVENT_KINDS: tuple[str, ...] = (
    "raised",
    "raised_again",
    "resolved",
    "reopened",
    "assigned",
    "note",
    "dismissed",
)
EVENT_LABELS: dict[str, str] = {
    "raised": "Raised",
    "raised_again": "Raised again",
    "resolved": "Resolved",
    "reopened": "Reopened",
    "assigned": "Assigned",
    "note": "Note",
    "dismissed": "Dismissed",
}

SEVERITY_SQL = "severity IN ('info','warn','block_close')"
SUBJECT_SQL = "subject_type IN ('job','estimate','customer')"
STATUS_SQL = "status IN ('open','resolved','dismissed')"
RESOLVED_SQL = "(status = 'resolved') = (resolved_at IS NOT NULL)"
DISMISSED_SQL = "(status = 'dismissed') = (dismissed_at IS NOT NULL AND dismissed_by IS NOT NULL)"
KIND_SQL = "kind IN ('raised','raised_again','resolved','reopened','assigned','note','dismissed')"
TEXT_SQL = "(kind IN ('note','dismissed')) = (text IS NOT NULL AND btrim(text) <> '')"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _fk(target: str, *, nullable: bool = True) -> Mapped:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(target, ondelete="RESTRICT"), nullable=nullable
    )


class ReviewException(Base):
    __tablename__ = "exception"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subject_type",
            "subject_id",
            "code",
            "item_key",
            name="uq_exception_identity",
        ),
        Index("ix_exception_tenant_id_status_severity", "tenant_id", "status", "severity"),
        CheckConstraint(SEVERITY_SQL, name="ck_exception_severity"),
        CheckConstraint(SUBJECT_SQL, name="ck_exception_subject"),
        CheckConstraint(STATUS_SQL, name="ck_exception_status"),
        CheckConstraint(RESOLVED_SQL, name="ck_exception_resolved"),
        CheckConstraint(DISMISSED_SQL, name="ck_exception_dismissed"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _fk("tenant.id", nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(12), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    item_key: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    message: Mapped[str] = mapped_column(String(2000), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    assigned_to: Mapped[uuid.UUID | None] = _fk("user.id")
    first_raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_by: Mapped[uuid.UUID | None] = _fk("user.id")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.subject_type, str(self.subject_id), self.code, self.item_key)


class ExceptionEvent(Base):
    __tablename__ = "exception_event"
    __table_args__ = (
        Index("ix_exception_event_tenant_id_exception", "tenant_id", "exception_id", "occurred_at"),
        CheckConstraint(KIND_SQL, name="ck_exception_event_kind"),
        CheckConstraint(TEXT_SQL, name="ck_exception_event_text"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _fk("tenant.id", nullable=False)
    exception_id: Mapped[uuid.UUID] = _fk("exception.id", nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    actor_user_id: Mapped[uuid.UUID | None] = _fk("user.id")
    assigned_to: Mapped[uuid.UUID | None] = _fk("user.id")
    text: Mapped[str | None] = mapped_column(String(2000))
    detail: Mapped[dict | None] = mapped_column(JSONB)
