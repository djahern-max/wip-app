"""The raw store (D-20) and the money-safe JSON path (CLAUDE.md Money; owner
conditions on plan call 2): versions by payload hash, source deletes as flagged
versions, Decimal round trip with JSON numbers in storage, floats refused."""

import uuid
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import StatementError

from app.audit.models import AuditLog
from app.core.db import tenant_session
from app.core.jsoncodec import JSONEncodeError, payload_sha256
from app.ingest.models import ImportBatch, RawRecord
from app.ingest.raw import RawOrigin, latest_raw, raw_history, store_raw
from tests.conftest import Seed


def make_batch(engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    with tenant_session(engine, tenant_id) as s:
        b = ImportBatch(
            tenant_id=tenant_id,
            source_kind="unparsed_file",
            sha256=uuid.uuid4().hex * 2,
            byte_size=0,
            original_filename="seed.bin",
            object_key=f"tenant/{tenant_id}/imports/seed.bin",
        )
        s.add(b)
        s.flush()
        return b.id


@pytest.fixture
def batch_a(seed: Seed, rw_engine: Engine) -> uuid.UUID:
    return make_batch(rw_engine, seed.tenant_a)


def test_origin_is_exactly_one() -> None:
    with pytest.raises(ValueError):
        RawOrigin()
    with pytest.raises(ValueError):
        RawOrigin(import_batch_id=uuid.uuid4(), sync_run_id=uuid.uuid4())


def test_same_payload_writes_nothing_and_a_change_is_a_new_version(
    seed: Seed, rw_engine: Engine, batch_a: uuid.UUID
) -> None:
    key = ("test", "row", uuid.uuid4().hex)
    origin = RawOrigin(import_batch_id=batch_a)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        r1 = store_raw(s, seed.tenant_a, *key, {"amount": Decimal("1.00")}, origin)
        r2 = store_raw(s, seed.tenant_a, *key, {"amount": Decimal("1.00")}, origin)
        r3 = store_raw(s, seed.tenant_a, *key, {"amount": Decimal("1.50")}, origin)
        assert (r1.version, r2.version, r3.version) == (1, 1, 2)
        assert r1.record is not None and r2.record is None and r3.record is not None
        history = raw_history(s, seed.tenant_a, *key)
        assert [h.version for h in history] == [1, 2]
        assert history[0].payload == {"amount": Decimal("1.00")}  # prior version untouched
        assert latest_raw(s, seed.tenant_a, *key).version == 2
        # A different key of the same entity is independent.
        other = store_raw(s, seed.tenant_a, "test", "row", uuid.uuid4().hex, {}, origin)
        assert other.version == 1


def test_source_delete_is_a_flagged_version_with_the_last_payload(
    seed: Seed, rw_engine: Engine, batch_a: uuid.UUID
) -> None:
    key = ("test", "row", uuid.uuid4().hex)
    origin = RawOrigin(import_batch_id=batch_a)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        store_raw(s, seed.tenant_a, *key, {"name": "a"}, origin)
        gone = store_raw(s, seed.tenant_a, *key, None, origin, deleted=True)
        assert gone.version == 2 and gone.record.is_deleted and gone.record.payload == {"name": "a"}
        # Reporting the delete again writes nothing; the record reappearing is v3.
        assert store_raw(s, seed.tenant_a, *key, None, origin, deleted=True).record is None
        back = store_raw(s, seed.tenant_a, *key, {"name": "a"}, origin)
        assert back.version == 3 and not back.record.is_deleted
        latest = latest_raw(s, seed.tenant_a, *key)
        assert (latest.version, latest.is_deleted) == (3, False)
        assert [(h.version, h.is_deleted) for h in raw_history(s, seed.tenant_a, *key)] == [
            (1, False),
            (2, True),
            (3, False),
        ]


# --- Decimal round trip ------------------------------------------------------------------------

PAYLOAD = {
    "total": Decimal("362.07"),
    "rate": Decimal("0.10"),
    "drift": Decimal("141366.68000000002"),
    "big": 12345678901234567890123,
    "lines": [{"amount": Decimal("141366.68")}, {"amount": Decimal("-5.00")}],
    "name": "Rye Beach",
}


def test_decimal_round_trip_through_raw_record(
    seed: Seed, rw_engine: Engine, batch_a: uuid.UUID
) -> None:
    ext = uuid.uuid4().hex
    with tenant_session(rw_engine, seed.tenant_a) as s:
        stored = store_raw(s, seed.tenant_a, "test", "row", ext, PAYLOAD, RawOrigin(batch_a))
        row_id = stored.record.id
        assert stored.record.payload_sha256 == payload_sha256(PAYLOAD)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        # ORM path
        back = s.get(RawRecord, row_id).payload
        assert back == PAYLOAD
        for k in ("total", "rate", "drift"):
            assert isinstance(back[k], Decimal) and str(back[k]) == str(PAYLOAD[k])
        assert isinstance(back["big"], int) and back["big"] == PAYLOAD["big"]
        assert all(isinstance(line["amount"], Decimal) for line in back["lines"])
        assert str(back["lines"][0]["amount"]) == "141366.68"
        # raw-SQL path reads through the same driver-level loader
        raw = s.execute(text("SELECT payload FROM raw_record WHERE id = :id"), {"id": row_id})
        raw_payload = raw.scalar_one()
        assert raw_payload == PAYLOAD and isinstance(raw_payload["drift"], Decimal)
        assert str(raw_payload["drift"]) == "141366.68000000002"
        # stored as JSON numbers, not strings
        types = s.execute(
            text(
                "SELECT jsonb_typeof(payload->'total'), jsonb_typeof(payload->'drift'), "
                "jsonb_typeof(payload->'big'), jsonb_typeof(payload->'lines'->0->'amount') "
                "FROM raw_record WHERE id = :id"
            ),
            {"id": row_id},
        ).one()
        assert tuple(types) == ("number", "number", "number", "number")
        # and with every digit Postgres keeps
        assert (
            s.execute(
                text("SELECT payload->>'drift' FROM raw_record WHERE id = :id"), {"id": row_id}
            ).scalar_one()
            == "141366.68000000002"
        )


def test_raw_sql_write_uses_the_same_serializer(
    seed: Seed, rw_engine: Engine, batch_a: uuid.UUID
) -> None:
    """A ``Jsonb(...)`` parameter on a raw statement goes through the driver-level
    dumps installed by the engine: Decimal as a number, float refused."""
    ext = uuid.uuid4().hex
    with tenant_session(rw_engine, seed.tenant_a) as s:
        s.execute(
            text(
                "INSERT INTO raw_record (id, tenant_id, source, entity_type, external_id, "
                "version, payload, payload_sha256, import_batch_id) VALUES (:id, :t, 'test', "
                "'row', :e, 1, :p, :h, :b)"
            ),
            {
                "id": uuid.uuid4(),
                "t": seed.tenant_a,
                "e": ext,
                "p": Jsonb({"amount": Decimal("0.10")}),
                "h": payload_sha256({"amount": Decimal("0.10")}),
                "b": batch_a,
            },
        )
        typ, txt = s.execute(
            text(
                "SELECT jsonb_typeof(payload->'amount'), payload->>'amount' FROM raw_record "
                "WHERE external_id = :e"
            ),
            {"e": ext},
        ).one()
        assert (typ, txt) == ("number", "0.10")
    with pytest.raises((JSONEncodeError, StatementError, TypeError)) as excinfo:
        with tenant_session(rw_engine, seed.tenant_a) as s:
            s.execute(text("SELECT CAST(:p AS jsonb)"), {"p": Jsonb({"amount": 1.5})})
    assert "$.amount" in str(excinfo.value)


def test_float_anywhere_in_a_jsonb_value_is_refused_naming_the_path(
    seed: Seed, rw_engine: Engine, batch_a: uuid.UUID
) -> None:
    bad = {"lines": [{"amount": Decimal("1")}, {"amount": 2.5}]}
    with pytest.raises((JSONEncodeError, StatementError)) as excinfo:
        with tenant_session(rw_engine, seed.tenant_a) as s:
            store_raw(s, seed.tenant_a, "test", "row", uuid.uuid4().hex, bad, RawOrigin(batch_a))
    message = str(excinfo.value)
    assert "$.lines[1].amount" in message and "2.5" not in message
    # The hash step refuses it first, before any SQL, with the same path.
    with pytest.raises(JSONEncodeError, match=r"\$\.lines\[1\]\.amount"):
        payload_sha256(bad)


def test_existing_audit_detail_rows_read_back_unchanged(seed: Seed, rw_engine: Engine) -> None:
    """``audit_log.detail`` (F02) goes through the same codec: str/int/bool/None and
    nesting survive exactly, via ORM and raw SQL."""
    detail = {"step": "password", "count": 3, "ok": True, "none": None, "nested": {"z": [1, "a"]}}
    with tenant_session(rw_engine, seed.tenant_a) as s:
        row = AuditLog(
            tenant_id=seed.tenant_a, action="probe", entity_type="t", entity_id="x", detail=detail
        )
        s.add(row)
        s.flush()
        row_id = row.id
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert (
            s.execute(select(AuditLog.detail).where(AuditLog.id == row_id)).scalar_one() == detail
        )
        raw = s.execute(
            text("SELECT detail FROM audit_log WHERE id = :id"), {"id": row_id}
        ).scalar_one()
        assert raw == detail and type(raw["count"]) is int
