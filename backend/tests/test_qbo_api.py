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
from tests.conftest import APP_ORIGIN, CSRF, Seed, make_client, new_fresh_tenant
from tests.qbo_helpers import FakeIntuit, decide_history_start, installed, state_from

ADMIN = "rotate_me"  # firm_admin with an entry row in every fresh tenant
HISTORY_START_SENTENCE = (
    "Set the history start on Configuration, Policy before connecting QuickBooks."
)


@pytest.fixture(autouse=True)
def _history_start_decided(request: pytest.FixtureRequest, rw_engine: Engine) -> None:
    """F05.3 (D-48): connecting is refused until the history start is set, so every test
    here that connects a fresh tenant does so on a company that has set it."""
    if "fresh_tenant" in request.fixturenames:
        decide_history_start(rw_engine, request.getfixturevalue("fresh_tenant"))


@pytest.fixture
def new_tenant(seed: Seed, owner_engine: Engine, rw_engine: Engine) -> Callable[[], uuid.UUID]:
    def make() -> uuid.UUID:
        marker = uuid.uuid4().hex[:8]
        with untenanted_session(owner_engine) as s:
            t = Tenant(firm_id=seed.firm_id, name=f"Qbo {marker}", slug=f"qbo-{marker}")
            s.add(t)
            s.flush()
            tid = t.id
        with tenant_session(owner_engine, tid) as s:
            s.add(Membership(tenant_id=tid, user_id=seed.users[ADMIN].id, role=None))
        decide_history_start(rw_engine, tid)
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


# --- the copy: status with what the platform holds, sync now, backfill ----------------------


def test_status_shows_counts_attention_items_and_month_totals_as_strings(
    login_as, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    from tests.qbo_fixtures import fixture
    from tests.qbo_helpers import FixtureCompany, connect_directly
    from tests.test_qbo_tasks import _drain

    admin = login_as(ADMIN, tenant=fresh_tenant)
    fake = FakeIntuit()
    served = FixtureCompany().serve(fake)
    with installed(fake):
        connect_directly(rw_engine, fresh_tenant, fake)
        body = admin.get("/api/qbo/status").json()
        assert body["held"]["backfill_needed"] is True
        assert [a["code"] for a in body["held"]["attention"]] == ["backfill_needed"]
        assert body["held"]["month_totals"] == []
        r = admin.post("/api/qbo/backfill", headers=CSRF)
        assert r.status_code == 200 and r.json()["created"] is True
        assert admin.post("/api/qbo/backfill", headers=CSRF).json()["created"] is False
        body = admin.get("/api/qbo/status").json()
        assert body["held"]["backfill"]["outcome_label"] == "Running"
        _drain(rw_engine, fresh_tenant)
        body = admin.get("/api/qbo/status").json()
    held = body["held"]
    assert held["backfill"]["outcome_label"] == "Succeeded" and held["backfill_needed"] is False
    counts = {e["entity"]: e for e in held["entities"]}
    assert (
        counts["Invoice"]["current"] == len(fixture("Invoice"))
        and counts["Invoice"]["skipped"] == 0
    )
    assert counts["Bill"]["skipped"] == 0  # F10: the cost side is normalized; none unreadable
    assert counts["TimeActivity"]["skipped"] is None  # not normalized by any feature yet
    assert [a["code"] for a in held["attention"]] == [
        "accounts_without_number",
        "numbered_not_in_chart",
        "unlinked_deposit_lines",
    ]
    # 90 accounts recorded, one inactive: 89 active, 64 of them without a number.
    assert held["attention"][0]["detail"] == "64 of 89 active accounts"
    assert held["attention"][1]["detail"].split(", ") == sorted(
        r["AcctNum"] for r in fixture("Account") if r.get("AcctNum")
    )  # no chart on this tenant: every numbered account is unmatched
    assert held["attention"][2]["detail"].startswith("2 lines, ")
    oracle = fixture("oracle_month_totals")
    assert held["month_totals"] == oracle  # strings with cents, straight from the API
    for m in held["month_totals"]:
        for column in ("invoices", "credit_memos", "sales_receipts", "payments"):
            assert isinstance(m[column], str) and "." in m[column]
    assert any(m["credit_memos"].startswith("-") for m in held["month_totals"])
    assert held["last_sync"]["kind_label"] == "Backfill"
    assert body["last_success_at"] is not None
    # Sync now: queued once, audited.
    with installed(fake):
        r = admin.post("/api/qbo/sync", headers=CSRF)
        assert r.status_code == 200 and r.json()["created"] is True
        assert admin.post("/api/qbo/sync", headers=CSRF).json()["created"] is False
    rows = [a for a, _, _ in _audit(rw_engine, fresh_tenant)]
    assert rows.count("sync_requested") == 0  # the audit helper lists connection% only
    with tenant_session(rw_engine, fresh_tenant) as db:
        n = db.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'sync_requested'")
        ).scalar_one()
    assert n == 4  # two backfill requests, two sync-now requests (created or not)
    assert served.queries  # the backfill did ask the stand-in


