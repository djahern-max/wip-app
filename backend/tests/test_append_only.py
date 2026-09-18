"""Insert-only tables are enforced by trigger (D-13)."""

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.audit.models import AuditLog, FirmAuditLog
from app.core.db import tenant_session, untenanted_session
from app.ingest.models import ImportBatch, RawRecord
from app.tenancy.rls import APPEND_ONLY_FUNCTION, APPEND_ONLY_TABLES, append_only_trigger_names
from tests.conftest import Seed


def _missing_triggers(engine: Engine, tables: tuple[str, ...]) -> list[str]:
    missing: list[str] = []
    with engine.connect() as conn:
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
                    missing.append(f"{table}: trigger {trigger} missing")
                elif enabled == "D":
                    missing.append(f"{table}: trigger {trigger} disabled")
    return missing


def test_every_registered_table_has_both_triggers(migrated_db: None, owner_engine: Engine) -> None:
    assert _missing_triggers(owner_engine, APPEND_ONLY_TABLES) == []


def test_register_matches_catalog(migrated_db: None, owner_engine: Engine) -> None:
    """Every table wired to the trigger function is registered, and vice versa."""
    with owner_engine.connect() as conn:
        tables = set(
            conn.execute(
                text(
                    "SELECT DISTINCT tgrelid::regclass::text FROM pg_trigger "
                    "WHERE tgfoid = to_regproc(:f) AND NOT tgisinternal"
                ),
                {"f": f"public.{APPEND_ONLY_FUNCTION}"},
            ).scalars()
        )
    assert tables == set(APPEND_ONLY_TABLES)


def test_registered_table_without_triggers_would_fail(
    migrated_db: None, owner_engine: Engine
) -> None:
    """Mutation check in-process: a listed table lacking the triggers is a failure."""
    assert _missing_triggers(owner_engine, (*APPEND_ONLY_TABLES, "_rls_probe")) == [
        "_rls_probe: trigger _rls_probe_append_only_row missing",
        "_rls_probe: trigger _rls_probe_append_only_truncate missing",
    ]


APPEND_ONLY_ERROR = "append-only|permission denied"  # app_rw has no TRUNCATE privilege at all


@pytest.mark.parametrize("engine_name", ["rw_engine", "owner_engine"])
@pytest.mark.parametrize("stmt", ["UPDATE", "DELETE", "TRUNCATE"])
def test_audit_log_rejects_changes(
    request: pytest.FixtureRequest, seed: Seed, engine_name: str, stmt: str
) -> None:
    engine: Engine = request.getfixturevalue(engine_name)
    sql = {
        "UPDATE": "UPDATE audit_log SET action = 'x' WHERE id = :id",
        "DELETE": "DELETE FROM audit_log WHERE id = :id",
        "TRUNCATE": "TRUNCATE audit_log",
    }[stmt]
    with pytest.raises(DBAPIError, match=APPEND_ONLY_ERROR):
        with tenant_session(engine, seed.tenant_a) as s:
            row = AuditLog(
                tenant_id=seed.tenant_a, action="probe", entity_type="test", entity_id="x"
            )
            s.add(row)
            s.flush()
            s.execute(text(sql), {"id": row.id})


@pytest.mark.parametrize("engine_name", ["rw_engine", "owner_engine"])
@pytest.mark.parametrize("stmt", ["UPDATE", "DELETE", "TRUNCATE"])
def test_firm_audit_log_rejects_changes(
    request: pytest.FixtureRequest, seed: Seed, engine_name: str, stmt: str
) -> None:
    engine: Engine = request.getfixturevalue(engine_name)
    sql = {
        "UPDATE": "UPDATE firm_audit_log SET action = 'x' WHERE id = :id",
        "DELETE": "DELETE FROM firm_audit_log WHERE id = :id",
        "TRUNCATE": "TRUNCATE firm_audit_log",
    }[stmt]
    with pytest.raises(DBAPIError, match=APPEND_ONLY_ERROR):
        with untenanted_session(engine) as s:
            row = FirmAuditLog(
                firm_id=seed.firm_id, action="probe", entity_type="test", entity_id="x"
            )
            s.add(row)
            s.flush()
            s.execute(text(sql), {"id": row.id})


@pytest.mark.parametrize("engine_name", ["rw_engine", "owner_engine"])
@pytest.mark.parametrize("stmt", ["UPDATE", "DELETE", "TRUNCATE"])
def test_raw_record_rejects_changes(
    request: pytest.FixtureRequest, seed: Seed, engine_name: str, stmt: str
) -> None:
    """D-20: a change to a raw record is a new version, never an UPDATE or DELETE."""
    engine: Engine = request.getfixturevalue(engine_name)
    sql = {
        "UPDATE": "UPDATE raw_record SET is_deleted = true WHERE id = :id",
        "DELETE": "DELETE FROM raw_record WHERE id = :id",
        "TRUNCATE": "TRUNCATE raw_record",
    }[stmt]
    with pytest.raises(DBAPIError, match=APPEND_ONLY_ERROR):
        with tenant_session(engine, seed.tenant_a) as s:
            batch = ImportBatch(
                tenant_id=seed.tenant_a,
                source_kind="unparsed_file",
                sha256=uuid.uuid4().hex * 2,
                byte_size=1,
                original_filename="x",
                object_key=f"tenant/{seed.tenant_a}/imports/x",
            )
            s.add(batch)
            s.flush()
            row = RawRecord(
                tenant_id=seed.tenant_a,
                source="test",
                entity_type="row",
                external_id=uuid.uuid4().hex,
                version=1,
                payload={},
                payload_sha256="0" * 64,
                import_batch_id=batch.id,
            )
            s.add(row)
            s.flush()
            s.execute(text(sql), {"id": row.id})


@pytest.mark.parametrize(
    "stmt",
    [
        "DROP TRIGGER audit_log_append_only_row ON audit_log",
        "DROP TRIGGER raw_record_append_only_row ON raw_record",
        "ALTER TABLE raw_record DISABLE TRIGGER ALL",
        "ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only_row",
        "ALTER TABLE audit_log DISABLE TRIGGER ALL",
        "DROP TRIGGER firm_audit_log_append_only_truncate ON firm_audit_log",
        "ALTER TABLE firm_audit_log DISABLE TRIGGER firm_audit_log_append_only_truncate",
        f"DROP FUNCTION {APPEND_ONLY_FUNCTION}() CASCADE",
        f"CREATE OR REPLACE FUNCTION {APPEND_ONLY_FUNCTION}() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$",
    ],
)
def test_app_role_cannot_remove_or_disable_triggers(
    migrated_db: None, rw_engine: Engine, stmt: str
) -> None:
    # Triggers: "must be owner of relation"; the function: app_rw has no CREATE on
    # the schema and does not own the function, so both forms are refused.
    with pytest.raises(DBAPIError, match="must be owner|permission denied"):
        with untenanted_session(rw_engine) as s:
            s.execute(text(stmt))


def test_inserts_still_work(seed: Seed, rw_engine: Engine) -> None:
    marker = uuid.uuid4().hex
    with tenant_session(rw_engine, seed.tenant_a) as s:
        s.add(AuditLog(tenant_id=seed.tenant_a, action="probe", entity_type="t", entity_id=marker))
    with untenanted_session(rw_engine) as s:
        s.add(FirmAuditLog(firm_id=seed.firm_id, action="probe", entity_type="t", entity_id=marker))
    with tenant_session(rw_engine, seed.tenant_a) as s:
        n = s.execute(
            text("SELECT count(*) FROM audit_log WHERE entity_id = :m"), {"m": marker}
        ).scalar_one()
    assert n == 1
