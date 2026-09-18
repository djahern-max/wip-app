"""Tenant isolation is enforced by the database (BLUEPRINT §3.8, §11; F01 acceptance)."""

import uuid

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ProgrammingError

from app.core.db import set_user_context, tenant_session, untenanted_session
from app.ingest.models import Connection, ImportBatch, RawRecord, SyncRun
from app.tenancy.models import Membership, RlsProbe, Role
from app.tenancy.rls import OWN_MEMBERSHIP_POLICY, POLICY_NAME
from app.worker.models import Task
from tests.conftest import Seed

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


def _tenant_tables(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(TENANT_TABLES_SQL)]


# --- (a) every tenant table has RLS enabled AND forced, a policy, and a leading index


F03_TABLES = ("connection", "sync_run", "import_batch", "raw_record", "task")


def test_every_tenant_table_is_enumerated(migrated_db: None, owner_engine: Engine) -> None:
    # Guard against the enumeration itself silently returning nothing.
    assert set(_tenant_tables(owner_engine)) >= {
        "membership",
        "_rls_probe",
        "audit_log",
        *F03_TABLES,
    }


def test_every_tenant_table_has_rls_enabled_and_forced(
    migrated_db: None, owner_engine: Engine
) -> None:
    failures: list[str] = []
    with owner_engine.connect() as conn:
        for table in _tenant_tables(owner_engine):
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
    assert not failures, "\n".join(failures)


