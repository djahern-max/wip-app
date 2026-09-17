"""Audit tables (D-12, D-13).

``audit_log`` is tenant-scoped (RLS enabled and forced, leading ``tenant_id``
index) and holds events that belong to one tenant. ``firm_audit_log`` is global,
keyed by ``firm_id``, readable only through a firm-role route, and holds events
with no tenant: logins, lockouts, TOTP and password events, user creation.
Both are insert-only by trigger (``make_append_only``; ``APPEND_ONLY_TABLES``).

``detail`` never contains a password, TOTP secret or code, recovery code, or
session id (``app.core.audit`` refuses those keys).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.tenancy.models import Base, Role, role_column


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _occurred_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant_id_occurred_at", "tenant_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = _occurred_at()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT"), nullable=True
    )
    actor_role: Mapped[Role | None] = role_column(nullable=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class FirmAuditLog(Base):
    """``firm_id`` is NULL only when no firm can be derived: a failed login for an
    unknown e-mail, or a user who holds no membership yet (see the F02 plan)."""

    __tablename__ = "firm_audit_log"
    __table_args__ = (Index("ix_firm_audit_log_firm_id_occurred_at", "firm_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    firm_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("firm.id", ondelete="RESTRICT"), nullable=True
    )
    occurred_at: Mapped[datetime] = _occurred_at()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT"), nullable=True
    )
    actor_role: Mapped[Role | None] = role_column(nullable=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
