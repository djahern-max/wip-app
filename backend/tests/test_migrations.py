"""``alembic upgrade head`` then ``downgrade base`` on an empty database, and the
``0003`` data step on F02-shaped rows (D-15): per-tenant context, no unforcing."""

import os
import subprocess
import sys
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from app.tenancy.rls import APPEND_ONLY_FUNCTION
from tests.conftest import BACKEND_DIR, alembic_config

EXPECTED_TABLES = {
    "firm",
    "tenant",
    "user",
    "membership",
    "_rls_probe",
    "session",
    "audit_log",
    "firm_audit_log",
    "firm_membership",
    # F03 (0004)
    "connection",
    "sync_run",
    "import_batch",
    "raw_record",
    "task",
    # F04 (0005)
    "division",
    "cost_category",
    "gl_account",
    "account_map",
    "account_suggest_rule",
    "tenant_policy",
    "burden_rate",
    # F05 (0008)
    "customer",
    "billing",
    "billing_line",
    "payment",
    "payment_application",
    # F05.1 (0009): tenant-less, append-only (D-29)
    "webhook_event",
}
F03_TABLES = {"connection", "sync_run", "import_batch", "raw_record", "task"}
F05_TABLES = {"customer", "billing", "billing_line", "payment", "payment_application"}
F04_TABLES = {
    "division",
    "cost_category",
    "gl_account",
    "account_map",
    "account_suggest_rule",
    "tenant_policy",
    "burden_rate",
}
F02_USER_COLUMNS = {"password_hash", "totp_secret_enc", "totp_key_id", "recovery_code_hashes"}
F02_1_USER_COLUMNS = {"activation_token_hash", "activation_expires_at"}
F02_ONLY_USER_COLUMNS = {"password_reset_token_hash", "password_reset_expires_at"}


def _query(url: str, sql: str) -> set:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(conn.execute(text(sql)).scalars())
    finally:
        engine.dispose()


def _public_tables(url: str) -> set[str]:
    return _query(
        url,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
    )


def _enum_types(url: str) -> set[str]:
    return _query(url, "SELECT typname FROM pg_type WHERE typtype = 'e'")


def _functions(url: str) -> set[str]:
    return _query(
        url,
        "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'",
    )


def _columns(url: str, table: str) -> set[str]:
    return _query(
        url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{table}'",
    )


def _user_columns(url: str) -> set[str]:
    return _columns(url, "user")


