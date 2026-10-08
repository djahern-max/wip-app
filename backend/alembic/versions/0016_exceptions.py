"""F09: exception, exception_event (D-13, D-22, D-46)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-08

The exceptions queue (BLUEPRINT §7 "Work queue", §10; the F09 brief, Plan answer 2, the
owner's yes of 2026-10-08). ``exception``: one row per review sentence with an identity
``(tenant_id, subject_type, subject_id, code, item_key)``: its code, its severity (§10;
refreshed by the run), what it is about (a job, an estimate or a QuickBooks customer row,
by id; no foreign key because the subject is one of three tables and nothing deletes a
subject except ``scripts/delete_tenant.py``, which deletes these rows first), the key
that tells two exceptions of one code on one subject apart, the sentence and the
generator's detail as last raised (the pages compute theirs live, D-22), its status
(``open``, ``resolved``, ``dismissed``), who it is assigned to, when it was first and
last raised, and when and by whom it was resolved or dismissed. Its row changes, so it
is not append-only. ``exception_event``: the history (D-13, append-only): raised, raised
again, resolved and reopened by the run (no actor), assigned, note, dismissed and
reopened by a person; a note or a dismissal carries text, and the database refuses a
blank one as the service does. The CHECK texts are literals, as 0006 explains.

Both tenant-scoped (``tenant_id NOT NULL``, an index leading with it,
``enable_tenant_rls``). ``exception_event`` is registered in ``APPEND_ONLY_TABLES``.

Reversible: yes. The downgrade drops the two tables (the notes and dismissals are
people's records, each with its audit row; the guard refuses ``wip``). The append-only
function stays: other tables use it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.tenancy.rls import enable_tenant_rls, make_append_only

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXC = "exception"
EVENT = "exception_event"
SEVERITY_SQL = "severity IN ('info','warn','block_close')"
SUBJECT_SQL = "subject_type IN ('job','estimate','customer')"
STATUS_SQL = "status IN ('open','resolved','dismissed')"
RESOLVED_SQL = "(status = 'resolved') = (resolved_at IS NOT NULL)"
DISMISSED_SQL = "(status = 'dismissed') = (dismissed_at IS NOT NULL AND dismissed_by IS NOT NULL)"
KIND_SQL = "kind IN ('raised','raised_again','resolved','reopened','assigned','note','dismissed')"
TEXT_SQL = "(kind IN ('note','dismissed')) = (text IS NOT NULL AND btrim(text) <> '')"


def _fk(name: str, target: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete="RESTRICT"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        EXC,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _fk("tenant_id", "tenant.id", nullable=False),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("severity", sa.String(12), nullable=False),
        sa.Column("subject_type", sa.String(10), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_key", sa.String(120), nullable=False, server_default=""),
        sa.Column("message", sa.String(2000), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        _fk("assigned_to", "user.id"),
        sa.Column("first_raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        _fk("dismissed_by", "user.id"),
        sa.UniqueConstraint(
            "tenant_id", "subject_type", "subject_id", "code", "item_key", name=f"uq_{EXC}_identity"
        ),
        sa.CheckConstraint(SEVERITY_SQL, name=f"ck_{EXC}_severity"),
        sa.CheckConstraint(SUBJECT_SQL, name=f"ck_{EXC}_subject"),
        sa.CheckConstraint(STATUS_SQL, name=f"ck_{EXC}_status"),
        sa.CheckConstraint(RESOLVED_SQL, name=f"ck_{EXC}_resolved"),
        sa.CheckConstraint(DISMISSED_SQL, name=f"ck_{EXC}_dismissed"),
    )
    op.create_index(f"ix_{EXC}_tenant_id_status_severity", EXC, ["tenant_id", "status", "severity"])
    op.create_table(
        EVENT,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _fk("tenant_id", "tenant.id", nullable=False),
        _fk("exception_id", f"{EXC}.id", nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _fk("actor_user_id", "user.id"),
        _fk("assigned_to", "user.id"),
        sa.Column("text", sa.String(2000), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(KIND_SQL, name=f"ck_{EVENT}_kind"),
        sa.CheckConstraint(TEXT_SQL, name=f"ck_{EVENT}_text"),
    )
    op.create_index(
        f"ix_{EVENT}_tenant_id_exception", EVENT, ["tenant_id", "exception_id", "occurred_at"]
    )
    enable_tenant_rls(EXC)
    enable_tenant_rls(EVENT)
    make_append_only(EVENT)


def downgrade() -> None:
    op.drop_table(EVENT)
    op.drop_table(EXC)
