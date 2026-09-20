"""F05: refresh under a row lock, needs-reconnect, and the single retry on a 401."""

import threading
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import Engine, select, text

from app.audit.models import AuditLog
from app.core.db import tenant_session
from app.ingest.connections import get_connection_tokens
from app.ingest.models import Connection
from app.integrations.qbo.reader import CompanyReader
from app.integrations.qbo.tokens import NeedsReconnect, access_for
from tests.qbo_helpers import FakeIntuit, connect_directly, installed


def _row(engine: Engine, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> dict:
    with tenant_session(engine, tenant_id) as db:
        c = db.get(Connection, connection_id)
        access, refresh = (None, None)
        if c.access_token_enc is not None:
            access, refresh = get_connection_tokens(c)
        return {
            "status": c.status,
            "access": access,
            "refresh": refresh,
            "expires": c.token_expires_at,
            "refresh_expires": c.refresh_token_expires_at,
            "refreshed_at": c.tokens_refreshed_at,
            "last_error": c.last_error,
        }


def _audit_actions(engine: Engine, tenant_id: uuid.UUID) -> list[str]:
    with tenant_session(engine, tenant_id) as db:
        return list(db.execute(select(AuditLog.action).order_by(AuditLog.occurred_at)).scalars())


def test_a_token_with_time_left_is_used_without_calling_intuit(
    fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with installed(FakeIntuit()) as fake:
        cid = connect_directly(rw_engine, fresh_tenant, fake)
        before = _row(rw_engine, fresh_tenant, cid)
        access = access_for(rw_engine, fresh_tenant, cid)
        assert access.access_token == before["access"] and fake.refresh_calls == 0
        assert access.realm_id == fake.realm_id and access.environment == "sandbox"


def test_refresh_replaces_both_tokens_and_both_expiries_from_the_response(
    fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with installed(FakeIntuit()) as fake:
        fake.expires_in = 1234
        cid = connect_directly(rw_engine, fresh_tenant, fake, access_valid_for=60)
        before = _row(rw_engine, fresh_tenant, cid)
        started = datetime.now(UTC)
        access = access_for(rw_engine, fresh_tenant, cid)
        after = _row(rw_engine, fresh_tenant, cid)
    assert fake.refresh_calls == 1
    assert after["access"] == access.access_token != before["access"]
    assert after["refresh"] != before["refresh"] and after["refresh"] in fake.refresh_tokens
    assert 1200 < (after["expires"] - started).total_seconds() < 1300  # read, not assumed
    assert after["refresh_expires"] > before["refresh_expires"]
    assert after["refreshed_at"] is not None and after["status"] == "connected"
    # Owner answer 8: a refresh writes no audit row.
    assert _audit_actions(rw_engine, fresh_tenant).count("connection_tokens_set") == 1


def test_two_workers_refreshing_at_once_leave_the_newest_pair_and_call_intuit_once(
    fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    """The second worker blocks on the row lock while the first is inside Intuit's
    token endpoint, then finds the new pair and makes no call of its own."""
    fake = FakeIntuit()
    inside, release = threading.Event(), threading.Event()

    def hold() -> None:
        inside.set()
        assert release.wait(10)

    fake.before_refresh = hold
    results: dict[str, object] = {}

    def worker(name: str) -> None:
        try:
            results[name] = access_for(rw_engine, fresh_tenant, cid).access_token
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertions below
            results[name] = exc

    with installed(fake):
        cid = connect_directly(rw_engine, fresh_tenant, fake, access_valid_for=10)
        first = threading.Thread(target=worker, args=("first",))
        first.start()
        assert inside.wait(10)
        second = threading.Thread(target=worker, args=("second",))
        second.start()
        second.join(0.5)
        assert second.is_alive(), "the second refresh must wait for the row lock"
        release.set()
        first.join(10)
        second.join(10)
        stored = _row(rw_engine, fresh_tenant, cid)
    assert fake.refresh_calls == 1
    assert results["first"] == results["second"] == stored["access"]
    assert stored["refresh"] in fake.refresh_tokens  # the pair Intuit still honours


def test_invalid_grant_sets_needs_reconnect_audits_and_clears_tokens(
    fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with installed(FakeIntuit()) as fake:
        cid = connect_directly(rw_engine, fresh_tenant, fake, access_valid_for=10)
        fake.refuse_refresh = True
        with pytest.raises(NeedsReconnect):
            access_for(rw_engine, fresh_tenant, cid)
        calls = fake.refresh_calls
        with pytest.raises(NeedsReconnect):  # and it does not ask Intuit again
            access_for(rw_engine, fresh_tenant, cid)
        assert fake.refresh_calls == calls == 1
    after = _row(rw_engine, fresh_tenant, cid)
    assert after["status"] == "needs_reconnect" and after["last_error"] == "invalid_grant"
    assert after["access"] is None and after["refresh"] is None
    assert _audit_actions(rw_engine, fresh_tenant).count("connection_needs_reconnect") == 1
    with tenant_session(rw_engine, fresh_tenant) as db:
        detail = db.execute(
            text("SELECT detail::text FROM audit_log WHERE action = 'connection_needs_reconnect'")
        ).scalar_one()
    assert "invalid_grant" in detail and "refresh-" not in detail and "access-" not in detail


def test_a_401_causes_exactly_one_refresh_and_one_retry(
    fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    with installed(FakeIntuit()) as fake:
        cid = connect_directly(rw_engine, fresh_tenant, fake)
        stale = _row(rw_engine, fresh_tenant, cid)["access"]
        fake.access_tokens.discard(stale)  # Intuit no longer accepts it, whatever the expiry says
        info = CompanyReader(rw_engine, fresh_tenant, cid).company_info()
        assert info["CompanyName"] == fake.company_name
        assert fake.refresh_calls == 1
        api_calls = [p for m, p in fake.requests if p.startswith("/v3/")]
        assert len(api_calls) == 2  # the 401 and the one retry


def test_a_second_401_is_an_error_not_a_loop(fresh_tenant: uuid.UUID, rw_engine: Engine) -> None:
    from app.integrations.qbo import client as qbo_client

    with installed(FakeIntuit()) as fake:
        cid = connect_directly(rw_engine, fresh_tenant, fake)
        fake.on_api(f"companyinfo/{fake.realm_id}", lambda request: httpx.Response(401, json={}))
        with pytest.raises(qbo_client.Unauthorized):
            CompanyReader(rw_engine, fresh_tenant, cid).company_info()
        assert fake.refresh_calls == 1
        assert _row(rw_engine, fresh_tenant, cid)["status"] == "connected"