def _triggers(url: str) -> set[str]:
    return _query(url, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")


def _constraints(url: str, table: str) -> set[str]:
    return _query(
        url,
        f"SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('public.{table}')",
    )


F05_CONNECTION_COLUMNS = {
    "realm_id",
    "environment",
    "company_name",
    "refresh_token_expires_at",
    "tokens_refreshed_at",
    "oauth_state_sha256",
    "oauth_state_user_id",
    "oauth_state_expires_at",
}


def _check_sql(url: str, name: str) -> str:
    (sql,) = _query(
        url, f"SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = '{name}'"
    )
    return sql


def test_upgrade_head_then_downgrade_base(scratch_db_url: str) -> None:
    cfg = alembic_config(scratch_db_url)
    assert _public_tables(scratch_db_url) == set()

    command.upgrade(cfg, "head")
    assert _public_tables(scratch_db_url) == EXPECTED_TABLES | {"alembic_version"}
    assert "membership_role" in _enum_types(scratch_db_url)
    assert APPEND_ONLY_FUNCTION in _functions(scratch_db_url)
    assert F02_USER_COLUMNS | F02_1_USER_COLUMNS <= _user_columns(scratch_db_url)
    assert F02_ONLY_USER_COLUMNS.isdisjoint(_user_columns(scratch_db_url))
    assert "ck_membership_role_client" in _constraints(scratch_db_url, "membership")
    assert "ck_firm_membership_role_firm" in _constraints(scratch_db_url, "firm_membership")

    command.downgrade(cfg, "base")
    assert _public_tables(scratch_db_url) == {"alembic_version"}
    assert "membership_role" not in _enum_types(scratch_db_url)
    assert APPEND_ONLY_FUNCTION not in _functions(scratch_db_url)

    # 0002 alone is reversible: the F01 schema is back, without the F02 columns.
    command.upgrade(cfg, "0001")
    assert F02_USER_COLUMNS.isdisjoint(_user_columns(scratch_db_url))
    command.upgrade(cfg, "0002")
    assert F02_ONLY_USER_COLUMNS <= _user_columns(scratch_db_url)
    assert "firm_membership" not in _public_tables(scratch_db_url)
    # 0008 alone is reversible (F05): the five billing tables come and go, with their
    # RLS and the total identity CHECK.
    command.upgrade(cfg, "head")
    assert F05_TABLES <= _public_tables(scratch_db_url)
    assert "ck_billing_total_identity" in _constraints(scratch_db_url, "billing")
    assert _query(scratch_db_url, "SELECT count(*) FROM billing") == {0}
    command.downgrade(cfg, "0007")
    assert F05_TABLES.isdisjoint(_public_tables(scratch_db_url))
    assert "realm_id" in _columns(scratch_db_url, "connection")
    # 0007 alone is reversible (F05): the connection's company, token bookkeeping and
    # pending-state columns, the one-company-one-tenant index and sync_run.detail.
    command.upgrade(cfg, "head")
    assert F05_CONNECTION_COLUMNS <= _columns(scratch_db_url, "connection")
    assert "detail" in _columns(scratch_db_url, "sync_run")
    assert _query(
        scratch_db_url,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_connection_system_realm_id'",
    ) == {
        "CREATE UNIQUE INDEX uq_connection_system_realm_id ON public.connection "
        "USING btree (system, realm_id) WHERE (realm_id IS NOT NULL)"
    }
    command.downgrade(cfg, "0006")
    assert F05_CONNECTION_COLUMNS.isdisjoint(_columns(scratch_db_url, "connection"))
    assert "detail" not in _columns(scratch_db_url, "sync_run")
    assert "token_expires_at" in _columns(scratch_db_url, "connection")  # F03's stay
    # 0006 alone is reversible (F04 patch): the status CHECK gains and loses
    # ``nothing_loaded`` and the follow-up outcome column comes and goes.
    command.upgrade(cfg, "head")
    assert "followup_outcome" in _columns(scratch_db_url, "import_batch")
    assert "nothing_loaded" in _check_sql(scratch_db_url, "ck_import_batch_status")
    assert "ck_import_batch_followup_outcome" in _constraints(scratch_db_url, "import_batch")
    command.downgrade(cfg, "0005")
    assert "followup_outcome" not in _columns(scratch_db_url, "import_batch")
    assert "nothing_loaded" not in _check_sql(scratch_db_url, "ck_import_batch_status")
    assert "loaded_with_issues" in _check_sql(scratch_db_url, "ck_import_batch_status")
    # 0005 alone is reversible (F04): the seven tables and the followup column go;
    # no tenant_policy or cost_category row is seeded by the migration.
    command.upgrade(cfg, "head")
    assert F04_TABLES <= _public_tables(scratch_db_url)
    assert "followup_task_id" in _columns(scratch_db_url, "import_batch")
    assert _query(scratch_db_url, "SELECT count(*) FROM cost_category") == {0}
    assert _query(scratch_db_url, "SELECT count(*) FROM tenant_policy") == {0}
    command.downgrade(cfg, "0004")
    assert F04_TABLES.isdisjoint(_public_tables(scratch_db_url))
    assert "followup_task_id" not in _columns(scratch_db_url, "import_batch")
    # 0004 alone is reversible (F03): the five tables go, the audit tables keep
    # their append-only triggers and the function.
    command.upgrade(cfg, "head")
    assert "raw_record_append_only_row" in _triggers(scratch_db_url)
    command.downgrade(cfg, "0003")
    assert F03_TABLES.isdisjoint(_public_tables(scratch_db_url))
    assert "audit_log_append_only_row" in _triggers(scratch_db_url)
    assert APPEND_ONLY_FUNCTION in _functions(scratch_db_url)
    # 0003 alone is reversible.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002")
    assert "firm_membership" not in _public_tables(scratch_db_url)
    assert F02_ONLY_USER_COLUMNS <= _user_columns(scratch_db_url)
    assert "ck_membership_role_client" not in _constraints(scratch_db_url, "membership")
    command.downgrade(cfg, "0001")
    assert _public_tables(scratch_db_url) == {
        "firm",
        "tenant",
        "user",
        "membership",
        "_rls_probe",
        "alembic_version",
    }
    command.downgrade(cfg, "base")

    # And up again: the downgrade left nothing behind that blocks a re-upgrade.
    command.upgrade(cfg, "head")
    assert _public_tables(scratch_db_url) == EXPECTED_TABLES | {"alembic_version"}


def test_0003_data_step_derives_firm_membership_and_is_reversible(scratch_db_url: str) -> None:
    """F02-shaped rows: firm roles inside membership. After 0003 they are one
    firm_membership per (firm, user) with the highest role, and entry rows; after
    the downgrade the F02 rows are back. The step never unforces RLS."""
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "0002")
    engine = create_engine(scratch_db_url)
    firm, other = uuid.uuid4(), uuid.uuid4()
    ta, tb, tc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    admin_staff, staff_only, client, mixed_other = (uuid.uuid4() for _ in range(4))
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO firm (id, name) VALUES (:f, 'F'), (:o, 'O')"),
                {"f": firm, "o": other},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant (id, firm_id, name, slug) VALUES "
                    "(:a, :f, 'A', 'a'), (:b, :f, 'B', 'b'), (:c, :o, 'C', 'c')"
                ),
                {"a": ta, "b": tb, "c": tc, "f": firm, "o": other},
            )
            for uid, email in (
                (admin_staff, "as@x.test"),
                (staff_only, "so@x.test"),
                (client, "cl@x.test"),
                (mixed_other, "mo@x.test"),
            ):
                conn.execute(
                    text('INSERT INTO "user" (id, email, display_name) VALUES (:u, :e, :e)'),
                    {"u": uid, "e": email},
                )
            rows = [
                (ta, admin_staff, "firm_staff"),
                (tb, admin_staff, "firm_admin"),  # highest wins → firm_admin
                (ta, staff_only, "firm_staff"),
                (ta, client, "client_pm"),
                (tc, mixed_other, "firm_staff"),  # other firm
                (ta, mixed_other, "client_viewer"),  # client in firm F: allowed (different firm)
            ]
            for tid, uid, role in rows:
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
                conn.execute(
                    text(
                        "INSERT INTO membership (id, tenant_id, user_id, role) "
                        "VALUES (:id, :t, :u, CAST(:r AS membership_role))"
                    ),
                    {"id": uuid.uuid4(), "t": tid, "u": uid, "r": role},
                )
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            forced = conn.execute(
                text(
                    "SELECT relforcerowsecurity FROM pg_class WHERE oid = to_regclass('membership')"
                )
            ).scalar_one()
            assert forced is True
            fms = {
                (r.firm_id, r.user_id): r.role
                for r in conn.execute(
                    text("SELECT firm_id, user_id, role::text AS role FROM firm_membership")
                )
            }
            assert fms == {
                (firm, admin_staff): "firm_admin",
                (firm, staff_only): "firm_staff",
                (other, mixed_other): "firm_staff",
            }
            roles = {}
            for tid in (ta, tb, tc):
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
                for r in conn.execute(
                    text("SELECT user_id, role::text AS role FROM membership WHERE tenant_id = :t"),
                    {"t": tid},
                ):
                    roles[(tid, r.user_id)] = r.role
            conn.rollback()
        assert roles == {
            (ta, admin_staff): None,
            (tb, admin_staff): None,
            (ta, staff_only): None,
            (ta, client): "client_pm",
            (tc, mixed_other): None,
            (ta, mixed_other): "client_viewer",
        }
        command.downgrade(cfg, "0002")
        with engine.connect() as conn:
            back = {}
            for tid in (ta, tb, tc):
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
                for r in conn.execute(
                    text("SELECT user_id, role::text AS role FROM membership WHERE tenant_id = :t"),
                    {"t": tid},
                ):
                    back[(tid, r.user_id)] = r.role
            conn.rollback()
        # Roles are restored from firm_membership: the highest role applies to every
        # entry row of that user (the F02 per-tenant nuance is not recoverable).
        assert back == {
            (ta, admin_staff): "firm_admin",
            (tb, admin_staff): "firm_admin",
            (ta, staff_only): "firm_staff",
            (ta, client): "client_pm",
            (tc, mixed_other): "firm_staff",
            (ta, mixed_other): "client_viewer",
        }
    finally:
        engine.dispose()


