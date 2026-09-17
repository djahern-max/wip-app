"""``alembic upgrade head`` then ``downgrade base`` on an empty database."""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from app.tenancy.rls import APPEND_ONLY_FUNCTION
from tests.conftest import OWNER_URL, alembic_config

EXPECTED_TABLES = {
    "firm",
    "tenant",
    "user",
    "membership",
    "_rls_probe",
    "session",
    "audit_log",
    "firm_audit_log",
}
F02_USER_COLUMNS = {"password_hash", "totp_secret_enc", "totp_key_id", "recovery_code_hashes"}


@pytest.fixture
def scratch_db_url(owner_engine: Engine) -> Iterator[str]:
    name = f"wip_mig_{uuid.uuid4().hex[:8]}"
    with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(OWNER_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def _public_tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(
                conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                ).scalars()
            )
    finally:
        engine.dispose()


def _enum_types(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(
                conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).scalars()
            )
    finally:
        engine.dispose()


def _functions(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(
                conn.execute(
                    text(
                        "SELECT proname FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'public'"
                    )
                ).scalars()
            )
    finally:
        engine.dispose()


def _user_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'user'"
                    )
                ).scalars()
            )
    finally:
        engine.dispose()


def test_upgrade_head_then_downgrade_base(scratch_db_url: str) -> None:
    cfg = alembic_config(scratch_db_url)
    assert _public_tables(scratch_db_url) == set()

    command.upgrade(cfg, "head")
    assert _public_tables(scratch_db_url) == EXPECTED_TABLES | {"alembic_version"}
    assert "membership_role" in _enum_types(scratch_db_url)
    assert APPEND_ONLY_FUNCTION in _functions(scratch_db_url)
    assert F02_USER_COLUMNS <= _user_columns(scratch_db_url)

    command.downgrade(cfg, "base")
    assert _public_tables(scratch_db_url) == {"alembic_version"}
    assert "membership_role" not in _enum_types(scratch_db_url)
    assert APPEND_ONLY_FUNCTION not in _functions(scratch_db_url)

    # 0002 alone is reversible: the F01 schema is back, without the F02 columns.
    command.upgrade(cfg, "0001")
    assert F02_USER_COLUMNS.isdisjoint(_user_columns(scratch_db_url))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")
    assert _public_tables(scratch_db_url) == {
        "firm",
        "tenant",
        "user",
        "membership",
        "_rls_probe",
        "alembic_version",
    }
    assert F02_USER_COLUMNS.isdisjoint(_user_columns(scratch_db_url))
    command.downgrade(cfg, "base")

    # And up again: the downgrade left nothing behind that blocks a re-upgrade.
    command.upgrade(cfg, "head")
    assert _public_tables(scratch_db_url) == EXPECTED_TABLES | {"alembic_version"}
