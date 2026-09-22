"""Webhook deliveries from Intuit (F05.1; Intuit "Configure webhooks" and "Best
practices", 2026-09-22). The payload is a trigger, never data: a delivery is stored
raw and answered, and the consumer is F05's change poll.

- ``signature_ok``: ``intuit-signature`` = base64(HMAC-SHA256(key = verifier token as
  UTF-8, message = the raw body bytes)). Compared on the decoded bytes in constant
  time; a header that is not base64 is a bad signature.
- ``parse_events``: the CloudEvents shape (a JSON array of events, ``intuitaccountid``
  the realm), the legacy ``eventNotifications`` shape as a fallback, or "neither"
  (one row, no realm, ``entity_count`` 0).
- ``store_delivery``: one ``webhook_event`` row per event, in one untenanted
  transaction; ``ON CONFLICT DO NOTHING`` on the event id, so a retried delivery
  stores once. **The only synchronous step of the request.**
- ``dispatch``: after the response, per realm: find the tenant that holds it (one
  ``tenant_session`` per tenant, under forced RLS there is no other way), set
  ``last_webhook_at``, and for a connected company enqueue the poll under the same
  dedupe key as Sync now (``cdc:{id}:now``), so a burst is one poll and the race is
  settled by the partial unique index on ``task``.

Nothing here calls Intuit. Log lines carry counts, realm ids and Intuit's transaction
id; never a payload or the verifier.
"""

import base64
import binascii
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.db import tenant_session, untenanted_session
from app.core.jsoncodec import json_loads
from app.ingest.models import Connection, WebhookEvent
from app.integrations.qbo import SYSTEM
from app.integrations.qbo.schedule import enqueue_cdc_poll
from app.tenancy.models import Tenant

log = logging.getLogger("app.qbo")

SIGNATURE_HEADER = "intuit-signature"
TID_HEADER = "intuit-t-id"
SCHEMA_HEADER = "intuit-notification-schema-version"
COUNT_WINDOW = timedelta(hours=24)
_MAX_TEXT = 4000  # of an unreadable body, kept for a support case


# --- signature -------------------------------------------------------------------------