def test_0003_refuses_a_user_with_both_a_firm_and_a_client_role_in_one_firm(
    scratch_db_url: str,
) -> None:
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "0002")
    engine = create_engine(scratch_db_url)
    firm, ta, tb, both = (uuid.uuid4() for _ in range(4))
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO firm (id, name) VALUES (:f, 'F')"), {"f": firm})
            conn.execute(
                text(
                    "INSERT INTO tenant (id, firm_id, name, slug) "
                    "VALUES (:a, :f, 'A', 'a'), (:b, :f, 'B', 'b')"
                ),
                {"a": ta, "b": tb, "f": firm},
            )
            conn.execute(
                text('INSERT INTO "user" (id, email, display_name) VALUES (:u, :e, :e)'),
                {"u": both, "e": "both@x.test"},
            )
            for tid, role in ((ta, "firm_staff"), (tb, "client_admin")):
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
                conn.execute(
                    text(
                        "INSERT INTO membership (id, tenant_id, user_id, role) "
                        "VALUES (:id, :t, :u, CAST(:r AS membership_role))"
                    ),
                    {"id": uuid.uuid4(), "t": tid, "u": both, "r": role},
                )
        with pytest.raises(RuntimeError, match=str(both)):
            command.upgrade(cfg, "head")
        # The failed migration rolled back: still at 0002, nothing half-applied.
        assert "firm_membership" not in _public_tables(scratch_db_url)
    finally:
        engine.dispose()


