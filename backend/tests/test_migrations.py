"""``alembic upgrade head`` then ``downgrade base`` on an empty database, and the
``0003`` data step on F02-shaped rows (D-15): per-tenant context, no unforcing."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from app.tenancy.rls import APPEND_ONLY_FUNCTION
from tests.conftest import alembic_config

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


def _user_columns(url: str) -> set[str]:
    return _query(
        url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'user'",
    )


def _constraints(url: str, table: str) -> set[str]:
    return _query(
        url,
        f"SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('public.{table}')",
    )


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