def test_sync_and_backfill_need_a_connected_company(login_as, fresh_tenant: uuid.UUID) -> None:
    admin = login_as(ADMIN, tenant=fresh_tenant)
    for path in ("/api/qbo/sync", "/api/qbo/backfill"):
        r = admin.post(path, headers=CSRF)
        assert r.status_code == 409 and "not connected" in r.json()["detail"]


# --- F05.3 (D-48): nothing is pulled until the history start is decided ---------------------


def test_connect_sync_and_backfill_are_refused_until_the_history_start_is_set(
    login_as, seed: Seed, owner_engine: Engine, rw_engine: Engine
) -> None:
    """With the key unset the three routes answer 409 with the one sentence and store
    nothing; the status says the key is not decided; once set, connect proceeds and the
    other two refuse only for want of a connection."""
    from datetime import date

    tenant = new_fresh_tenant(seed, owner_engine)  # no autouse: the key stays unset
    admin = login_as(ADMIN, tenant=tenant)
    for path in ("/api/qbo/connect", "/api/qbo/sync", "/api/qbo/backfill"):
        r = admin.post(path, headers=CSRF)
        assert r.status_code == 409 and r.json()["detail"] == HISTORY_START_SENTENCE, path
    assert _connection(rw_engine, tenant) is None and _audit(rw_engine, tenant) == []
    with tenant_session(rw_engine, tenant) as db:
        requested = text("SELECT count(*) FROM audit_log WHERE action = 'sync_requested'")
        assert db.execute(requested).scalar_one() == 0
    status = admin.get("/api/qbo/status").json()
    assert status["history_start"] is None and status["status"] == "disconnected"
    decide_history_start(rw_engine, tenant, date(2026, 1, 1))
    assert admin.get("/api/qbo/status").json()["history_start"] == "2026-01-01"
    with installed(FakeIntuit()):
        assert _start(admin)  # connect now proceeds to Intuit
    for path in ("/api/qbo/sync", "/api/qbo/backfill"):
        r = admin.post(path, headers=CSRF)
        assert r.status_code == 409 and "not connected" in r.json()["detail"]


def test_a_company_connected_before_the_history_start_is_told_so_and_pulls_nothing(
    login_as, seed: Seed, owner_engine: Engine, rw_engine: Engine
) -> None:
    """A company connected before F05.3 (none on production; the case is covered here):
    "Needs attention" carries the sentence, Sync now and a fresh backfill are refused
    with it, and the status carries the date once it is set."""
    from datetime import date

    from app.integrations.qbo.schedule import after_connect
    from tests.qbo_helpers import connect_directly

    tenant = new_fresh_tenant(seed, owner_engine)
    admin = login_as(ADMIN, tenant=tenant)
    fake = FakeIntuit()
    with installed(fake):
        cid = connect_directly(rw_engine, tenant, fake, history_start=None)
        body = admin.get("/api/qbo/status").json()
        assert body["status"] == "connected" and body["history_start"] is None
        items = {a["code"]: a for a in body["held"]["attention"]}
        assert items["history_start_needed"]["label"] == "History start not decided"
        assert items["history_start_needed"]["detail"] == "nothing is pulled until it is set."
        assert list(items)[0] == "history_start_needed"  # first, before "backfill needed"
        for path in ("/api/qbo/sync", "/api/qbo/backfill"):
            r = admin.post(path, headers=CSRF)
            assert r.status_code == 409 and r.json()["detail"] == HISTORY_START_SENTENCE, path
        with tenant_session(rw_engine, tenant) as db:
            assert after_connect(db, tenant, db.get(Connection, cid)) == "history_start_needed"
            backfills = text("SELECT count(*) FROM sync_run WHERE kind = 'backfill'")
            assert db.execute(backfills).scalar_one() == 0
            assert (
                db.execute(
                    text("SELECT count(*) FROM task WHERE kind LIKE 'qbo.backfill%'")
                ).scalar_one()
                == 0
            )
        decide_history_start(rw_engine, tenant, date(2026, 1, 1))
        body = admin.get("/api/qbo/status").json()
        assert body["history_start"] == "2026-01-01"
        assert "history_start_needed" not in {a["code"] for a in body["held"]["attention"]}
        assert [a["code"] for a in body["held"]["attention"]] == ["backfill_needed"]