# --- the migration process holds the owner URL and nothing else -----------------------------


def _alembic_cli(*args: str, owner_url: str | None) -> subprocess.CompletedProcess:
    """``alembic`` as CI and ``make migrate`` run it: a separate process, no env file,
    and none of the API's required settings."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATABASE_", "CRYPTO_", "TEST_DATABASE_"))
    }
    env["ENV_FILE"] = "/nonexistent/.env"
    if owner_url is not None:
        env["DATABASE_OWNER_URL"] = owner_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_alembic_upgrade_head_needs_only_the_owner_url(scratch_db_url: str) -> None:
    """CRYPTO_KEYS, CRYPTO_ACTIVE_KEY_ID and DATABASE_URL are unset: no migration
    needs them, and ``alembic/env.py`` must not ask for them."""
    up = _alembic_cli("upgrade", "head", owner_url=scratch_db_url)
    assert up.returncode == 0, up.stderr
    assert _public_tables(scratch_db_url) == EXPECTED_TABLES | {"alembic_version"}
    down = _alembic_cli("downgrade", "base", owner_url=scratch_db_url)
    assert down.returncode == 0, down.stderr
    assert _public_tables(scratch_db_url) == {"alembic_version"}


def test_alembic_without_the_owner_url_names_only_that_variable() -> None:
    proc = _alembic_cli("upgrade", "head", owner_url=None)
    assert proc.returncode != 0
    assert "DATABASE_OWNER_URL" in proc.stderr
    assert "CRYPTO" not in proc.stderr


# --- destructive-downgrade guard (F03 close-out) ------------------------------------------------


def test_downgrade_is_refused_for_a_protected_database_name_and_without_the_flag(
    scratch_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database named like the dev database: the downgrade exits non-zero naming it
    and changes nothing. Without ALLOW_DESTRUCTIVE_DOWNGRADE=1 the same happens for
    any database."""
    from sqlalchemy import make_url

    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "head")
    before = _public_tables(scratch_db_url)
    name = make_url(scratch_db_url).database
    monkeypatch.setenv("PROTECTED_DATABASE_NAMES", f"wip,{name}")
    with pytest.raises(SystemExit) as excinfo:
        command.downgrade(cfg, "base")
    assert excinfo.value.code != 0 and name in str(excinfo.value.code)
    assert "nothing was changed" in str(excinfo.value.code)
    assert _public_tables(scratch_db_url) == before
    monkeypatch.delenv("PROTECTED_DATABASE_NAMES")
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_DOWNGRADE")
    with pytest.raises(SystemExit) as excinfo:
        command.downgrade(cfg, "base")
    assert "ALLOW_DESTRUCTIVE_DOWNGRADE" in str(excinfo.value.code) and name in str(
        excinfo.value.code
    )
    assert _public_tables(scratch_db_url) == before
    # The default protected name is the dev database, whatever the flag says.
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_DOWNGRADE", "1")
    dev_cfg = alembic_config(
        make_url(scratch_db_url).set(database="wip").render_as_string(hide_password=False)
    )
    with pytest.raises(SystemExit) as excinfo:
        command.downgrade(dev_cfg, "base")
    assert "'wip'" in str(excinfo.value.code)
    # Upgrades are never guarded; a scratch database with the flag downgrades.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert _public_tables(scratch_db_url) == {"alembic_version"}


