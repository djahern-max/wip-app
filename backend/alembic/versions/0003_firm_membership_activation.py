"""F02.1: firm_membership, entry rows (nullable membership.role + CHECK), activation
columns, IP index on firm_audit_log

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17

Decisions: D-15 (firm authority is a row in ``firm_membership``), D-16 (activation
links), D-17 (one firm per deployment).

Data step (owner answer B): reads and writes on ``membership`` set ``app.tenant_id``
per tenant, exactly as the application does; RLS is never disabled or unforced.
For every (firm, user) that held a firm role in any tenant of the firm, one
``firm_membership`` row is created with the highest role (firm_admin over
firm_staff), the way F02 derived it; those membership rows become entry rows
(role NULL). The step aborts, naming the users, if any user holds both a firm role
and a client role inside one firm: no derivation is honest for them.

Reversible: yes. Downgrade restores each entry row's role from ``firm_membership``
(per tenant, with context), then drops the CHECK, the NOT NULL is restored, the
columns renamed back, the index and the table dropped. Entry rows with no matching
``firm_membership`` (orphans, which grant no access) cannot be mapped back and are
deleted with a NOTICE; that is the only lossy part.
"""

from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.models import (
    FIRM_MEMBERSHIP_ROLE_CHECK,
    FIRM_MEMBERSHIP_ROLE_CHECK_SQL,
    MEMBERSHIP_ROLE_CHECK,
    MEMBERSHIP_ROLE_CHECK_SQL,
)

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIRM_ROLES = ("firm_admin", "firm_staff")
RENAMES = (
    ("password_reset_token_hash", "activation_token_hash"),
    ("password_reset_expires_at", "activation_expires_at"),
)


def _tenants(bind) -> list[tuple[UUID, UUID]]:
    return [
        (r.id, r.firm_id) for r in bind.execute(sa.text("SELECT id, firm_id FROM tenant")).all()
    ]


def _set_tenant(bind, tenant_id: UUID) -> None:
    bind.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})


def upgrade() -> None:
    bind = op.get_bind()
    role = postgresql.ENUM(name="membership_role", create_type=False)

    op.create_table(
        "firm_membership",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "firm_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("firm.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", role, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("firm_id", "user_id", name="uq_firm_membership_firm_user"),
        sa.CheckConstraint(FIRM_MEMBERSHIP_ROLE_CHECK_SQL, name=FIRM_MEMBERSHIP_ROLE_CHECK),
    )
    op.create_index("ix_firm_membership_user_id", "firm_membership", ["user_id"])

    for old, new in RENAMES:
        op.alter_column("user", old, new_column_name=new)

    op.create_index("ix_firm_audit_log_ip_occurred_at", "firm_audit_log", ["ip", "occurred_at"])

    # --- data step: per tenant, with tenant context (never unforcing RLS) ---------
    firm_users: dict[tuple[UUID, UUID], set[str]] = defaultdict(set)
    client_users: dict[tuple[UUID, UUID], set[str]] = defaultdict(set)
    tenants = _tenants(bind)
    for tenant_id, firm_id in tenants:
        _set_tenant(bind, tenant_id)
        rows = bind.execute(
            sa.text("SELECT user_id, role::text AS role FROM membership WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).all()
        for r in rows:
            target = firm_users if r.role in FIRM_ROLES else client_users
            target[(firm_id, r.user_id)].add(r.role)
    conflicts = sorted(str(u) for key in firm_users if key in client_users for u in [key[1]])
    if conflicts:
        raise RuntimeError(
            "0003 cannot derive firm_membership: these users hold both a firm role and a "
            f"client role inside one firm: {', '.join(conflicts)}. Resolve by hand first."
        )
    for (firm_id, user_id), roles in firm_users.items():
        bind.execute(
            sa.text(
                "INSERT INTO firm_membership (id, firm_id, user_id, role) "
                "VALUES (:id, :f, :u, CAST(:r AS membership_role))"
            ),
            {
                "id": str(uuid4()),
                "f": str(firm_id),
                "u": str(user_id),
                "r": "firm_admin" if "firm_admin" in roles else "firm_staff",
            },
        )

    op.alter_column("membership", "role", existing_type=role, nullable=True)
    for tenant_id, _firm_id in tenants:
        _set_tenant(bind, tenant_id)
        bind.execute(
            sa.text(
                "UPDATE membership SET role = NULL WHERE tenant_id = :t "
                "AND role IN ('firm_admin', 'firm_staff')"
            ),
            {"t": str(tenant_id)},
        )
    op.create_check_constraint(MEMBERSHIP_ROLE_CHECK, "membership", MEMBERSHIP_ROLE_CHECK_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    role = postgresql.ENUM(name="membership_role", create_type=False)

    op.drop_constraint(MEMBERSHIP_ROLE_CHECK, "membership", type_="check")
    for tenant_id, firm_id in _tenants(bind):
        _set_tenant(bind, tenant_id)
        bind.execute(
            sa.text(
                "UPDATE membership m SET role = fm.role FROM firm_membership fm "
                "WHERE m.tenant_id = :t AND m.role IS NULL "
                "AND fm.user_id = m.user_id AND fm.firm_id = :f"
            ),
            {"t": str(tenant_id), "f": str(firm_id)},
        )
        orphans = bind.execute(
            sa.text("DELETE FROM membership WHERE tenant_id = :t AND role IS NULL"),
            {"t": str(tenant_id)},
        ).rowcount
        if orphans:
            bind.execute(
                sa.text("SELECT pg_catalog.pg_notify('alembic', :m)"),
                {"m": f"0003 downgrade: dropped {orphans} orphan entry row(s), tenant {tenant_id}"},
            )
    op.alter_column("membership", "role", existing_type=role, nullable=False)

    op.drop_index("ix_firm_audit_log_ip_occurred_at", table_name="firm_audit_log")
    for old, new in RENAMES:
        op.alter_column("user", new, new_column_name=old)
    op.drop_index("ix_firm_membership_user_id", table_name="firm_membership")
    op.drop_table("firm_membership")
