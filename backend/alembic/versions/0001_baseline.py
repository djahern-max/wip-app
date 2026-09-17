"""F01 baseline: firm, tenant, user, membership, _rls_probe

Revision ID: 0001
Revises:
Create Date: 2026-09-17

Reversible: yes (drops the tables and the membership_role enum).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("firm_admin", "firm_staff", "client_admin", "client_pm", "client_viewer")


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        "firm",
        _uuid_pk(),
        sa.Column("name", sa.String(200), nullable=False),
        _created_at(),
    )
    op.create_table(
        "tenant",
        _uuid_pk(),
        sa.Column(
            "firm_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("firm.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        _created_at(),
    )
    op.create_table(
        "user",
        _uuid_pk(),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        _created_at(),
    )

    membership_role = postgresql.ENUM(*ROLES, name="membership_role", create_type=False)
    membership_role.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "membership",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", membership_role, nullable=False),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
    )
    enable_tenant_rls("membership")

    # Test-only tenant-scoped table (F01 brief). Exercised by tests/test_rls.py.
    op.create_table(
        "_rls_probe",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("label", sa.String(100), nullable=False),
    )
    op.create_index("ix__rls_probe_tenant_id", "_rls_probe", ["tenant_id"])
    enable_tenant_rls("_rls_probe")


def downgrade() -> None:
    op.drop_table("_rls_probe")
    op.drop_table("membership")
    postgresql.ENUM(name="membership_role").drop(op.get_bind(), checkfirst=True)
    op.drop_table("user")
    op.drop_table("tenant")
    op.drop_table("firm")