def test_make_downgrade_refuses_without_the_flag() -> None:
    proc = subprocess.run(
        ["make", "-n", "downgrade"], cwd=BACKEND_DIR + "/..", capture_output=True, text=True
    )
    assert "ALLOW_DESTRUCTIVE_DOWNGRADE" in proc.stdout
    env = {k: v for k, v in os.environ.items() if k != "ALLOW_DESTRUCTIVE_DOWNGRADE"}
    proc = subprocess.run(
        ["make", "downgrade"], cwd=BACKEND_DIR + "/..", capture_output=True, text=True, env=env
    )
    assert proc.returncode != 0 and "refusing" in proc.stdout


def test_0006_downgrade_renames_nothing_loaded_per_tenant_and_upgrade_rewrites_no_row(
    scratch_db_url: str,
) -> None:
    """``nothing_loaded`` rows become ``loaded_with_issues`` on the way down, in every
    tenant, with RLS still enabled and forced; the way up rewrites no row (the owner's
    2026-09-19 workbook batch stays ``loaded_with_issues`` with 0 rows loaded)."""
    cfg = alembic_config(scratch_db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(scratch_db_url)
    firm, ta, tb = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    insert = text(
        "INSERT INTO import_batch (id, tenant_id, source_kind, sha256, byte_size, "
        "original_filename, object_key, status, rows_loaded, rows_rejected, followup_outcome) "
        "VALUES (:id, :t, 'chart_of_accounts', :sha, 1, 'f.csv', :key, :status, :loaded, 3, :o)"
    )

    def statuses() -> dict[str, list[str]]:
        out = {}
        with engine.begin() as conn:
            for t in (ta, tb):
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t)})
                out[str(t)] = sorted(
                    conn.execute(text("SELECT status FROM import_batch")).scalars()
                )
        return out

    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO firm (id, name) VALUES (:f, 'F')"), {"f": firm})
            conn.execute(
                text(
                    "INSERT INTO tenant (id, firm_id, name, slug) VALUES "
                    "(:a, :f, 'A', 'a'), (:b, :f, 'B', 'b')"
                ),
                {"a": ta, "b": tb, "f": firm},
            )
            for t in (ta, tb):
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t)})
                for status, loaded, outcome in (
                    ("nothing_loaded", 0, None),
                    ("loaded", 5, "superseded"),
                ):
                    conn.execute(
                        insert,
                        {
                            "id": uuid.uuid4(),
                            "t": t,
                            "sha": uuid.uuid4().hex * 2,
                            "key": f"tenant/{t}/imports/x.csv",
                            "status": status,
                            "loaded": loaded,
                            "o": outcome,
                        },
                    )
        command.downgrade(cfg, "0005")
        assert statuses() == {str(ta): ["loaded", "loaded_with_issues"]} | {
            str(tb): ["loaded", "loaded_with_issues"]
        }
        assert _query(
            scratch_db_url,
            "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
            "WHERE relname = 'import_batch'",
        ) == {True}
        command.upgrade(cfg, "head")
        assert statuses()[str(ta)] == ["loaded", "loaded_with_issues"]  # not rewritten
    finally:
        engine.dispose()