def test_every_tenant_table_has_index_leading_with_tenant_id(
    migrated_db: None, owner_engine: Engine
) -> None:
    failures: list[str] = []
    with owner_engine.connect() as conn:
        for table in _tenant_tables(owner_engine):
            count = conn.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_index i
                    JOIN pg_attribute a
                      ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                    WHERE i.indrelid = to_regclass(:t) AND a.attname = 'tenant_id'
                    """
                ),
                {"t": f'public."{table}"'},
            ).scalar_one()
            if count == 0:
                failures.append(f"{table}: no index leads with tenant_id")
    assert not failures, "\n".join(failures)


def test_tenant_id_columns_are_not_null(migrated_db: None, owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        nullable = (
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'tenant_id' "
                    "AND is_nullable = 'YES'"
                )
            )
            .scalars()
            .all()
        )
    assert nullable == []


# --- (b) with tenant A context, tenant B rows are invisible (ORM and raw SQL, app_rw)


def test_orm_reads_only_own_tenant(seed: Seed, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, seed.tenant_a) as s:
        labels = [p.label for p in s.execute(select(RlsProbe)).scalars()]
        member_tenants = {m.tenant_id for m in s.execute(select(Membership)).scalars()}
    assert labels == ["a-row"]
    assert member_tenants == {seed.tenant_a}


def test_raw_sql_reads_only_own_tenant(seed: Seed, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, seed.tenant_a) as s:
        rows = s.execute(text('SELECT tenant_id, label FROM "_rls_probe"')).all()
        # Even an explicit cross-tenant predicate returns nothing.
        other = s.execute(
            text('SELECT count(*) FROM "_rls_probe" WHERE tenant_id = :b'),
            {"b": seed.tenant_b},
        ).scalar_one()
    assert [(r.tenant_id, r.label) for r in rows] == [(seed.tenant_a, "a-row")]
    assert other == 0


def test_cannot_write_rows_for_another_tenant(seed: Seed, rw_engine: Engine) -> None:
    with pytest.raises(ProgrammingError, match="row-level security"):
        with tenant_session(rw_engine, seed.tenant_a) as s:
            s.add(RlsProbe(tenant_id=seed.tenant_b, label="smuggled"))
            s.flush()
    with pytest.raises(ProgrammingError, match="row-level security"):
        with tenant_session(rw_engine, seed.tenant_a) as s:
            s.execute(
                text('UPDATE "_rls_probe" SET tenant_id = :b WHERE tenant_id = :a'),
                {"a": seed.tenant_a, "b": seed.tenant_b},
            )
    with tenant_session(rw_engine, seed.tenant_b) as s:
        labels = s.execute(select(RlsProbe.label)).scalars().all()
    assert labels == ["b-row"]


def _seed_f03_rows(owner_engine: Engine, tenant_id: uuid.UUID, marker: str) -> None:
    """One row per F03 table in ``tenant_id`` (as the owner, with context)."""
    with tenant_session(owner_engine, tenant_id) as s:
        conn = Connection(tenant_id=tenant_id, system=f"sys-{marker}"[:40], status="disconnected")
        s.add(conn)
        s.flush()
        s.add(SyncRun(tenant_id=tenant_id, connection_id=conn.id, kind="backfill"))
        batch = ImportBatch(
            tenant_id=tenant_id,
            source_kind="unparsed_file",
            sha256=marker.ljust(64, "0"),
            byte_size=1,
            original_filename="x.bin",
            object_key=f"tenant/{tenant_id}/imports/{marker}.bin",
        )
        s.add(batch)
        s.flush()
        s.add(
            RawRecord(
                tenant_id=tenant_id,
                source="test",
                entity_type="row",
                external_id=marker,
                version=1,
                payload={"m": marker},
                payload_sha256="0" * 64,
                import_batch_id=batch.id,
            )
        )
        s.add(Task(tenant_id=tenant_id, kind="probe", payload={}, max_attempts=1))


@pytest.mark.parametrize("table", F03_TABLES)
def test_f03_tables_read_zero_rows_of_another_tenant(
    seed: Seed, owner_engine: Engine, rw_engine: Engine, table: str
) -> None:
    """Tenant A sees none of tenant B's rows in each F03 table, via ORM and raw SQL
    as app_rw; B sees its own."""
    marker = uuid.uuid4().hex[:12]
    _seed_f03_rows(owner_engine, seed.tenant_b, marker)
    model = {
        "connection": Connection,
        "sync_run": SyncRun,
        "import_batch": ImportBatch,
        "raw_record": RawRecord,
        "task": Task,
    }[table]
    with tenant_session(rw_engine, seed.tenant_a) as s:
        orm_tenants = {r.tenant_id for r in s.execute(select(model)).scalars()}
        raw = s.execute(
            text(f'SELECT count(*) FROM "{table}" WHERE tenant_id = :b'), {"b": seed.tenant_b}
        ).scalar_one()
    assert seed.tenant_b not in orm_tenants and raw == 0
    with tenant_session(rw_engine, seed.tenant_b) as s:
        assert s.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one() >= 1


def test_context_does_not_leak_across_transactions(seed: Seed, rw_engine: Engine) -> None:
    """SET LOCAL expires with the transaction, so a pooled connection reused
    without context sees nothing."""
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert s.execute(select(RlsProbe)).scalars().one().label == "a-row"
    with untenanted_session(rw_engine) as s:
        assert s.execute(select(RlsProbe)).scalars().all() == []


# --- (c) no tenant context: zero rows, inserts fail


def test_no_context_reads_zero_rows(seed: Seed, rw_engine: Engine) -> None:
    with untenanted_session(rw_engine) as s:
        assert s.execute(select(RlsProbe)).scalars().all() == []
        assert s.execute(select(Membership)).scalars().all() == []
        assert s.execute(text('SELECT count(*) FROM "_rls_probe"')).scalar_one() == 0


def test_no_context_insert_fails(seed: Seed, rw_engine: Engine) -> None:
    with pytest.raises(ProgrammingError, match="row-level security"):
        with untenanted_session(rw_engine) as s:
            s.add(RlsProbe(tenant_id=seed.tenant_a, label="no-context"))
            s.flush()


def test_no_context_applies_to_owner_too(seed: Seed, owner_engine: Engine) -> None:
    """FORCE ROW LEVEL SECURITY: the table owner is not exempt."""
    with untenanted_session(owner_engine) as s:
        assert s.execute(select(RlsProbe)).scalars().all() == []


# --- (d) app_rw cannot turn RLS off


@pytest.mark.parametrize(
    "stmt",
    [
        'ALTER TABLE "_rls_probe" DISABLE ROW LEVEL SECURITY',
        'ALTER TABLE "_rls_probe" NO FORCE ROW LEVEL SECURITY',
        f'DROP POLICY {POLICY_NAME} ON "_rls_probe"',
        'ALTER TABLE "membership" DISABLE ROW LEVEL SECURITY',
    ],
)
def test_app_role_cannot_weaken_rls(seed: Seed, rw_engine: Engine, stmt: str) -> None:
    with pytest.raises(ProgrammingError, match="must be owner"):
        with untenanted_session(rw_engine) as s:
            s.execute(text(stmt))


def test_app_role_has_no_bypassrls_or_superuser(rw_engine: Engine) -> None:
    with rw_engine.connect() as conn:
        rolsuper, rolbypassrls = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert (rolsuper, rolbypassrls) == (False, False)


def test_unknown_tenant_context_reads_nothing(seed: Seed, rw_engine: Engine) -> None:
    with tenant_session(rw_engine, uuid.uuid4()) as s:
        assert s.execute(select(RlsProbe)).scalars().all() == []


# --- (e) D-11: extra policies are allow-listed; membership own-read


# (table, policy, command, predicate exactly as Postgres deparses it). Any policy on
# any tenant table that is neither tenant_isolation nor listed here fails the build.
# Adding an entry needs a decision in docs/DECISIONS.md.
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


def test_no_policy_beyond_tenant_isolation_unless_listed(
    migrated_db: None, owner_engine: Engine
) -> None:
    found: set[tuple[str, str, str, str]] = set()
    failures: list[str] = []
    with owner_engine.connect() as conn:
        for table in _tenant_tables(owner_engine):
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
    assert not failures, "\n".join(failures)
    assert found == EXTRA_POLICIES, "an allow-listed policy is missing from the database"
    assert len(EXTRA_POLICIES) == 1  # exactly one after F02 (brief)


def test_user_context_reads_own_memberships_only(seed: Seed, rw_engine: Engine) -> None:
    """No tenant context, only app.user_id: the user's own rows and nothing else,
    via ORM and via raw SQL as app_rw."""
    other_user = seed.users["client_pm"].id
    with untenanted_session(rw_engine) as s:
        set_user_context(s, seed.user_id)
        orm = [(m.user_id, m.tenant_id) for m in s.execute(select(Membership)).scalars().all()]
        raw = s.execute(text("SELECT user_id, tenant_id FROM membership")).all()
        other = s.execute(
            text("SELECT count(*) FROM membership WHERE user_id = :u"), {"u": other_user}
        ).scalar_one()
    assert {t for _, t in orm} == {seed.tenant_a, seed.tenant_b}
    assert all(u == seed.user_id for u, _ in orm)
    assert sorted(r.tenant_id for r in raw) == sorted([seed.tenant_a, seed.tenant_b])
    assert all(r.user_id == seed.user_id for r in raw)
    assert other == 0


def test_no_user_context_reads_zero_memberships(seed: Seed, rw_engine: Engine) -> None:
    with untenanted_session(rw_engine) as s:
        assert s.execute(select(Membership)).scalars().all() == []
        assert s.execute(text("SELECT count(*) FROM membership")).scalar_one() == 0


def test_user_context_alone_cannot_write_membership(
    seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    """The own-read policy is SELECT only: insert is rejected, update and delete
    match zero rows (no write policy applies), so nothing changes."""
    with pytest.raises(ProgrammingError, match="row-level security"):
        with untenanted_session(rw_engine) as s:
            set_user_context(s, seed.user_id)
            s.add(Membership(tenant_id=seed.tenant_new, user_id=seed.user_id, role=Role.client_pm))
            s.flush()
    with untenanted_session(rw_engine) as s:
        set_user_context(s, seed.user_id)
        updated = s.execute(
            text("UPDATE membership SET role = 'client_viewer' WHERE user_id = :u"),
            {"u": seed.user_id},
        ).rowcount
        deleted = s.execute(
            text("DELETE FROM membership WHERE user_id = :u"), {"u": seed.user_id}
        ).rowcount
    assert (updated, deleted) == (0, 0)
    with tenant_session(owner_engine, seed.tenant_a) as s:
        role = s.execute(
            select(Membership.role).where(Membership.user_id == seed.user_id)
        ).scalar_one()
    assert role is None  # the seeded entry row is untouched (D-15: firm users hold NULL)
