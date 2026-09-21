"""Recurring work on the existing queue, no scheduler (F05 plan, question 2).

Each poll enqueues its successor **first**, keyed by time slot
(``cdc:{connection_id}:{slot}``), so a running poll cannot block its own successor
(the dedupe index covers queued *and* running rows) and a poll that fails cannot end
the chain. Chains are seeded when a connection completes and when the worker starts
(``ensure_scheduled``, one tenant per transaction). A poll that finds the connection
``needs_reconnect`` or ``disconnected`` ends and enqueues nothing.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import RequestMeta, TenantEvent, write_tenant_audit
from app.core.config import get_settings
from app.ingest.models import Connection, SyncRun
from app.ingest.sync_runs import iso_utc, latest_successful_cursor, start_sync_run
from app.integrations.qbo import SYSTEM
from app.integrations.qbo.entities import ENTITIES
from app.tenancy.models import Role
from app.worker.models import Task
from app.worker.queue import enqueue

log = logging.getLogger("app.qbo")

CDC_KIND = "qbo.cdc_poll"
DRIFT_KIND = "qbo.drift_check"
BACKFILL_PAGE_KIND = "qbo.backfill_page"
NORMALIZE_KIND = "qbo.normalize"
BACKFILL_OVERLAP = timedelta(minutes=2)
# Intuit documents a 30-day CDC look-back; one day of margin (owner, question 3).
CDC_MAX_AGE = timedelta(days=29)
DRIFT_HOUR_UTC = 7  # about 02:00–03:00 Eastern


def qbo_connection(db: Session, tenant_id: UUID) -> Connection | None:
    return db.execute(
        select(Connection).where(Connection.tenant_id == tenant_id, Connection.system == SYSTEM)
    ).scalar_one_or_none()


# --- slots -------------------------------------------------------------------------------


def next_cdc_slot(now: datetime) -> datetime:
    minutes = max(1, get_settings().qbo_cdc_poll_minutes)
    floored = now.replace(second=0, microsecond=0)
    floored -= timedelta(minutes=floored.minute % minutes)
    return floored + timedelta(minutes=minutes)


def next_drift_slot(now: datetime) -> datetime:
    candidate = now.replace(hour=DRIFT_HOUR_UTC, minute=0, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _open_task(db: Session, kind: str, prefix: str) -> Task | None:
    return db.execute(
        select(Task)
        .where(
            Task.kind == kind,
            Task.dedupe_key.like(prefix + "%"),
            Task.status.in_(("queued", "running")),
        )
        .limit(1)
    ).scalar_one_or_none()


def enqueue_cdc_poll(
    db: Session, tenant_id: UUID, connection_id: UUID, *, slot: datetime | None
) -> Task | None:
    """``slot=None`` is "now" (Sync now); a slot is a scheduled poll."""
    key = f"cdc:{connection_id}:{'now' if slot is None else iso_utc(slot)}"
    return enqueue(
        db,
        tenant_id,
        CDC_KIND,
        {"connection_id": str(connection_id), "slot": None if slot is None else iso_utc(slot)},
        dedupe_key=key,
        run_after=slot,
    )


def enqueue_drift_check(db: Session, tenant_id: UUID, connection_id: UUID, slot: datetime):
    return enqueue(
        db,
        tenant_id,
        DRIFT_KIND,
        {"connection_id": str(connection_id), "slot": iso_utc(slot)},
        dedupe_key=f"drift:{connection_id}:{slot.date().isoformat()}",
        run_after=slot,
    )


def ensure_scheduled(db: Session, tenant_id: UUID, now: datetime | None = None) -> None:
    """Seed the poll and drift chains of a connected company that has none open.
    Idempotent (slot keys). Called on connect and at worker start-up."""
    connection = qbo_connection(db, tenant_id)
    if connection is None or connection.status != "connected":
        return
    now = now or datetime.now(UTC)
    if _open_task(db, CDC_KIND, f"cdc:{connection.id}:") is None:
        enqueue_cdc_poll(db, tenant_id, connection.id, slot=next_cdc_slot(now))
    if _open_task(db, DRIFT_KIND, f"drift:{connection.id}:") is None:
        enqueue_drift_check(db, tenant_id, connection.id, next_drift_slot(now))


# --- cursors -----------------------------------------------------------------------------


def resume_cursor(db: Session, connection: Connection) -> dict | None:
    """The cursor the next poll starts from: the latest successful CDC run, else the
    finished backfill. A cursor from another company (``realm_id``) is not one."""
    for kind in ("cdc", "backfill"):
        cursor = latest_successful_cursor(db, connection.id, kind)
        if isinstance(cursor, dict) and cursor.get("realm_id") == connection.realm_id:
            if isinstance(cursor.get("changed_since"), str):
                return cursor
    return None


def cursor_moment(cursor: dict) -> datetime:
    return datetime.fromisoformat(cursor["changed_since"].replace("Z", "+00:00"))


def cursor_too_old(cursor: dict, now: datetime) -> bool:
    return now - cursor_moment(cursor) > CDC_MAX_AGE


# --- backfill ---------------------------------------------------------------------------------


def open_backfill(db: Session, connection_id: UUID) -> SyncRun | None:
    return db.execute(
        select(SyncRun)
        .where(
            SyncRun.connection_id == connection_id,
            SyncRun.kind == "backfill",
            SyncRun.outcome.is_(None),
        )
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def start_backfill(db: Session, tenant_id: UUID, connection: Connection) -> SyncRun | None:
    """One task per entity to fetch its first page. The CDC watermark is taken
    **before** page 1 (minus an overlap), so anything changed during the backfill is
    picked up by the first poll. ``None`` when a backfill is already running."""
    if open_backfill(db, connection.id) is not None:
        return None
    now = datetime.now(UTC)
    run = start_sync_run(db, tenant_id, connection.id, "backfill")
    run.detail = {
        "realm_id": connection.realm_id,
        "changed_since": iso_utc(now - BACKFILL_OVERLAP),
        "entities": {entity: {"pages": 0, "done": False} for entity in ENTITIES},
    }
    db.flush()
    for entity in ENTITIES:
        enqueue(
            db,
            tenant_id,
            BACKFILL_PAGE_KIND,
            {
                "connection_id": str(connection.id),
                "sync_run_id": str(run.id),
                "entity": entity,
                "after_id": "0",
            },
            dedupe_key=f"backfill:{run.id}:{entity}:0",
        )
    log.info("qbo backfill started run=%s tenant=%s", run.id, tenant_id)
    return run


def after_connect(db: Session, tenant_id: UUID, connection: Connection) -> str:
    """A first connection starts a backfill; a reconnect with a usable cursor resumes
    polling; a reconnect whose cursor is too old waits for the admin to ask for a
    fresh backfill (never silently). Returns what was done."""
    cursor = resume_cursor(db, connection)
    if cursor is None:
        start_backfill(db, tenant_id, connection)
        return "backfill"
    if cursor_too_old(cursor, datetime.now(UTC)):
        return "backfill_needed"
    ensure_scheduled(db, tenant_id)
    return "resumed"


def request_sync(
    db: Session,
    tenant_id: UUID,
    connection: Connection,
    *,
    kind: str,
    actor_user_id: UUID | None,
    actor_role: Role | None,
    meta: RequestMeta | None,
) -> bool:
    """ "Sync now" (one CDC poll) or a fresh backfill, audited. ``False`` when the
    same request is already queued or running."""
    if kind == "cdc":
        created = enqueue_cdc_poll(db, tenant_id, connection.id, slot=None) is not None
    elif kind == "backfill":
        created = start_backfill(db, tenant_id, connection) is not None
    else:
        raise ValueError(kind)
    write_tenant_audit(
        db,
        tenant_id=tenant_id,
        action=TenantEvent.sync_requested,
        entity_type="connection",
        entity_id=connection.id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        detail={"system": SYSTEM, "kind": kind, "created": created},
        meta=meta,
    )
    return created
