"""F05.1: Intuit's webhook. The signature is the only proof; the raw body is stored
first and the poll is enqueued after the response, once per burst; nothing here
reaches Intuit (the refuse-all transport is in place for every test)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from app.core.config import get_settings
from app.core.db import tenant_session, untenanted_session
from app.ingest.models import Connection, WebhookEvent
from app.ingest.sync_runs import finish_sync_run, iso_utc, start_sync_run
from app.integrations.qbo.schedule import CDC_KIND
from app.integrations.qbo.webhooks import (
    SIGNATURE_HEADER,
    Event,
    parse_events,
    sign,
    signature_ok,
)
from app.worker.models import Task
from tests._env import QBO_WEBHOOK_VERIFIER
from tests.conftest import CSRF, make_client
from tests.qbo_helpers import FakeIntuit, connect_directly, installed
from tests.test_qbo_tasks import _drain

ADMIN = "rotate_me"

# --- the test vector, produced outside the app ----------------------------------------------
# printf '%s' "$BODY" | openssl dgst -sha256 -hmac 'test-verifier-token-0123' -binary | base64
# with BODY = VECTOR_BODY exactly (no trailing newline), run 2026-09-22 on macOS openssl.
# Intuit's Java sample (Configure webhooks): SecretKeySpec(verifier.getBytes("UTF-8"),
# "HmacSHA256"); Base64 of mac.doFinal(payload.getBytes()).
VECTOR_VERIFIER = "test-verifier-token-0123"
VECTOR_BODY = (
    b'[{"specversion":"1.0","id":"evt-0001","source":"intuit.qbo",'
    b'"type":"qbo.invoice.create.v1","datacontenttype":"application/json",'
    b'"time":"2026-09-22T14:00:00Z","intuitentityid":"145",'
    b'"intuitaccountid":"9130001234567890","data":{}}]'
)
VECTOR_SIGNATURE = "H6OEbK7uGPkliXuslEPXaXI47okV6VAlkZUeIrQ6+TE="


def test_signature_matches_the_openssl_vector_and_rejects_every_variation() -> None:
    assert sign(VECTOR_BODY, VECTOR_VERIFIER) == VECTOR_SIGNATURE
    assert signature_ok(VECTOR_BODY, VECTOR_SIGNATURE, VECTOR_VERIFIER)
    assert signature_ok(VECTOR_BODY, " " + VECTOR_SIGNATURE + "\n", VECTOR_VERIFIER)  # trimmed
    assert not signature_ok(VECTOR_BODY, None, VECTOR_VERIFIER)
    assert not signature_ok(VECTOR_BODY, "", VECTOR_VERIFIER)
    assert not signature_ok(VECTOR_BODY, VECTOR_SIGNATURE, "test-verifier-token-0124")
    assert not signature_ok(VECTOR_BODY + b" ", VECTOR_SIGNATURE, VECTOR_VERIFIER)
    assert not signature_ok(VECTOR_BODY, VECTOR_SIGNATURE[:-2] + "==", VECTOR_VERIFIER)
    assert not signature_ok(VECTOR_BODY, "not base64!", VECTOR_VERIFIER)
    assert not signature_ok(VECTOR_BODY, VECTOR_SIGNATURE, "")


# --- payload shapes ---------------------------------------------------------------------------


def cloud_events(realm: str, n: int = 1, *, entity: str = "invoice", ids=None) -> list[dict]:
    return [
        {
            "specversion": "1.0",
            "id": (ids[i] if ids else f"evt-{uuid.uuid4().hex}"),
            "source": "intuit.qbo",
            "type": f"qbo.{entity}.update.v1",
            "datacontenttype": "application/json",
            "time": "2026-09-22T14:00:00Z",
            "intuitentityid": str(100 + i),
            "intuitaccountid": realm,
            "data": {},
        }
        for i in range(n)
    ]


def legacy(realm: str, n: int = 2) -> dict:
    return {
        "eventNotifications": [
            {
                "realmId": realm,
                "dataChangeEvent": {
                    "entities": [
                        {"name": "Invoice", "id": str(i), "operation": "Update"} for i in range(n)
                    ]
                },
            }
        ]
    }


def test_parse_reads_cloud_events_then_legacy_then_stores_anything_else() -> None:
    events = parse_events(cloud_events("91", 2, ids=["a", "b"]))
    assert [
        (e.event_id, e.realm_id, e.event_type, e.entity_id, e.entity_count) for e in events
    ] == [
        ("a", "91", "qbo.invoice.update.v1", "100", 1),
        ("b", "91", "qbo.invoice.update.v1", "101", 1),
    ]
    (one,) = parse_events(legacy("92", 3))
    assert (one.event_id, one.realm_id, one.event_type, one.entity_count) == (
        None,
        "92",
        "legacy",
        3,
    )
    for body in ({"hello": "world"}, [], [1, 2], "text", {"eventNotifications": []}):
        (unknown,) = parse_events(body)
        assert unknown == Event(
            None, None, None, None, 0, body if isinstance(body, dict | list) else {}
        )


# --- the route ------------------------------------------------------------------------------


def post(client: TestClient, body: bytes, *, signature: str | None = "good", **headers: str):
    h = dict(headers)
    if signature == "good":
        h[SIGNATURE_HEADER] = sign(body, QBO_WEBHOOK_VERIFIER)
    elif signature is not None:
        h[SIGNATURE_HEADER] = signature
    return client.post("/api/qbo/webhook", content=body, headers=h)


def rows_for(engine: Engine, realm: str) -> list[WebhookEvent]:
    with untenanted_session(engine) as db:
        rows = list(
            db.execute(
                select(WebhookEvent)
                .where(WebhookEvent.realm_id == realm)
                .order_by(WebhookEvent.received_at)
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


def all_rows(engine: Engine) -> int:
    with untenanted_session(engine) as db:
        return db.execute(text("SELECT count(*) FROM webhook_event")).scalar_one()


def open_polls(engine: Engine, tenant_id: uuid.UUID) -> list[Task]:
    with tenant_session(engine, tenant_id) as db:
        rows = list(
            db.execute(
                select(Task).where(Task.kind == CDC_KIND, Task.status.in_(("queued", "running")))
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


def test_bad_or_missing_signature_and_a_modified_body_are_401_and_store_nothing(
    client: TestClient, rw_engine: Engine
) -> None:
    realm = "9130" + uuid.uuid4().hex[:12]
    body = json.dumps(cloud_events(realm)).encode()
    before = all_rows(rw_engine)
    assert post(client, body, signature=None).status_code == 401
    assert post(client, body, signature="").status_code == 401
    assert post(client, body, signature=sign(body, "another-verifier")).status_code == 401
    assert post(client, body, signature="not base64!").status_code == 401
    good = sign(body, QBO_WEBHOOK_VERIFIER)
    assert post(client, body + b"\n", signature=good).status_code == 401  # body modified
    assert all_rows(rw_engine) == before and rows_for(rw_engine, realm) == []
    # No CSRF header, no Origin, no cookie: the signature alone admits the delivery.
    assert post(client, body).status_code == 200
    assert len(rows_for(rw_engine, realm)) == 1


def test_five_deliveries_in_a_second_store_five_rows_and_enqueue_one_poll(
    client: TestClient, fresh_tenant: uuid.UUID, rw_engine: Engine, login_as
) -> None:
    fake = FakeIntuit()
    with installed(fake):
        cid = connect_directly(rw_engine, fresh_tenant, fake)
    # Every call below runs with the refuse-all transport: the endpoint and its
    # dispatch never reach Intuit (a call would raise inside the request).
    before = datetime.now(UTC)
    for _ in range(5):
        r = post(
            client,
            json.dumps(cloud_events(fake.realm_id)).encode(),
            **{"intuit-t-id": "tid-abc", "intuit-notification-schema-version": "1.0"},
        )
        assert r.status_code == 200, r.text
    rows = rows_for(rw_engine, fake.realm_id)
    assert len(rows) == 5
    assert {(r.intuit_tid, r.schema_version, r.entity_count) for r in rows} == {
        ("tid-abc", "1.0", 1)
    }
    polls = open_polls(rw_engine, fresh_tenant)
    assert [(t.dedupe_key, t.payload["connection_id"]) for t in polls] == [
        (f"cdc:{cid}:now", str(cid))
    ]
    with tenant_session(rw_engine, fresh_tenant) as db:
        c = db.get(Connection, cid)
        assert c.last_webhook_at is not None and c.last_webhook_at >= before
    # Sync now coalesces with it: the same dedupe key.
    admin = login_as(ADMIN, tenant=fresh_tenant)
    r = admin.post("/api/qbo/sync", headers=CSRF)
    assert r.status_code == 200 and r.json()["created"] is False
    status = admin.get("/api/qbo/status").json()
    assert status["webhooks_24h"] == 5 and status["last_webhook_at"] is not None


def test_a_retried_delivery_stores_once_and_other_shapes_store_without_a_task(
    client: TestClient, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    fake = FakeIntuit()
    with installed(fake):
        connect_directly(rw_engine, fresh_tenant, fake)
    ids = [f"evt-{uuid.uuid4().hex}" for _ in range(2)]
    body = json.dumps(cloud_events(fake.realm_id, 2, ids=ids)).encode()
    assert post(client, body).status_code == 200
    assert post(client, body).status_code == 200  # Intuit's retry: same ids
    assert [r.event_id for r in rows_for(rw_engine, fake.realm_id)] == ids
    # Legacy shape: one row per notification, no event id, the entity count.
    other = "9130" + uuid.uuid4().hex[:12]
    assert post(client, json.dumps(legacy(other, 3)).encode()).status_code == 200
    (row,) = rows_for(rw_engine, other)
    assert (row.event_id, row.event_type, row.entity_count) == (None, "legacy", 3)
    # A body matching neither, and one that is not JSON: stored, 200, count 0.
    before = all_rows(rw_engine)
    assert post(client, b'{"hello":"world"}').status_code == 200
    assert post(client, b"not json at all").status_code == 200
    assert all_rows(rw_engine) == before + 2
    with untenanted_session(rw_engine) as db:
        latest = db.execute(
            select(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(2)
        ).scalars()
        assert {(r.realm_id, r.entity_count) for r in latest} == {(None, 0)}
        unread = db.execute(
            select(WebhookEvent.payload).where(
                WebhookEvent.payload["unreadable_body"].astext == "not json at all"
            )
        ).scalar_one_or_none()
        assert unread is not None
    # The unknown realm and the connected one: only the connected one has a task.
    assert open_polls(rw_engine, fresh_tenant) != []


def test_an_unknown_realm_and_a_connection_needing_reconnect_enqueue_nothing(
    client: TestClient, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    unknown = "9130" + uuid.uuid4().hex[:12]
    assert post(client, json.dumps(cloud_events(unknown)).encode()).status_code == 200
    assert len(rows_for(rw_engine, unknown)) == 1
    fake = FakeIntuit()
    with installed(fake):
        cid = connect_directly(rw_engine, fresh_tenant, fake)
    with tenant_session(rw_engine, fresh_tenant) as db:
        c = db.get(Connection, cid)
        c.status = "needs_reconnect"
        c.last_error = "invalid_grant"
    assert post(client, json.dumps(cloud_events(fake.realm_id)).encode()).status_code == 200
    assert open_polls(rw_engine, fresh_tenant) == []
    with tenant_session(rw_engine, fresh_tenant) as db:
        assert db.get(Connection, cid).last_webhook_at is not None  # it did arrive


def test_bad_signatures_are_throttled_per_ip_and_a_good_one_never_is(
    rw_engine: Engine,
) -> None:
    threshold = get_settings().ip_throttle_failures
    body = json.dumps(cloud_events("9130" + uuid.uuid4().hex[:12])).encode()
    with make_client() as c:
        for _ in range(threshold):
            assert post(c, body, signature="AAAA").status_code == 401
        assert post(c, body, signature="AAAA").status_code == 429
        assert post(c, body).status_code == 200  # a signed delivery is never refused
    with make_client() as other:  # another address has its own window
        assert post(other, body, signature="AAAA").status_code == 401


def test_without_a_verifier_the_route_answers_503_and_stores_nothing(
    client: TestClient, rw_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps(cloud_events("9130" + uuid.uuid4().hex[:12])).encode()
    before = all_rows(rw_engine)
    monkeypatch.setattr(get_settings(), "qbo_webhook_verifier", None)
    assert post(client, body).status_code == 503
    assert all_rows(rw_engine) == before


# --- the Intuit-side disconnect page --------------------------------------------------------


def _seed_cursor(engine: Engine, tenant_id: uuid.UUID, cid: uuid.UUID, realm: str) -> None:
    with tenant_session(engine, tenant_id) as db:
        run = start_sync_run(db, tenant_id, cid, "backfill")
        finish_sync_run(
            db,
            run,
            outcome="succeeded",
            cursor={
                "realm_id": realm,
                "changed_since": iso_utc(datetime.now(UTC) - timedelta(minutes=5)),
            },
        )


def test_disconnect_page_enqueues_one_poll_whose_first_call_sets_needs_reconnect(
    client: TestClient, fresh_tenant: uuid.UUID, rw_engine: Engine
) -> None:
    fake = FakeIntuit()
    with installed(fake):
        cid = connect_directly(rw_engine, fresh_tenant, fake)
    _seed_cursor(rw_engine, fresh_tenant, cid, fake.realm_id)
    target = f"{get_settings().app_base_url.rstrip('/')}/qbo/disconnected"
    # Without a realm, or with an unknown one: the page, nothing else.
    r = client.get("/api/qbo/disconnected", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, target)
    r = client.get("/api/qbo/disconnected", params={"realmId": "0"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, target)
    assert open_polls(rw_engine, fresh_tenant) == []
    # The company's realm: one poll, nothing marked yet, no webhook counted.
    r = client.get(
        "/api/qbo/disconnected", params={"realmId": fake.realm_id}, follow_redirects=False
    )
    assert (r.status_code, r.headers["location"]) == (303, target)
    assert "set-cookie" not in r.headers
    (poll,) = open_polls(rw_engine, fresh_tenant)
    assert poll.dedupe_key == f"cdc:{cid}:now"
    with tenant_session(rw_engine, fresh_tenant) as db:
        c = db.get(Connection, cid)
        assert c.status == "connected" and c.last_webhook_at is None
    # The user revoked the app inside QuickBooks: the token is dead, the refresh refused.
    fake.access_tokens.clear()
    fake.refuse_refresh = True
    with installed(fake):
        _drain(rw_engine, fresh_tenant)
    with tenant_session(rw_engine, fresh_tenant) as db:
        c = db.get(Connection, cid)
        assert (c.status, c.last_error) == ("needs_reconnect", "invalid_grant")
        assert c.access_token_enc is None
    assert fake.refresh_calls == 1
