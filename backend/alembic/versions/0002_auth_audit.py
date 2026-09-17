"""F02: credentials on user, session, audit_log, firm_audit_log, own-membership policy,
append-only triggers

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17

Reversible: yes. Downgrade drops the extra policy, both audit tables (their triggers
go with them), the trigger function, the session table, and the credential columns
on user. Decisions: D-10, D-11, D-12, D-13.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import (
    allow_own_membership_read,
    drop_append_only_function,
    enable_tenant_rls,
    make_append_only,
    revoke_own_membership_read,
)

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def user_columns() -> list[sa.Column]:
    """Fresh Column objects each call (a Column may be bound to one table only)."""
    return [
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_login_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("totp_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("totp_key_id", sa.String(40), nullable=True),
        sa.Column("totp_enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("totp_last_counter", sa.BigInteger(), nullable=True),
        sa.Column("recovery_code_hashes", postgresql.ARRAY(sa.String(64)), nullable=True),
        sa.Column("password_reset_token_hash", sa.String(64), nullable=True),
        sa.Column("password_reset_expires_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _audit_columns() -> list[sa.Column]:
    """Columns shared by both audit tables (everything but the scope key)."""
    role = postgresql.ENUM(name="membership_role", create_type=False)
    return [
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("actor_role", role, nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=False),
        sa.Column("entity_id", sa.String(80), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
    ]


def upgrade() -> None:
    for column in user_columns():
        op.add_column("user", column)

    op.create_table(
        "session",
        _uuid_pk(),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "active_tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("totp_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_session_user_id", "session", ["user_id"])

    op.create_table(
        "audit_log",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        *_audit_columns(),
    )
    op.create_index("ix_audit_log_tenant_id_occurred_at", "audit_log", ["tenant_id", "occurred_at"])
    enable_tenant_rls("audit_log")
    make_append_only("audit_log")

    op.create_table(
        "firm_audit_log",
        _uuid_pk(),
        sa.Column(
            "firm_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("firm.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        *_audit_columns(),
    )
    op.create_index(
        "ix_firm_audit_log_firm_id_occurred_at", "firm_audit_log", ["firm_id", "occurred_at"]
    )
    make_append_only("firm_audit_log")

    allow_own_membership_read()


def downgrade() -> None:
    revoke_own_membership_read()
    op.drop_table("firm_audit_log")
    op.drop_table("audit_log")
    drop_append_only_function()
    op.drop_table("session")
    for column in reversed(user_columns()):
        op.drop_column("user", column.name)
