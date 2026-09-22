"""Catalog queries behind the tenancy rules (CLAUDE.md "Tenancy"; D-11, D-13).

One implementation, read by ``tests/test_rls.py``, ``tests/test_append_only.py`` and
``scripts/prod_check.py`` (F05.0), so the production database is checked with the
same queries the test suite enforces. Pure functions of a connection: no app import
beyond ``app.tenancy.rls``, no settings, no I/O other than the catalog reads.

Every function returns a list of failure strings naming a table, policy or trigger
and nothing else (no row, no URL), empty when the rule holds.
"""

from sqlalchemy import Connection, text

from app.tenancy.rls import (
    APPEND_ONLY_FUNCTION,
    APPEND_ONLY_TABLES,
    OWN_MEMBERSHIP_POLICY,
    POLICY_NAME,
    append_only_trigger_names,
)

# D-11 allow-list: (table, policy, command, predicate exactly as Postgres deparses it).
# Any policy on any tenant table that is neither tenant_isolation nor listed here is a
# failure. Adding an entry needs a decision in docs/DECISIONS.md; tests/test_rls.py
# asserts the list has exactly one entry.
EXTRA_POLICIES: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        (
            "membership",
            OWN_MEMBERSHIP_POLICY,
            "SELECT",
            "(user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)",
        ),
    }
)

TENANT_TABLES_SQL = text(
    """
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
    WHERE c.table_schema = 'public'
      AND c.column_name = 'tenant_id'
      AND t.table_type = 'BASE TABLE'
    ORDER BY 1
    """
)


def tenant_tables(conn: Connection) -> list[str]:
    """Every base table in ``public`` with a ``tenant_id`` column (the enumeration
    CLAUDE.md relies on: a table without tenant scope may never have that column)."""
    return [r[0] for r in conn.execute(TENANT_TABLES_SQL)]


def rls_failures(conn: Connection) -> list[str]:
    """Each tenant table must have RLS enabled, forced, and the ``tenant_isolation``
    policy."""
    failures: list[str] = []
    for table in tenant_tables(conn):
        enabled, forced = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = to_regclass(:t)"
            ),
            {"t": f'public."{table}"'},
        ).one()
        policies = (
            conn.execute(
                text(
                    "SELECT policyname FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = :t"
                ),
                {"t": table},
            )
            .scalars()
            .all()
        )
        if not enabled:
            failures.append(f"{table}: RLS not enabled")
        if not forced:
            failures.append(f"{table}: RLS not forced")
        if POLICY_NAME not in policies:
            failures.append(f"{table}: policy {POLICY_NAME} missing")
    return failures


def policy_failures(conn: Connection) -> list[str]:
    """No policy beyond ``tenant_isolation`` unless allow-listed (D-11); an allow-listed
    policy must exist, exactly as listed, and must not ``WITH CHECK``."""
    found: set[tuple[str, str, str, str]] = set()
    failures: list[str] = []
    for table in tenant_tables(conn):
        rows = conn.execute(
            text(
                "SELECT policyname, cmd, qual, with_check FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = :t"
            ),
            {"t": table},
        ).all()
        for name, cmd, qual, with_check in rows:
            if name == POLICY_NAME:
                continue
            key = (table, name, cmd, qual)
            if key not in EXTRA_POLICIES:
                failures.append(f"unlisted policy on {table}: {name} FOR {cmd} USING {qual}")
            elif with_check is not None:
                failures.append(f"{table}.{name}: extra policies must not WITH CHECK")
            else:
                found.add(key)
    for table, name, cmd, _qual in sorted(EXTRA_POLICIES - found):
        failures.append(f"allow-listed policy missing: {table}.{name} FOR {cmd}")
    return failures


def append_only_failures(
    conn: Connection, tables: tuple[str, ...] = APPEND_ONLY_TABLES
) -> list[str]:
    """Both append-only triggers present and enabled on each table (D-13)."""
    failures: list[str] = []
    for table in tables:
        for trigger in append_only_trigger_names(table):
            enabled = conn.execute(
                text(
                    "SELECT tgenabled FROM pg_trigger "
                    "WHERE tgrelid = to_regclass(:t) AND tgname = :n AND NOT tgisinternal"
                ),
                {"t": f'public."{table}"', "n": trigger},
            ).scalar_one_or_none()
            if enabled is None:
                failures.append(f"{table}: trigger {trigger} missing")
            elif enabled == "D":
                failures.append(f"{table}: trigger {trigger} disabled")
    return failures


def append_only_register_mismatch(conn: Connection) -> list[str]:
    """Every table wired to the trigger function is in ``APPEND_ONLY_TABLES`` and
    vice versa."""
    wired = set(
        conn.execute(
            text(
                "SELECT DISTINCT tgrelid::regclass::text FROM pg_trigger "
                "WHERE tgfoid = to_regproc(:f) AND NOT tgisinternal"
            ),
            {"f": f"public.{APPEND_ONLY_FUNCTION}"},
        ).scalars()
    )
    registered = set(APPEND_ONLY_TABLES)
    failures = [
        f"{t}: append-only triggers present but not registered" for t in sorted(wired - registered)
    ]
    failures += [f"{t}: registered but no append-only trigger" for t in sorted(registered - wired)]
    return failures