def sign(body: bytes, verifier: str) -> str:
    """What Intuit sends for ``body``: base64 of the HMAC-SHA256 keyed with the verifier."""
    digest = hmac.new(verifier.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def signature_ok(body: bytes, header: str | None, verifier: str) -> bool:
    if not header or not verifier:
        return False
    try:
        given = base64.b64decode(header.strip().encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False
    expected = hmac.new(verifier.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(given, expected)


# --- payload ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    event_id: str | None
    realm_id: str | None
    event_type: str | None
    entity_id: str | None
    entity_count: int
    payload: dict | list


def _text(value: Any, limit: int = 120) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _cloud_event(item: dict) -> Event:
    return Event(
        event_id=_text(item.get("id")),
        realm_id=_text(item.get("intuitaccountid"), 40),
        event_type=_text(item.get("type"), 80),
        entity_id=_text(item.get("intuitentityid"), 80),
        entity_count=1,
        payload=item,
    )


def _legacy_notification(item: dict) -> Event:
    change = item.get("dataChangeEvent")
    entities = change.get("entities") if isinstance(change, dict) else None
    return Event(
        event_id=None,
        realm_id=_text(item.get("realmId"), 40),
        event_type="legacy",
        entity_id=None,
        entity_count=len(entities) if isinstance(entities, list) else 0,
        payload=item,
    )


def parse_events(body: Any) -> list[Event]:
    """The events of a parsed body. CloudEvents first, the legacy shape second; a body
    matching neither is one event with no realm and ``entity_count`` 0."""
    if isinstance(body, list) and body and all(isinstance(i, dict) for i in body):
        if all("intuitaccountid" in i or "specversion" in i for i in body):
            return [_cloud_event(i) for i in body]
    if isinstance(body, dict):
        notifications = body.get("eventNotifications")
        if isinstance(notifications, list) and notifications:
            return [_legacy_notification(n) for n in notifications if isinstance(n, dict)] or [
                Event(None, None, None, None, 0, body)
            ]
    return [Event(None, None, None, None, 0, body if isinstance(body, dict | list) else {})]


def parse_body(body: bytes) -> list[Event]:
    try:
        parsed = json_loads(body)
    except ValueError:
        text_ = body.decode("utf-8", "replace")[:_MAX_TEXT]
        return [Event(None, None, None, None, 0, {"unreadable_body": text_})]
    return parse_events(parsed)


# --- store -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Stored:
    rows: int
    duplicates: int
    realms: tuple[str, ...] = field(default_factory=tuple)


def store_delivery(
    engine: Engine, *, body: bytes, tid: str | None, schema_version: str | None, now: datetime
) -> Stored:
    events = parse_body(body)
    rows = duplicates = 0
    realms: list[str] = []
    with untenanted_session(engine) as db:
        for e in events:
            stmt = (
                insert(WebhookEvent)
                .values(
                    received_at=now,
                    event_id=e.event_id,
                    realm_id=e.realm_id,
                    event_type=e.event_type,
                    entity_id=e.entity_id,
                    entity_count=e.entity_count,
                    payload=e.payload,
                    intuit_tid=_text(tid, 80),
                    schema_version=_text(schema_version, 40),
                )
                .on_conflict_do_nothing(
                    index_elements=["event_id"], index_where=text("event_id IS NOT NULL")
                )
                .returning(WebhookEvent.id)
            )
            if db.execute(stmt).scalar_one_or_none() is None:
                duplicates += 1
            else:
                rows += 1
            if e.realm_id and e.realm_id not in realms:
                realms.append(e.realm_id)
    log.info(
        "qbo webhook stored rows=%d duplicates=%d realms=%d tid=%s",
        rows,
        duplicates,
        len(realms),
        _text(tid, 80) or "-",
    )
    return Stored(rows, duplicates, tuple(realms))


# --- dispatch --------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    tenant_id: UUID
    connection_id: UUID
    status: str


def connection_for_realm(engine: Engine, realm_id: str) -> Held | None:
    """The tenant that holds this QuickBooks company, or ``None``. One short
    transaction per tenant, as the worker does; never two tenants in one."""
    with untenanted_session(engine) as db:
        tenant_ids = list(db.execute(select(Tenant.id).order_by(Tenant.created_at)).scalars())
    for tenant_id in tenant_ids:
        with tenant_session(engine, tenant_id) as db:
            row = db.execute(
                select(Connection.id, Connection.status).where(
                    Connection.tenant_id == tenant_id,
                    Connection.system == SYSTEM,
                    Connection.realm_id == realm_id,
                )
            ).one_or_none()
            if row is not None:
                return Held(tenant_id, row[0], row[1])
    return None


def dispatch(
    engine: Engine, realms: tuple[str, ...], received_at: datetime, *, source: str = "webhook"
) -> dict[str, str]:
    """realm → ``queued``, ``already_queued``, the connection's status when not
    connected, or ``unknown``. ``source="disconnect"`` (the Intuit-side disconnect
    page) enqueues the same poll but does not count as a webhook."""
    outcomes: dict[str, str] = {}
    for realm in realms:
        held = connection_for_realm(engine, realm)
        if held is None:
            outcomes[realm] = "unknown"
            continue
        with tenant_session(engine, held.tenant_id) as db:
            connection = db.execute(
                select(Connection).where(Connection.id == held.connection_id).with_for_update()
            ).scalar_one()
            if source == "webhook" and (
                connection.last_webhook_at is None or connection.last_webhook_at < received_at
            ):
                connection.last_webhook_at = received_at
            if connection.status != "connected":
                outcomes[realm] = connection.status
                continue
            created = enqueue_cdc_poll(db, held.tenant_id, connection.id, slot=None)
            outcomes[realm] = "queued" if created is not None else "already_queued"
        log.info(
            "qbo %s realm=%s tenant=%s outcome=%s", source, realm, held.tenant_id, outcomes[realm]
        )
    return outcomes


def dispatch_in_background(engine: Engine, realms: tuple[str, ...], received_at: datetime) -> None:
    """The background-task entry: a failure is logged, never raised into the server."""
    try:
        dispatch(engine, realms, received_at)
    except Exception as exc:  # noqa: BLE001 - the scheduled poll is the safety net
        log.error("qbo webhook dispatch failed: %s", type(exc).__name__)
        raise


# --- what the Connections page shows -----------------------------------------------


def deliveries_in_window(db: Session, realm_id: str, now: datetime) -> int:
    """Rows stored for this realm in the last 24 hours. ``webhook_event`` has no tenant
    scope; the caller passes the realm of the tenant it is acting for."""
    return db.execute(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.realm_id == realm_id, WebhookEvent.received_at > now - COUNT_WINDOW)
    ).scalar_one()
