"""Tenancy shape (BLUEPRINT §11): firm → tenant → membership(user, tenant, role).

``firm``, ``tenant``, ``user`` and ``session`` are not tenant-scoped (§7; D-10,
D-12). ``membership`` is, and so is the test-only ``_rls_probe`` table. Every
tenant-scoped table: ``tenant_id`` NOT NULL, an index that leads with it, RLS
enabled and forced via ``enable_tenant_rls`` in its migration.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _timestamp_nullable() -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=True)


class Role(enum.StrEnum):
    firm_admin = "firm_admin"
    firm_staff = "firm_staff"
    client_admin = "client_admin"
    client_pm = "client_pm"
    client_viewer = "client_viewer"


FIRM_ROLES: frozenset[Role] = frozenset({Role.firm_admin, Role.firm_staff})


def role_column(*, nullable: bool) -> Mapped[Role]:
    """The ``membership_role`` enum, created once in ``0001_baseline`` and shared."""
    return mapped_column(
        Enum(
            Role,
            name="membership_role",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,
        ),
        nullable=nullable,
    )


class Firm(Base):
    """The CPA practice operating the platform."""

    __tablename__ = "firm"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Tenant(Base):
    """A client company. Its ``id`` is the ``tenant_id`` on every scoped row."""

    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = _uuid_pk()
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("firm.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    """A person. Credentials live here (F02); none of them is ever returned by the
    API or written to a log. The TOTP secret is ciphertext (``app.core.crypto``)."""

    __tablename__ = "user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    # Password (argon2id). NULL until the user sets one through a reset link.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_changed_at: Mapped[datetime | None] = _timestamp_nullable()
    # Throttle: failures inside the window; the lock itself.
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_login_window_start: Mapped[datetime | None] = _timestamp_nullable()
    locked_until: Mapped[datetime | None] = _timestamp_nullable()
    # TOTP. ``totp_enrolled_at`` NULL with a secret present = enrolment pending.
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    totp_key_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    totp_enrolled_at: Mapped[datetime | None] = _timestamp_nullable()
    totp_last_counter: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # SHA-256 of each unused recovery code.
    recovery_code_hashes: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)), nullable=True)
    # One-time password-reset link, SHA-256 of the token.
    password_reset_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_reset_expires_at: Mapped[datetime | None] = _timestamp_nullable()


class Membership(Base):
    """user ↔ tenant ↔ role. Firm staff hold many; client users hold one."""

    __tablename__ = "membership"
    __table_args__ = (
        # Leads with tenant_id, which also satisfies the tenant-index rule.
        UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[Role] = role_column(nullable=False)
    created_at: Mapped[datetime] = _created_at()


class UserSession(Base):
    """Server-side session (D-10). Global: belongs to a user, not a tenant. The
    cookie carries a random 256-bit token; only its SHA-256 is stored here.
    ``active_tenant_id`` is the tenant the session is working in (NULL until
    chosen). It is deliberately not named ``tenant_id``: the table is not
    tenant-scoped and must not be picked up by the RLS enumeration."""

    __tablename__ = "session"
    __table_args__ = (Index("ix_session_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    active_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=True
    )
    totp_verified_at: Mapped[datetime | None] = _timestamp_nullable()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RlsProbe(Base):
    """Throwaway tenant-scoped table used only by the isolation tests."""

    __tablename__ = "_rls_probe"
    __table_args__ = (Index("ix__rls_probe_tenant_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
