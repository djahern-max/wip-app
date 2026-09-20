"""F05: connect, callback, disconnect, status. The callback takes the tenant and the
user from the pending ``state``, never from the session (owner answer 1)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.db import tenant_session, untenanted_session
from app.ingest.connections import get_connection_tokens
from app.ingest.models import Connection
from app.integrations.qbo.oauth import state_sha256
from app.tenancy.models import Membership, Tenant
from tests.conftest import APP_ORIGIN, CSRF, Seed, make_client
from tests.qbo_helpers import FakeIntuit, installed, state_from

ADMIN = "rotate_me"  # firm_admin with an entry row in every fresh tenant


@pytest.fixture
def new_tenant(seed: Seed, owner_engine: Engine) -> Callable[[], uuid.UUID]:
    def make() -> uuid.UUID:
        marker = uuid.uuid4().hex[:8]
        with untenanted_session(owner_engine) as s:
            t = Tenant(firm_id=seed.firm_id, name=f"Qbo {marker}", slug=f"qbo-{marker}")
            s.add(t)
            s.flush()
            tid = t.id
        with tenant_session(owner_engine, tid) as s:
            s.add(Membership(tenant_id=tid, user_id=seed.users[ADMIN].id, role=None))
        return tid

    return make


def _start(client: TestClient) -> str:
    r = client.post("/api/qbo/connect", headers=CSRF)
    assert r.status_code == 200, r.text
    return state_from(r.json()["authorization_url"])


def _callback(client: TestClient, **params: str) -> str:
    r = client.get("/api/qbo/callback", params=params, follow_redirects=False)
    assert r.status_code == 303
    target = urlparse(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{APP_ORIGIN}/connections"
    assert "set-cookie" not in r.headers
    return parse_qs(target.query)["result"][0]


def _connection(engine: Engine, tenant_id: uuid.UUID) -> Connection | None:
    with tenant_session(engine, tenant_id) as db:
        row = db.execute(
            text("SELECT id FROM connection WHERE system = 'qbo'")
        ).scalar_one_or_none()
        if row is None:
            return None
        c = db.get(Connection, row)
        db.expunge(c)
        return c


def _audit(engine: Engine, tenant_id: uuid.UUID) -> list[tuple[str, str | None, str]]:
    with tenant_session(engine, tenant_id) as db:
        return [
            (a, None if u is None else str(u), d or "")
            for a, u, d in db.execute(
                text(
                    "SELECT action, actor_user_id, detail::text FROM audit_log "
                    "WHERE action LIKE 'connection%' ORDER BY occurred_at, action"
                )
            ).all()
        ]


def test_connect_stores_only_the_hash_of_a_state_bound_to_tenant_and_user(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine, seed: Seed
) -> None:
    client = login_as(ADMIN, tenant=fresh_tenant)
    r = client.post("/api/qbo/connect", headers=CSRF)
    url = r.json()["authorization_url"]
    assert set(r.json()) == {"authorization_url"}
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["com.intuit.quickbooks.accounting"]
    state = state_from(url)
    c = _connection(rw_engine, fresh_tenant)
    assert c.oauth_state_sha256 == state_sha256(state) != state
    assert c.oauth_state_user_id == seed.users[ADMIN].id
    assert (
        timedelta(minutes=9) < c.oauth_state_expires_at - datetime.now(UTC) <= timedelta(minutes=10)
    )
    assert c.status == "disconnected" and c.access_token_enc is None
    actions = _audit(rw_engine, fresh_tenant)
    assert [a for a, _, _ in actions] == ["connection_started"]
    assert state not in actions[0][2] and state_sha256(state) not in actions[0][2]


def test_callback_without_a_session_cookie_completes_from_the_state_alone(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine, seed: Seed
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    with installed(FakeIntuit(company_name="Sample Landscaping")) as fake, make_client() as anon:
        state = _start(admin)
        assert _callback(anon, state=state, code=fake.new_code(), realmId=fake.realm_id) == (
            "connected"
        )
        assert anon.cookies.get("sid") is None
    c = _connection(rw_engine, fresh_tenant)
    assert (c.status, c.realm_id, c.environment) == ("connected", fake.realm_id, "sandbox")
    assert c.company_name == "Sample Landscaping"
    assert c.oauth_state_sha256 is None and c.oauth_state_user_id is None
    assert c.access_token_key_id and c.refresh_token_key_id
    access, refresh = get_connection_tokens(c)
    assert access in fake.access_tokens and refresh in fake.refresh_tokens
    assert access.encode() not in c.access_token_enc  # ciphertext, not the token
    assert c.token_expires_at > datetime.now(UTC) + timedelta(minutes=55)
    assert c.refresh_token_expires_at > datetime.now(UTC) + timedelta(days=99)
    rows = _audit(rw_engine, fresh_tenant)
    assert sorted(a for a, _, _ in rows) == [
        "connection_completed",
        "connection_started",
        "connection_tokens_set",
    ]
    # The actor of the callback's rows is the user who started the flow.
    assert {u for _, u, _ in rows} == {str(seed.users[ADMIN].id)}
    blob = "\n".join(d for _, _, d in rows)
    for secret in (state, access, refresh):
        assert secret not in blob
    # Status, as the page reads it.
    status = admin.get("/api/qbo/status", params={"result": "connected"}).json()
    assert status["status_label"] == "Connected" and status["company_name"] == "Sample Landscaping"
    assert status["result_message"] == "QuickBooks is connected." and status["can_manage"] is True
    assert "token" not in " ".join(status).lower() and "state" not in set(status)


def test_a_state_works_once(login_as, fresh_tenant: uuid.UUID, rw_engine: Engine) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    with installed(FakeIntuit()) as fake:
        state = _start(admin)
        assert _callback(admin, state=state, code=fake.new_code(), realmId=fake.realm_id) == (
            "connected"
        )
        before = _connection(rw_engine, fresh_tenant)
        calls = len(fake.requests)
        assert _callback(admin, state=state, code=fake.new_code(), realmId=fake.realm_id) == (
            "state_invalid"
        )
        assert len(fake.requests) == calls  # a replay never reaches Intuit
    after = _connection(rw_engine, fresh_tenant)
    assert after.access_token_enc == before.access_token_enc
    assert after.updated_at == before.updated_at


def test_expired_unknown_and_foreign_states_store_nothing(
    login_as, new_tenant, rw_engine: Engine, owner_engine: Engine
) -> None:
    mine, other = new_tenant(), new_tenant()
    admin = login_as(ADMIN, tenant=mine)
    with installed(FakeIntuit()) as fake:
        # expired
        state = _start(admin)
        with tenant_session(owner_engine, mine) as db:
            db.execute(text("UPDATE connection SET oauth_state_expires_at = now() - interval '1s'"))
        assert _callback(admin, state=state, code=fake.new_code(), realmId=fake.realm_id) == (
            "state_invalid"
        )
        # unknown, malformed, missing
        from app.integrations.qbo.oauth import new_state

        for bogus in (new_state(mine), new_state(uuid.uuid4()), "garbage", ""):
            assert _callback(admin, state=bogus, code=fake.new_code(), realmId=fake.realm_id) == (
                "state_invalid"
            )
        # foreign: a live state of tenant A, with the tenant bytes swapped for tenant B's
        import base64

        state = _start(admin)
        raw = base64.urlsafe_b64decode(state)
        forged = base64.urlsafe_b64encode(other.bytes + raw[16:]).decode()
        assert _callback(admin, state=forged, code=fake.new_code(), realmId=fake.realm_id) == (
            "state_invalid"
        )
        assert [p for _, p in fake.requests] == []  # none of them reached Intuit
    assert _connection(rw_engine, other) is None
    c = _connection(rw_engine, mine)
    assert c.status == "disconnected" and c.access_token_enc is None and c.realm_id is None
    assert c.oauth_state_sha256 == state_sha256(state)  # the live one is still pending


def test_a_cookie_of_a_different_user_is_refused(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    someone_else = login_as("client_admin")
    with installed(FakeIntuit()) as fake:
        state = _start(admin)
        assert _callback(
            someone_else, state=state, code=fake.new_code(), realmId=fake.realm_id
        ) == ("wrong_user")
        assert fake.requests == []
    assert _connection(rw_engine, fresh_tenant).status == "disconnected"


def test_the_starting_user_must_still_be_firm_admin_at_the_callback(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine, owner_engine: Engine, seed: Seed
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    with installed(FakeIntuit()) as fake, make_client() as anon:
        state = _start(admin)
        with tenant_session(owner_engine, fresh_tenant) as db:
            db.execute(
                text("DELETE FROM membership WHERE user_id = :u"), {"u": seed.users[ADMIN].id}
            )
        assert _callback(anon, state=state, code=fake.new_code(), realmId=fake.realm_id) == (
            "not_allowed"
        )
        assert fake.requests == []
    assert _connection(rw_engine, fresh_tenant).status == "disconnected"


def test_cancelling_at_intuit_uses_up_the_state_and_changes_nothing(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    state = _start(admin)
    assert _callback(admin, state=state, error="access_denied") == "cancelled"
    c = _connection(rw_engine, fresh_tenant)
    assert c.status == "disconnected" and c.oauth_state_sha256 is None


def test_a_used_or_made_up_code_is_refused_by_intuit_and_stores_nothing(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    with installed(FakeIntuit()) as fake:
        state = _start(admin)
        assert _callback(admin, state=state, code="made-up", realmId=fake.realm_id) == (
            "code_refused"
        )
    assert _connection(rw_engine, fresh_tenant).access_token_enc is None


def test_the_realm_on_the_redirect_is_checked_against_the_token(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    with installed(FakeIntuit()) as fake:
        state = _start(admin)
        assert _callback(admin, state=state, code=fake.new_code(), realmId="1234567890") == (
            "company_unverified"
        )
        assert len(fake.revoked) == 1  # tokens for a company we will not keep
    c = _connection(rw_engine, fresh_tenant)
    assert c.realm_id is None and c.access_token_enc is None


def test_a_company_connected_to_one_tenant_is_refused_for_another(
    login_as, new_tenant, rw_engine: Engine
) -> None:
    first, second = new_tenant(), new_tenant()
    with installed(FakeIntuit()) as fake:
        a = login_as(ADMIN, tenant=first)
        assert _callback(a, state=_start(a), code=fake.new_code(), realmId=fake.realm_id) == (
            "connected"
        )
        b = login_as(ADMIN, tenant=second)
        assert _callback(b, state=_start(b), code=fake.new_code(), realmId=fake.realm_id) == (
            "company_in_use"
        )
        assert len(fake.revoked) == 1
        message = b.get("/api/qbo/status", params={"result": "company_in_use"}).json()
    assert "another client" in message["result_message"]
    assert str(first) not in str(message)  # names no tenant
    c = _connection(rw_engine, second)
    assert c.realm_id is None and c.access_token_enc is None and c.status == "disconnected"
    assert _connection(rw_engine, first).status == "connected"


def test_a_different_company_is_refused_on_reconnect_and_allowed_after_disconnect_with_no_books(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    with installed(FakeIntuit()) as one:
        assert _callback(admin, state=_start(admin), code=one.new_code(), realmId=one.realm_id) == (
            "connected"
        )
    with installed(FakeIntuit()) as two:
        assert _callback(admin, state=_start(admin), code=two.new_code(), realmId=two.realm_id) == (
            "different_company"
        )
        assert two.requests == []
        assert _connection(rw_engine, fresh_tenant).realm_id == one.realm_id
    with installed(one):
        r = admin.post("/api/qbo/disconnect", headers=CSRF)
        assert r.status_code == 200 and r.json()["status_label"] == "Not connected"
    with installed(two):
        assert _callback(admin, state=_start(admin), code=two.new_code(), realmId=two.realm_id) == (
            "connected"
        )
    c = _connection(rw_engine, fresh_tenant)
    assert c.realm_id == two.realm_id
    assert "company_changed" not in "".join(d for _, _, d in _audit(rw_engine, fresh_tenant))


def test_once_books_are_held_a_different_company_is_refused_even_after_disconnect(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    from app.ingest.raw import RawOrigin, store_raw
    from app.ingest.sync_runs import start_sync_run

    admin = login_as(ADMIN, tenant=fresh_tenant)
    with installed(FakeIntuit()) as one:
        assert _callback(admin, state=_start(admin), code=one.new_code(), realmId=one.realm_id) == (
            "connected"
        )
        with tenant_session(rw_engine, fresh_tenant) as db:
            run = start_sync_run(db, fresh_tenant, _connection(rw_engine, fresh_tenant).id, "cdc")
            store_raw(
                db,
                fresh_tenant,
                "qbo",
                "Invoice",
                "12",
                {"Id": "12"},
                RawOrigin(sync_run_id=run.id),
            )
        assert admin.post("/api/qbo/disconnect", headers=CSRF).status_code == 200
    with installed(FakeIntuit()) as two:
        assert _callback(admin, state=_start(admin), code=two.new_code(), realmId=two.realm_id) == (
            "books_held"
        )
        assert two.requests == []  # refused before anything is asked of Intuit
        message = admin.get("/api/qbo/status", params={"result": "books_held"}).json()
    assert message["result_message"] == (
        "This company's books are already held for a different QuickBooks company; "
        "a new QuickBooks company needs a new tenant."
    )
    c = _connection(rw_engine, fresh_tenant)
    assert (c.status, c.realm_id, c.access_token_enc) == ("disconnected", one.realm_id, None)
    # The same company again is a reconnect.
    with installed(one):
        assert _callback(admin, state=_start(admin), code=one.new_code(), realmId=one.realm_id) == (
            "connected"
        )
    completed = [d for a, _, d in _audit(rw_engine, fresh_tenant) if a == "connection_completed"]
    assert sorted('"reconnect": true' in d for d in completed) == [False, True]
    assert _connection(rw_engine, fresh_tenant).realm_id == one.realm_id


def test_disconnect_revokes_clears_tokens_audits_and_keeps_the_company_link(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    with installed(FakeIntuit()) as fake:
        assert _callback(
            admin, state=_start(admin), code=fake.new_code(), realmId=fake.realm_id
        ) == ("connected")
        _, refresh = get_connection_tokens(_connection(rw_engine, fresh_tenant))
        r = admin.post("/api/qbo/disconnect", headers=CSRF)
        assert r.status_code == 200, r.text
        assert fake.revoked == [refresh]
        # Reconnecting the same company is a reconnect.
        assert _callback(
            admin, state=_start(admin), code=fake.new_code(), realmId=fake.realm_id
        ) == ("connected")
    rows = _audit(rw_engine, fresh_tenant)
    assert [a for a, _, _ in rows].count("connection_disconnected") == 1
    completed = [d for a, _, d in rows if a == "connection_completed"]
    assert sorted('"reconnect": true' in d for d in completed) == [False, True]
    c = _connection(rw_engine, fresh_tenant)
    assert c.status == "connected" and c.realm_id == fake.realm_id


def test_disconnect_with_nothing_connected_is_quiet(login_as, fresh_tenant: uuid.UUID) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    r = admin.post("/api/qbo/disconnect", headers=CSRF)
    assert r.status_code == 200 and r.json()["status"] == "disconnected"
    assert admin.get("/api/qbo/status").json()["connected_company"] is False


def test_status_never_creates_a_row_and_hides_manage_from_other_roles(
    login_as, rw_engine: Engine, seed: Seed
) -> None:
    viewer = login_as("client_admin")
    body = viewer.get("/api/qbo/status").json()
    assert body["can_manage"] is False and body["status_label"] in (
        "Not connected",
        "Connected",
        "Needs reconnect",
    )
    assert viewer.post("/api/qbo/connect", headers=CSRF).status_code == 403
    assert viewer.post("/api/qbo/disconnect", headers=CSRF).status_code == 403


def test_connect_says_so_when_quickbooks_is_not_configured(
    login_as, fresh_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    admin = login_as(ADMIN, tenant=fresh_tenant)
    monkeypatch.setattr(get_settings(), "qbo_client_secret", None)
    r = admin.post("/api/qbo/connect", headers=CSRF)
    assert r.status_code == 503 and "not set up" in r.json()["detail"]
    assert _callback(admin, state="x", code="y", realmId="z") == "not_configured"
