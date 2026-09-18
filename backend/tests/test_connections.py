"""Connection tokens (F03; BLUEPRINT §11): ciphertext with key id, bound by
associated data to tenant, connection and field; never in an audit row, a log
line, ``last_error`` or a response (the end-of-run scans check the recorded
tokens)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select, text

from app.audit.models import AuditLog
from app.core.crypto import CryptoError
from app.core.db import tenant_session
from app.ingest.connections import (
    get_connection_tokens,
    get_or_create_connection,
    set_connection_tokens,
)
from app.ingest.models import Connection
from app.tenancy.models import Role
from tests.conftest import Seed
from tests.leaks import record_secret


def _token(label: str) -> str:
    t = f"{label}-token-{uuid.uuid4().hex}"
    record_secret("connection_token", t)
    return t


def _set(
    engine: Engine, seed: Seed, tenant_id: uuid.UUID, system: str
) -> tuple[uuid.UUID, str, str]:
    access, refresh = _token("access"), _token("refresh")
    with tenant_session(engine, tenant_id) as s:
        conn = get_or_create_connection(s, tenant_id, system)
        set_connection_tokens(
            s,
            conn,
            access_token=access,
            refresh_token=refresh,
            token_expires_at=datetime.now(UTC),
            actor_user_id=seed.users["firm_admin"].id,
            actor_role=Role.firm_admin,
        )
        return conn.id, access, refresh


def test_tokens_are_ciphertext_with_key_id_and_round_trip(
    seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    conn_id, access, refresh = _set(rw_engine, seed, seed.tenant_a, "qbo")
    with tenant_session(owner_engine, seed.tenant_a) as c:
        row = c.execute(
            text(
                "SELECT access_token_enc, access_token_key_id, refresh_token_enc, "
                "refresh_token_key_id, status, last_error FROM connection WHERE id = :id"
            ),
            {"id": conn_id},
        ).one()
    assert row.access_token_key_id == "test1" and row.refresh_token_key_id == "test1"
    assert access.encode() not in bytes(row.access_token_enc)
    assert refresh.encode() not in bytes(row.refresh_token_enc)
    assert (row.status, row.last_error) == ("connected", None)
    with tenant_session(rw_engine, seed.tenant_a) as s:
        conn = s.get(Connection, conn_id)
        assert get_connection_tokens(conn) == (access, refresh)
        # Idempotent: the same system returns the same row.
        assert get_or_create_connection(s, seed.tenant_a, "qbo").id == conn_id


def test_ciphertext_moved_to_another_row_field_or_tenant_fails_to_decrypt(
    seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    a_id, access, _ = _set(rw_engine, seed, seed.tenant_a, "qbo")
    other_id, _, _ = _set(rw_engine, seed, seed.tenant_a, "file")
    b_id, _, _ = _set(rw_engine, seed, seed.tenant_b, "qbo")
    with tenant_session(owner_engine, seed.tenant_a) as c:
        enc, kid = c.execute(
            text("SELECT access_token_enc, access_token_key_id FROM connection WHERE id = :id"),
            {"id": a_id},
        ).one()
    # Another connection in the same tenant.
    with tenant_session(owner_engine, seed.tenant_a) as s:
        s.execute(
            text(
                "UPDATE connection SET access_token_enc = :e, access_token_key_id = :k "
                "WHERE id = :id"
            ),
            {"e": enc, "k": kid, "id": other_id},
        )
    with tenant_session(rw_engine, seed.tenant_a) as s:
        with pytest.raises(CryptoError, match="decryption failed"):
            get_connection_tokens(s.get(Connection, other_id))
    # Another field of the same connection.
    with tenant_session(owner_engine, seed.tenant_a) as s:
        s.execute(
            text(
                "UPDATE connection SET refresh_token_enc = :e, refresh_token_key_id = :k "
                "WHERE id = :id"
            ),
            {"e": enc, "k": kid, "id": a_id},
        )
    with tenant_session(rw_engine, seed.tenant_a) as s:
        with pytest.raises(CryptoError, match="decryption failed"):
            get_connection_tokens(s.get(Connection, a_id))
    # Another tenant.
    with tenant_session(owner_engine, seed.tenant_b) as s:
        s.execute(
            text(
                "UPDATE connection SET access_token_enc = :e, access_token_key_id = :k "
                "WHERE id = :id"
            ),
            {"e": enc, "k": kid, "id": b_id},
        )
    with tenant_session(rw_engine, seed.tenant_b) as s:
        with pytest.raises(CryptoError, match="decryption failed"):
            get_connection_tokens(s.get(Connection, b_id))
    # Leave no deliberately corrupted row behind (scripts/reencrypt.py would report it).
    for tid, cid in ((seed.tenant_a, a_id), (seed.tenant_a, other_id), (seed.tenant_b, b_id)):
        with tenant_session(owner_engine, tid) as s:
            s.execute(text("DELETE FROM connection WHERE id = :id"), {"id": cid})


def test_setting_tokens_is_audited_without_values_in_the_same_transaction(
    seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    conn_id, access, refresh = _set(rw_engine, seed, seed.tenant_a, "qbo")
    with tenant_session(rw_engine, seed.tenant_a) as s:
        rows = (
            s.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "connection", AuditLog.entity_id == str(conn_id))
                .order_by(AuditLog.occurred_at.desc())
            )
            .scalars()
            .all()
        )
        assert rows and rows[0].action == "connection_tokens_set"
        assert rows[0].detail == {"system": "qbo", "has_refresh_token": True}
    with tenant_session(owner_engine, seed.tenant_a) as c:
        blob = c.execute(text("SELECT string_agg(detail::text, ' ') FROM audit_log")).scalar_one()
        errors = c.execute(
            text("SELECT string_agg(coalesce(last_error, ''), ' ') FROM connection")
        ).scalar_one()
    assert blob and access not in blob and refresh not in blob
    assert access not in (errors or "") and refresh not in (errors or "")
    # A rolled-back write leaves neither the tokens nor the audit row.
    with pytest.raises(RuntimeError):
        with tenant_session(rw_engine, seed.tenant_a) as s:
            conn = get_or_create_connection(s, seed.tenant_a, "qbo")
            set_connection_tokens(
                s,
                conn,
                access_token=_token("rolled"),
                refresh_token=None,
                token_expires_at=None,
                actor_user_id=None,
                actor_role=None,
            )
            raise RuntimeError("abort")
    with tenant_session(rw_engine, seed.tenant_a) as s:
        assert get_connection_tokens(s.get(Connection, conn_id)) == (access, refresh)
        n = (
            s.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "connection", AuditLog.entity_id == str(conn_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(n) == len(rows)


def test_unknown_system_is_refused(seed: Seed, rw_engine: Engine) -> None:
    with pytest.raises(ValueError, match="unknown system"):
        with tenant_session(rw_engine, seed.tenant_a) as s:
            get_or_create_connection(s, seed.tenant_a, "spreadsheet")


def test_connection_rows_are_tenant_scoped(seed: Seed, rw_engine: Engine) -> None:
    a_id, _, _ = _set(rw_engine, seed, seed.tenant_a, "qbo")
    with tenant_session(rw_engine, seed.tenant_b) as s:
        assert s.get(Connection, a_id) is None
