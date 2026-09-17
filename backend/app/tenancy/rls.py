"""Alembic helpers for the database-enforced rules (CLAUDE.md; BLUEPRINT §11).

``enable_tenant_rls(table)`` — every migration that creates a tenant-scoped table
must call it. Policy ``tenant_isolation``:
``tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid``.
``current_setting(..., true)`` returns NULL when the setting was never defined and
'' after a ``SET LOCAL`` has expired, so NULLIF makes both cases NULL: reads return
zero rows and writes fail the WITH CHECK, instead of raising a cast error.
FORCE ROW LEVEL SECURITY makes the policy apply to the table owner as well.

``allow_own_membership_read()`` — D-11. One extra policy, on ``membership`` only,
``FOR SELECT``, keyed to ``app.user_id``. ``tests/test_rls.py`` holds an allow-list
of extra policies; any other policy on any tenant table fails the build.

``make_append_only(table)`` — D-13. Row trigger on UPDATE/DELETE and statement
trigger on TRUNCATE that raise. Role-agnostic (applies to the owner too) and only
the owner can drop or disable a trigger, so ``app_rw`` cannot. Tables using it are
listed in ``APPEND_ONLY_TABLES``; ``tests/test_append_only.py`` checks the list
against the catalog.

Migrations never name the application role (D-13 reasoning).
"""

from alembic import op

POLICY_NAME = "tenant_isolation"
TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

OWN_MEMBERSHIP_POLICY = "own_membership_read"
OWN_MEMBERSHIP_PREDICATE = "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"

APPEND_ONLY_FUNCTION = "raise_append_only"
# Register of insert-only tables (D-13). Add a table here in the same migration
# that calls make_append_only() for it.
APPEND_ONLY_TABLES: tuple[str, ...] = ("audit_log", "firm_audit_log")


def _q(table_name: str) -> str:
    return f'"{table_name}"'


def enable_tenant_rls(table_name: str) -> None:
    q = _q(table_name)
    op.execute(f"ALTER TABLE {q} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {q} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {POLICY_NAME} ON {q} "
        f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
    )


def disable_tenant_rls(table_name: str) -> None:
    """Inverse of ``enable_tenant_rls`` for downgrades that keep the table."""
    q = _q(table_name)
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {q}")
    op.execute(f"ALTER TABLE {q} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {q} DISABLE ROW LEVEL SECURITY")


def allow_own_membership_read() -> None:
    """D-11: a user reads their own ``membership`` rows with only ``app.user_id`` set.
    SELECT only; every write still passes only through ``tenant_isolation``."""
    op.execute(
        f"CREATE POLICY {OWN_MEMBERSHIP_POLICY} ON {_q('membership')} "
        f"FOR SELECT USING ({OWN_MEMBERSHIP_PREDICATE})"
    )


def revoke_own_membership_read() -> None:
    op.execute(f"DROP POLICY IF EXISTS {OWN_MEMBERSHIP_POLICY} ON {_q('membership')}")


def append_only_trigger_names(table_name: str) -> tuple[str, str]:
    """(row trigger for UPDATE/DELETE, statement trigger for TRUNCATE)."""
    return f"{table_name}_append_only_row", f"{table_name}_append_only_truncate"


def make_append_only(table_name: str) -> None:
    """D-13: UPDATE, DELETE and TRUNCATE raise for every role, owner included."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {APPEND_ONLY_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    row_trigger, truncate_trigger = append_only_trigger_names(table_name)
    q = _q(table_name)
    op.execute(
        f"CREATE TRIGGER {row_trigger} BEFORE UPDATE OR DELETE ON {q} "
        f"FOR EACH ROW EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}()"
    )
    op.execute(
        f"CREATE TRIGGER {truncate_trigger} BEFORE TRUNCATE ON {q} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}()"
    )


def drop_append_only(table_name: str) -> None:
    """Inverse of ``make_append_only`` for downgrades that keep the table."""
    for trigger in append_only_trigger_names(table_name):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {_q(table_name)}")


def drop_append_only_function() -> None:
    """Call after the last append-only table is dropped in a downgrade."""
    op.execute(f"DROP FUNCTION IF EXISTS {APPEND_ONLY_FUNCTION}()")
