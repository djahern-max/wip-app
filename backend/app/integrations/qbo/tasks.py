"""Worker tasks for the QuickBooks copy (F05; D-19). Every task takes ``tenant_id``
first, opens its own ``tenant_session`` and touches one tenant. Raw before
normalized: a page or a poll stores payloads through ``store_raw`` /
``store_delete`` and enqueues a normalize task holding **ids only**.

- ``qbo.backfill_page``: one page of one entity by keyset; enqueues the next page
  or marks the entity done; the run finishes when every entity is done.
- ``qbo.cdc_poll``: one Change Data Capture request from the resume cursor;
  successor first.
- ``qbo.normalize``: apply the given raw versions (skipping any that are no longer
  the latest).
- ``qbo.drift_check``: counts and month totals against QuickBooks, nightly.

A connection that needs reconnecting ends the task without retries
(``PermanentTaskError``); the worker keeps running. Log lines carry ids, entity
names, counts and codes; never a payload or a name.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, select

from app.core.db import tenant_session
from app.domain.billing.report import current_counts, raw_month_totals
from app.domain.billing.sync import apply_raw, attach_account_ids
from app.domain.billing.totals import month_totals
from app.ingest.models import Connection, RawRecord, SyncRun
from app.ingest.raw import RawOrigin, latest_raw, store_delete, store_raw
from app.ingest.sync_runs import finish_sync_run, iso_utc, start_sync_run
from app.integrations.qbo import SYSTEM, client, fetch
from app.integrations.qbo.entities import ENTITIES, NORMALIZED, SINGLETONS
from app.integrations.qbo.reader import CompanyReader
from app.integrations.qbo.schedule import (
    BACKFILL_OVERLAP,
    BACKFILL_PAGE_KIND,
    CDC_KIND,
    DRIFT_KIND,
    NORMALIZE_KIND,
    cursor_too_old,
    enqueue_cdc_poll,
    enqueue_drift_check,
    ensure_scheduled,
    next_cdc_slot,
    next_drift_slot,
    resume_cursor,
)
from app.integrations.qbo.tokens import NeedsReconnect
from app.worker.queue import PermanentTaskError, enqueue
from app.worker.registry import task
from app.worker.runner import STARTUP_HOOKS

log = logging.getLogger("app.qbo")

CDC_ENTITIES = ENTITIES  # all sixteen are accepted (S-01 (c))
NORMALIZE_BATCH = 200


def _connection(db, connection_id: UUID) -> Connection | None:
    return db.execute(select(Connection).where(Connection.id == connection_id)).scalar_one_or_none()


def _enqueue_normalize(db, tenant_id: UUID, connection_id: UUID, entity: str, raw_ids: list[UUID]):
    for start in range(0, len(raw_ids), NORMALIZE_BATCH):
        chunk = raw_ids[start : start + NORMALIZE_BATCH]
        enqueue(
            db,
            tenant_id,
            NORMALIZE_KIND,
            {
                "connection_id": str(connection_id),
                "entity": entity,
                "raw_record_ids": [str(i) for i in chunk],
            },
        )


def _store_rows(db, tenant_id: UUID, entity: str, rows: list[dict], origin: RawOrigin):
    """Store payloads; returns (fetched, stored, ids of the versions to normalize)."""
    stored: list[UUID] = []
    for row in rows:
        external_id = row.get("Id")
        if not isinstance(external_id, str) or not external_id:
            continue
        result = store_raw(db, tenant_id, SYSTEM, entity, external_id, row, origin)
        if result.record is not None:
            stored.append(result.record.id)
    return len(rows), len(stored), stored


# --- backfill --------------------------------------------------------------------------


@task(BACKFILL_PAGE_KIND)
def backfill_page(
    tenant_id: UUID,
    *,
    engine: Engine,
    connection_id: str,
    sync_run_id: str,
    entity: str,
    after_id: str,
) -> None:
    cid, rid = UUID(connection_id), UUID(sync_run_id)
    with tenant_session(engine, tenant_id) as db:
        connection, run = _connection(db, cid), db.get(SyncRun, rid)
        if connection is None or run is None or run.outcome is not None:
            return  # the run was finished or failed elsewhere: nothing to do
        if connection.status != "connected":
            raise PermanentTaskError("needs_reconnect")
        realm_id = connection.realm_id or ""
    reader = CompanyReader(engine, tenant_id, cid)
    try:
        if entity in SINGLETONS:
            rows = fetch.singleton(reader, entity, realm_id)
        else:
            rows = fetch.page_after(reader, entity, after_id)
    except NeedsReconnect as exc:
        _fail_run(engine, tenant_id, rid, "needs_reconnect", tid=exc.tid)
        raise PermanentTaskError("needs_reconnect") from exc
    with tenant_session(engine, tenant_id) as db:
        run = db.get(SyncRun, rid)
        if run is None or run.outcome is not None:
            return
        fetched, stored, ids = _store_rows(db, tenant_id, entity, rows, RawOrigin(sync_run_id=rid))
        run.records_fetched += fetched
        run.records_stored += stored
        if entity in NORMALIZED or entity == "Account":
            _enqueue_normalize(db, tenant_id, cid, entity, ids)
        detail = dict(run.detail or {})
        entities = dict(detail.get("entities", {}))
        state = dict(entities.get(entity, {}))
        state["pages"] = int(state.get("pages", 0)) + 1
        full_page = entity not in SINGLETONS and len(rows) >= fetch.PAGE_SIZE
        if full_page:
            last_id = str(rows[-1].get("Id"))
            enqueue(
                db,
                tenant_id,
                BACKFILL_PAGE_KIND,
                {
                    "connection_id": connection_id,
                    "sync_run_id": sync_run_id,
                    "entity": entity,
                    "after_id": last_id,
                },
                dedupe_key=f"backfill:{rid}:{entity}:{last_id}",
            )
        else:
            state["done"] = True
        entities[entity] = state
        detail["entities"] = entities
        run.detail = detail
        db.flush()
        log.info(
            "qbo backfill page entity=%s fetched=%d stored=%d run=%s tenant=%s",
            entity,
            fetched,
            stored,
            rid,
            tenant_id,
        )
        if all(e.get("done") for e in entities.values()) and set(entities) >= set(ENTITIES):
            finish_sync_run(
                db,
                run,
                outcome="succeeded",
                records_fetched=run.records_fetched,
                records_stored=run.records_stored,
                cursor={
                    "realm_id": detail.get("realm_id"),
                    "changed_since": detail["changed_since"],
                },
                detail=detail,
            )
            connection = _connection(db, cid)
            if connection is not None:
                connection.last_success_at = datetime.now(UTC)
            ensure_scheduled(db, tenant_id)
            log.info("qbo backfill finished run=%s tenant=%s", rid, tenant_id)


def _fail_run(
    engine: Engine, tenant_id: UUID, run_id: UUID, error: str, *, tid: str | None = None
) -> None:
    """Close the run as failed. ``tid`` (Intuit's ``intuit_tid`` of the failing
    response) goes on ``detail`` so a support case can name the call (F05.1)."""
    with tenant_session(engine, tenant_id) as db:
        run = db.get(SyncRun, run_id)
        if run is not None and run.outcome is None:
            detail = dict(run.detail or {})
            if tid:
                detail["intuit_tid"] = tid
            finish_sync_run(
                db,
                run,
                outcome="failed",
                records_fetched=run.records_fetched,
                records_stored=run.records_stored,
                error=error,
                detail=detail,
            )


# --- change polling --------------------------------------------------------------------


@task(CDC_KIND)
def cdc_poll(tenant_id: UUID, *, engine: Engine, connection_id: str, slot: str | None) -> None:
    cid = UUID(connection_id)
    now = datetime.now(UTC)
    with tenant_session(engine, tenant_id) as db:
        connection = _connection(db, cid)
        if connection is None or connection.status != "connected":
            log.info("qbo poll skipped connection=%s tenant=%s (not connected)", cid, tenant_id)
            return  # the chain ends here; reconnect seeds it again
        cursor = resume_cursor(db, connection)
        if cursor is None or cursor_too_old(cursor, now):
            run = start_sync_run(db, tenant_id, cid, "cdc")
            reason = "no_cursor" if cursor is None else "cursor_too_old"
            finish_sync_run(db, run, outcome="failed", error=reason)
            log.warning(
                "qbo poll stopped connection=%s tenant=%s reason=%s", cid, tenant_id, reason
            )
            return  # no successor: the page offers a fresh backfill
        if slot is not None:  # a scheduled poll re-arms the chain before doing anything
            own = datetime.fromisoformat(slot.replace("Z", "+00:00"))
            enqueue_cdc_poll(db, tenant_id, cid, slot=next_cdc_slot(max(now, own)))
        run = start_sync_run(db, tenant_id, cid, "cdc")
        run_id, realm_id = run.id, connection.realm_id
        since = datetime.fromisoformat(cursor["changed_since"].replace("Z", "+00:00"))
    reader = CompanyReader(engine, tenant_id, cid)
    try:
        result = fetch.cdc(reader, CDC_ENTITIES, since)
        overflow_rows = {e: fetch.changed_after(reader, e, since) for e in result.overflowed}
    except NeedsReconnect as exc:
        _fail_run(engine, tenant_id, run_id, "needs_reconnect", tid=exc.tid)
        raise PermanentTaskError("needs_reconnect") from exc
    except client.QboError as exc:
        _fail_run(engine, tenant_id, run_id, exc.code, tid=exc.tid)
        raise
    with tenant_session(engine, tenant_id) as db:
        run = db.get(SyncRun, run_id)
        origin = RawOrigin(sync_run_id=run_id)
        fetched = stored = 0
        detail: dict = {"changed": {}, "deleted": {}}
        for entity in CDC_ENTITIES:
            rows = overflow_rows.get(entity, result.changed[entity])
            n_fetched, n_stored, ids = _store_rows(db, tenant_id, entity, rows, origin)
            for stub in result.deleted[entity]:
                external_id = stub.get("Id")
                if not isinstance(external_id, str) or not external_id:
                    continue
                n_fetched += 1
                deleted = store_delete(db, tenant_id, SYSTEM, entity, external_id, stub, origin)
                if deleted.record is not None:
                    n_stored += 1
                    ids.append(deleted.record.id)
            fetched += n_fetched
            stored += n_stored
            if n_fetched:
                detail["changed"][entity] = len(rows)
            if result.deleted[entity]:
                detail["deleted"][entity] = len(result.deleted[entity])
            if ids and (entity in NORMALIZED or entity == "Account"):
                _enqueue_normalize(db, tenant_id, cid, entity, ids)
        if result.overflowed:
            detail["overflow"] = list(result.overflowed)
        finish_sync_run(
            db,
            run,
            outcome="succeeded",
            records_fetched=fetched,
            records_stored=stored,
            cursor={"realm_id": realm_id, "changed_since": iso_utc(now - BACKFILL_OVERLAP)},
            detail=detail,
        )
        connection = _connection(db, cid)
        if connection is not None:
            connection.last_success_at = datetime.now(UTC)
        log.info(
            "qbo poll fetched=%d stored=%d run=%s tenant=%s", fetched, stored, run_id, tenant_id
        )


# --- normalize -------------------------------------------------------------------------


@task(NORMALIZE_KIND)
def normalize(
    tenant_id: UUID, *, engine: Engine, connection_id: str, entity: str, raw_record_ids: list
) -> None:
    applied = skipped = stale = 0
    with tenant_session(engine, tenant_id) as db:
        for raw_id in raw_record_ids:
            raw = db.get(RawRecord, UUID(str(raw_id)))
            if raw is None:
                continue
            current = latest_raw(db, tenant_id, raw.source, raw.entity_type, raw.external_id)
            if current is None or current.id != raw.id:
                stale += 1  # a newer version exists; its own task applies it
                continue
            outcome = apply_raw(db, tenant_id, raw)
            if outcome.result == "skipped":
                skipped += 1
            elif outcome.result in ("applied", "deleted"):
                applied += 1
        if entity == "Account":
            result = attach_account_ids(db, tenant_id)
            log.info(
                "qbo accounts attached=%d changed=%d without_number=%d duplicates=%d tenant=%s",
                result.attached,
                result.changed,
                result.without_number,
                result.duplicate_numbers,
                tenant_id,
            )
    log.info(
        "qbo normalize entity=%s applied=%d skipped=%d stale=%d tenant=%s",
        entity,
        applied,
        skipped,
        stale,
        tenant_id,
    )


# --- drift check -----------------------------------------------------------------------


@task(DRIFT_KIND)
def drift_check(tenant_id: UUID, *, engine: Engine, connection_id: str, slot: str) -> None:
    cid = UUID(connection_id)
    now = datetime.now(UTC)
    with tenant_session(engine, tenant_id) as db:
        connection = _connection(db, cid)
        if connection is None or connection.status != "connected":
            return
        own = datetime.fromisoformat(slot.replace("Z", "+00:00"))
        enqueue_drift_check(
            db, tenant_id, cid, next_drift_slot(max(now, own) + timedelta(minutes=1))
        )
        run = start_sync_run(db, tenant_id, cid, "drift")
        run_id = run.id
    reader = CompanyReader(engine, tenant_id, cid)
    quickbooks: dict[str, int | None] = {}
    try:
        for entity in ENTITIES:
            if entity not in SINGLETONS:
                quickbooks[entity] = fetch.count(reader, entity)
    except NeedsReconnect as exc:
        _fail_run(engine, tenant_id, run_id, "needs_reconnect", tid=exc.tid)
        raise PermanentTaskError("needs_reconnect") from exc
    except client.QboError as exc:
        _fail_run(engine, tenant_id, run_id, exc.code, tid=exc.tid)
        raise
    with tenant_session(engine, tenant_id) as db:
        run = db.get(SyncRun, run_id)
        platform = current_counts(db, tenant_id)
        counts = {
            e: {"quickbooks": q, "platform": platform.get(e, 0)}
            for e, q in quickbooks.items()
            if q is not None and q != platform.get(e, 0)
        }
        normalized = {m.month: m for m in month_totals(db, tenant_id)}
        raw = {m.month: m for m in raw_month_totals(db, tenant_id)}
        months: dict[str, dict[str, dict[str, str]]] = {}
        for month in sorted(set(normalized) | set(raw)):
            for column in ("invoices", "credit_memos", "sales_receipts", "payments"):
                a = getattr(normalized[month], column) if month in normalized else 0
                b = getattr(raw[month], column) if month in raw else 0
                if a != b:
                    months.setdefault(month, {})[column] = {
                        "normalized": f"{a:.2f}",
                        "raw": f"{b:.2f}",
                    }
        detail = {"counts": counts, "months": months, "checked_at": iso_utc(now)}
        outcome = "drift" if counts or months else "succeeded"
        finish_sync_run(db, run, outcome=outcome, detail=detail)
        log.info(
            "qbo drift check outcome=%s entities=%d months=%d tenant=%s",
            outcome,
            len(counts),
            len(months),
            tenant_id,
        )


STARTUP_HOOKS.append(ensure_scheduled)
