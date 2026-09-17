"""Tenancy shape (BLUEPRINT §11; D-15): firm → tenant → membership(user, tenant, role),
plus firm_membership(firm, user, role) for the practice's own people.

``firm``, ``tenant``, ``user``, ``session`` and ``firm_membership`` are not
tenant-scoped (§7; D-10, D-12, D-15, D-17). ``membership`` is, and so is the
test-only ``_rls_probe`` table. Every tenant-scoped table: ``tenant_id`` NOT NULL,
an index that leads with it, RLS enabled and forced via ``enable_tenant_rls`` in
its migration.

Roles (D-15): a firm role is held only in ``firm_membership``. A ``membership``
row carries a client role, or NULL for an *entry row* that lets a firm user open
that tenant with the authority of their ``firm_membership``. Nothing outside
``app.core.auth.effective_role`` reads ``Membership.role``.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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


def _timestamp_nullable(**kw) -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=True, **kw)


class Role(enum.StrEnum):
    firm_admin = "firm_admin"
    firm_staff = "firm_staff"
    client_admin = "client_admin"
    client_pm = "client_pm"
    client_viewer = "client_viewer"


FIRM_ROLES: frozenset[Role] = frozenset({Role.firm_admin, Role.firm_staff})
CLIENT_ROLES: frozenset[Role] = frozenset({Role.client_admin, Role.client_pm, Role.client_viewer})

# CHECK constraints (D-15): the database itself refuses a firm role in ``membership``
# and a client role in ``firm_membership``. Names are shared with migration 0003.
MEMBERSHIP_ROLE_CHECK = "ck_membership_role_client"
MEMBERSHIP_ROLE_CHECK_SQL = (
    "role IS NULL OR role IN ('client_admin'::membership_role, "
    "'client_pm'::membership_role, 'client_viewer'::membership_role)"
)
FIRM_MEMBERSHIP_ROLE_CHECK = "ck_firm_membership_role_firm"
FIRM_MEMBERSHIP_ROLE_CHECK_SQL = (
    "role IN ('firm_admin'::membership_role, 'firm_staff'::membership_role)"
)

# Columns of ``user`` that hold credential state. Deferred in the mapping so they
# load only where the auth service asks for them (``undefer_group``); the response
# scan in the test suite checks that none of these names or values ever leaves.
CREDENTIAL_GROUP = "credentials"
CREDENTIAL_COLUMNS: tuple[str, ...] = (
    "password_hash",
    "failed_login_count",
    "failed_login_window_start",
    "locked_until",
    "totp_secret_enc",
    "totp_key_id",
    "totp_last_counter",
    "recovery_code_hashes",
    "activation_token_hash",
    "activation_expires_at",
)


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
    """The CPA practice operating the platform (one per deployment, D-17)."""

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
    """A person. Credentials live here (F02; deferred group, F02.1); none of them is
    ever returned by the API or written to a log. The TOTP secret is ciphertext
    (``app.core.crypto``). An account starts with no password and is activated
    through an admin-issued one-time link (D-16)."""

    __tablename__ = "user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    password_changed_at: Mapped[datetime | None] = _timestamp_nullable()
    # NULL with a secret present = enrolment pending. Not a credential: it is the
    # "TOTP enrolled" flag shown to admins.
    totp_enrolled_at: Mapped[datetime | None] = _timestamp_nullable()

    # --- credential group: deferred ---------------------------------------------
    # Password (argon2id). NULL until the user redeems an activation link.
    password_hash: Mapped[str | None] = mapped_column(
        String(255), nullable=True, deferred=True, deferred_group=CREDENTIAL_GROUP
    )
    # Account lockout: failures inside the window; the lock itself.
    failed_login_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        deferred=True,
        deferred_group=CREDENTIAL_GROUP,
    )
    failed_login_window_start: Mapped[datetime | None] = _timestamp_nullable(
        deferred=True, deferred_group=CREDENTIAL_GROUP
    )
    locked_until: Mapped[datetime | None] = _timestamp_nullable(
        deferred=True, deferred_group=CREDENTIAL_GROUP
    )
    totp_secret_enc: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, deferred=True, deferred_group=CREDENTIAL_GROUP
    )
    totp_key_id: Mapped[str | None] = mapped_column(
        String(40), nullable=True, deferred=True, deferred_group=CREDENTIAL_GROUP
    )
    totp_last_counter: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, deferred=True, deferred_group=CREDENTIAL_GROUP
    )
    # SHA-256 of each unused recovery code.
    recovery_code_hashes: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(64)), nullable=True, deferred=True, deferred_group=CREDENTIAL_GROUP
    )
    # One-time activation link (D-16), SHA-256 of the token. Issuing a new link
    # overwrites both, which invalidates the earlier link.
    activation_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, deferred=True, deferred_group=CREDENTIAL_GROUP
    )
    activation_expires_at: Mapped[datetime | None] = _timestamp_nullable(
        deferred=True, deferred_group=CREDENTIAL_GROUP
    )


class FirmMembership(Base):
    """user ↔ firm ↔ firm role (D-15). Global table (approved without ``tenant_id``).
    The only source of firm authority."""

    __tablename__ = "firm_membership"
    __table_args__ = (
        UniqueConstraint("firm_id", "user_id", name="uq_firm_membership_firm_user"),
        Index("ix_firm_membership_user_id", "user_id"),
        CheckConstraint(FIRM_MEMBERSHIP_ROLE_CHECK_SQL, name=FIRM_MEMBERSHIP_ROLE_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("firm.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[Role] = role_column(nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Membership(Base):
    """user ↔ tenant ↔ client role, or an entry row (``role`` NULL) for a firm user.
    Read the role only through ``app.core.auth.effective_role``."""

    __tablename__ = "membership"
    __table_args__ = (
        # Leads with tenant_id, which also satisfies the tenant-index rule.
        UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
        CheckConstraint(MEMBERSHIP_ROLE_CHECK_SQL, name=MEMBERSHIP_ROLE_CHECK),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[Role | None] = role_column(nullable=True)
    created_at: Mapped[datetime] = _created_at()


class UserSession(Base):
    """Server-side session (D-10). Global: belongs to a user, not a tenant. The
    cookie carries a random 256-bit token; only its SHA-256 is stored here.
    ``active_tenant_id`` is the tenant the session is working in (NULL until
    chosen). It is deliberately not named ``tenant_id``: the table is not
    tenant-scoped and must not be picked up by the RLS enumeration.
    An enrolment-only session (D-16) is an ordinary row whose ``expires_at`` is
    ``ENROL_SESSION_TTL_MINUTES`` after creation."""

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
