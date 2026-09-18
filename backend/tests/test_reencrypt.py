"""``scripts/reencrypt.py`` (F03): after a rotation, one run leaves zero rows on the
old key across ``user`` and ``connection``, everything still decrypts, a second
run changes nothing, the old key can then be dropped; one transaction per tenant;
never prints a value."""

import importlib.util
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, select, text

from app.core.config import get_settings
from app.core.crypto import Keyring, aad_for
from app.core.db import tenant_session
from app.ingest.connections import (
    get_connection_tokens,
    get_or_create_connection,
    set_connection_tokens,
)
from app.ingest.models import Connection
from app.tenancy.models import Role, Tenant
from tests._env import TEST_KEYS
from tests.conftest import Seed
from tests.leaks import record_secret

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reencrypt.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("reencrypt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def active_key(monkeypatch: pytest.MonkeyPatch, rw_engine: Engine) -> Iterator[callable]:
    """Switch the active key id for the test; afterwards restore ``test1`` and move
    every row back to it, so the rest of the suite sees the seed's key id."""

    def switch(kid: str) -> None:
        monkeypatch.setenv("CRYPTO_ACTIVE_KEY_ID", kid)
        get_settings.cache_clear()

    yield switch
    switch("test1")
    script = _load_script()
    monkeypatch.setattr(script, "create_app_engine", lambda *_a, **_k: rw_engine)
    assert script.main([]) == 0


def _key_ids(engine: Engine, seed: Seed) -> dict[str, set[str]]:
    with engine.connect() as c:
        users = set(
            c.execute(
                text('SELECT totp_key_id FROM "user" WHERE totp_secret_enc IS NOT NULL')
            ).scalars()
        )
    conns: set[str] = set()
    for tid in (seed.tenant_a, seed.tenant_b, seed.tenant_c, seed.tenant_new):
        with tenant_session(engine, tid) as s:
            for a, r in s.execute(
                select(Connection.access_token_key_id, Connection.refresh_token_key_id)
            ):
                conns |= {k for k in (a, r) if k}
    return {"user": users, "connection": conns}


def _blobs(engine: Engine, seed: Seed) -> list[tuple]:
    with engine.connect() as c:
        rows = c.execute(
            text('SELECT id, totp_secret_enc FROM "user" WHERE totp_secret_enc IS NOT NULL')
        ).all()
    for tid in (seed.tenant_a, seed.tenant_b, seed.tenant_c, seed.tenant_new):
        with tenant_session(engine, tid) as s:
            rows += s.execute(text("SELECT id, access_token_enc FROM connection")).all()
    return sorted(rows, key=lambda r: str(r[0]))


def test_reencrypt_moves_every_row_to_the_active_key_once(
    seed: Seed,
    rw_engine: Engine,
    owner_engine: Engine,
    active_key,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    script = _load_script()
    # A connection with tokens under the current key (test1) in two tenants.
    tokens: dict[uuid.UUID, tuple[str, str]] = {}
    for tid in (seed.tenant_a, seed.tenant_b):
        access, refresh = f"acc-{uuid.uuid4().hex}", f"ref-{uuid.uuid4().hex}"
        record_secret("connection_token", access)
        record_secret("connection_token", refresh)
        with tenant_session(rw_engine, tid) as s:
            conn = get_or_create_connection(s, tid, "qbo")
            set_connection_tokens(
                s,
                conn,
                access_token=access,
                refresh_token=refresh,
                token_expires_at=datetime.now(UTC),
                actor_user_id=seed.users["firm_admin"].id,
                actor_role=Role.firm_admin,
            )
            tokens[conn.id] = (access, refresh)
    assert _key_ids(owner_engine, seed) == {"user": {"test1"}, "connection": {"test1"}}

    # Rotate: test0 becomes active; test1 stays in the ring. Count transactions that
    # set a tenant context: one per tenant, never two contexts in one transaction.
    active_key("test0")
    monkeypatch.setattr(script, "create_app_engine", lambda *_a, **_k: rw_engine)
    contexts: dict[int, int] = {}
    per_tx: list[int] = []

    def on_begin(conn):
        contexts[id(conn)] = 0

    def on_exec(conn, cursor, statement, parameters, context, executemany):
        if "set_config('app.tenant_id'" in statement:
            contexts[id(conn)] = contexts.get(id(conn), 0) + 1

    def on_end(conn):
        per_tx.append(contexts.pop(id(conn), 0))

    listeners = [
        ("begin", on_begin),
        ("before_cursor_execute", on_exec),
        ("commit", on_end),
        ("rollback", on_end),
    ]
    for name, fn in listeners:
        event.listen(rw_engine, name, fn)
    try:
        assert script.main([]) == 0
    finally:
        for name, fn in listeners:
            event.remove(rw_engine, name, fn)
    with owner_engine.connect() as c:
        tenant_count = c.execute(select(text("count(*)")).select_from(Tenant)).scalar_one()
    assert max(per_tx) == 1
    assert per_tx.count(1) >= tenant_count  # at least one contextual transaction per tenant
    out = capsys.readouterr().out
    assert (
        "before user.totp_secret_enc: test1=" in out and "after user.totp_secret_enc: test0=" in out
    )
    after = [line for line in out.splitlines() if line.startswith("after ")]
    assert len(after) == 2 and all("test0=" in line and "test1" not in line for line in after)
    for access, refresh in tokens.values():
        assert access not in out and refresh not in out
    for su in seed.users.values():
        if su.totp_secret:
            assert su.totp_secret not in out

    assert _key_ids(owner_engine, seed) == {"user": {"test0"}, "connection": {"test0"}}
    # Everything still decrypts, and with the old key gone from the ring.
    ring_without_old = Keyring.parse(f"test0:{TEST_KEYS['test0']}", "test0")
    seed_secrets = {u.id: u.totp_secret for u in seed.users.values() if u.totp_secret}
    with owner_engine.connect() as c:
        for uid, enc, kid in c.execute(
            text(
                'SELECT id, totp_secret_enc, totp_key_id FROM "user" '
                "WHERE totp_secret_enc IS NOT NULL"
            )
        ):
            secret = ring_without_old.decrypt(kid, enc, aad=uid.bytes).decode()
            assert secret  # other tests enrol users of their own; the seed's must match
            if uid in seed_secrets:
                assert secret == seed_secrets[uid]
    for tid in (seed.tenant_a, seed.tenant_b):
        with tenant_session(rw_engine, tid) as s:
            conn = s.execute(
                select(Connection).where(Connection.tenant_id == tid, Connection.system == "qbo")
            ).scalar_one()
            assert get_connection_tokens(conn) == tokens[conn.id]
            assert (
                ring_without_old.decrypt(
                    conn.access_token_key_id,
                    conn.access_token_enc,
                    aad=aad_for(tid, conn.id, "access_token"),
                ).decode()
                == tokens[conn.id][0]
            )

    # A second run changes nothing (idempotent).
    before = _blobs(owner_engine, seed)
    assert script.main([]) == 0
    assert _blobs(owner_engine, seed) == before
    assert "re-encrypted: user rows 0, connection tokens 0" in capsys.readouterr().out


def test_dry_run_writes_nothing(
    seed: Seed, rw_engine: Engine, owner_engine: Engine, active_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    active_key("test0")
    monkeypatch.setattr(script, "create_app_engine", lambda *_a, **_k: rw_engine)
    before = _blobs(owner_engine, seed)
    assert script.main(["--dry-run"]) == 0
    assert _blobs(owner_engine, seed) == before
    assert "test1" in _key_ids(owner_engine, seed)["user"]
