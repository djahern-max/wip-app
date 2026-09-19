"""Upload and import pipeline (F03). Raw before normalized: the object is written
before the ``import_batch`` row is committed; the row, the task and the audit row
land in one transaction.

``receive_upload`` streams the file to a temporary file while hashing, enforces
``MAX_UPLOAD_BYTES`` and the source kind's extensions before anything is stored,
writes the object (content-addressed: ``imports/{sha256}.{ext}`` under the tenant
prefix, so a retried upload overwrites the identical object), then inserts the
batch. The same bytes for the same tenant and source kind return the existing
batch with ``duplicate=True``: no second batch, object or task.

The task ``import.process_batch`` opens the object, runs the source's ``parse``,
calls ``store_raw`` per item, and sets counts and status. A ``parse`` that raises
marks the batch ``failed`` with ``parse failed: <ExceptionType>`` (never file
content) and fails the task with no retry; a failure opening the object or talking
to the database leaves the batch ``received`` and the task retries with backoff; a
rejected item is counted and the rest still loads (``loaded_with_issues``). A
``failed`` batch is final. Re-processing a loaded batch is safe: identical payloads
write nothing (D-20).
"""

import hashlib
import logging
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import RequestMeta, TenantEvent, write_tenant_audit
from app.core.config import get_settings
from app.core.db import tenant_session
from app.core.jsoncodec import JSONEncodeError
from app.core.storage import ObjectStore
from app.ingest.models import ImportBatch
from app.ingest.raw import RawOrigin, store_raw
from app.integrations.base import SOURCE_KINDS, RawItem, get_source_kind
from app.tenancy.models import Role
from app.worker.queue import PermanentTaskError, describe_error, enqueue, open_task
from app.worker.registry import task

log = logging.getLogger("app.ingest")

PROCESS_BATCH = "import.process_batch"
CHUNK = 1024 * 1024
_EXT = re.compile(r"^[a-z0-9]{1,10}$")


class UploadRefused(Exception):
    """``detail`` is the sentence shown to the person (what happened, what to do
    next; D-22); ``reason`` is the machine detail for the log, never shown."""

    def __init__(self, status_code: int, detail: str, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.detail = detail
        self.reason = reason


@dataclass(frozen=True)
class Spooled:
    file: BinaryIO
    sha256: str
    byte_size: int


def spool_and_hash(stream: BinaryIO, max_bytes: int) -> Spooled:
    """Copy ``stream`` to a temporary file while hashing; refuse past ``max_bytes``
    before anything reaches the object store or the database."""
    tmp = tempfile.TemporaryFile()
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK):
        size += len(chunk)
        if size > max_bytes:
            tmp.close()
            raise UploadRefused(
                413,
                f"This file is larger than {human_size(max_bytes)}. Upload a smaller file.",
                f"file exceeds MAX_UPLOAD_BYTES ({max_bytes})",
            )
        digest.update(chunk)
        tmp.write(chunk)
    tmp.seek(0)
    return Spooled(file=tmp, sha256=digest.hexdigest(), byte_size=size)


def human_size(n: int) -> str:
    """``26214400`` → ``25 MB``; ``64`` → ``64 bytes`` (for a message a person reads)."""
    if n >= 1024 * 1024:
        mb = n / (1024 * 1024)
        return f"{mb:.0f} MB" if mb == int(mb) else f"{mb:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} bytes"


def safe_extension(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if _EXT.match(ext) else "bin"


FOLLOWUP_LABELS: dict[str, str] = {
    "queued": "Updating",
    "running": "Updating",
    "succeeded": "Updated",
    "failed": "Not updated",
}


def followup_message(subject: str, followup_status: str | None) -> str | None:
    """Owner amendment C: what the after_load task's state means, in one sentence."""
    if not followup_status or not subject:
        return None
    if followup_status in ("queued", "running"):
        return f"Updating {subject}…"
    if followup_status == "succeeded":
        return f"{subject.capitalize()} updated."
    return f"but the {subject} could not be updated. Try uploading again, or contact support."


def batch_message(batch: ImportBatch, followup_status: str | None = None) -> str | None:
    """The sentence shown next to a batch's status (D-22): what happened and what to
    do next, never a setting or an exception type. ``batch.error`` keeps the machine
    detail for OPERATIONS and the logs. With a follow-on task (amendment C) the
    sentence continues: "Loaded. Updating accounts…" / "Loaded. Accounts updated." /
    "Loaded, but the accounts could not be updated. …"."""
    if batch.status == "failed":
        return "This file could not be read. Check that it is the right export and upload it again."
    if batch.status == "received" and batch.error:
        return "Processing did not finish; it will be tried again shortly. Refresh in a minute."
    base: str | None = None
    kind = SOURCE_KINDS.get(batch.source_kind)
    if batch.status == "loaded_with_issues" and batch.rows_loaded == 0:
        # Nothing was loaded, so nothing follows: never "the rest were loaded".
        what = kind.records_noun if kind else "rows"
        export = kind.file_noun if kind else "the right export"
        return (
            f"No {what} could be read from this file. "
            f"Check that it is {export} and upload it again."
        )
    if batch.status == "loaded_with_issues":
        n = batch.rows_rejected
        rows = (
            "1 row could not be read and was" if n == 1 else f"{n} rows could not be read and were"
        )
        base = f"{rows} skipped; the rest were loaded."
    tail = followup_message(kind.after_load_subject if kind else "", followup_status)
    if tail is None:
        return base
    if followup_status == "failed":
        head = base or "Loaded"
        return f"{head.rstrip('.')}, {tail}"
    return f"{base or 'Loaded.'} {tail}"


def relative_key(batch: ImportBatch) -> str:
    prefix = f"tenant/{batch.tenant_id}/"
    if not batch.object_key.startswith(prefix):
        raise ValueError("object key does not belong to this tenant")
    return batch.object_key[len(prefix) :]


def find_batch(db: Session, tenant_id: UUID, source_kind: str, sha256: str) -> ImportBatch | None:
    return db.execute(
        select(ImportBatch).where(
            ImportBatch.tenant_id == tenant_id,
            ImportBatch.source_kind == source_kind,
            ImportBatch.sha256 == sha256,
        )
    ).scalar_one_or_none()


def receive_upload(
    db: Session,
    store: ObjectStore,
    *,
    tenant_id: UUID,
    source_kind: str,
    filename: str,
    content_type: str | None,
    stream: BinaryIO,
    actor_user_id: UUID | None,
    actor_role: Role | None,
    meta: RequestMeta | None = None,
) -> tuple[ImportBatch, bool]:
    """``(batch, duplicate)``. Requires ``app.tenant_id`` = ``tenant_id`` on ``db``."""
    try:
        kind = get_source_kind(source_kind)
    except LookupError:
        raise UploadRefused(
            422, "Choose a source from the list.", f"unknown source kind {source_kind!r}"
        ) from None
    # The filename is data: stored as text on the row, never part of a key.
    name = (filename or "").strip()[:255] or "upload"
    if not kind.accepts(name):
        accepted = ", ".join(f".{e}" for e in sorted(kind.extensions))
        raise UploadRefused(
            415,
            f"{kind.label} files must end in {accepted}. "
            f"Choose a {accepted} file and upload it again.",
            f"source kind {kind.name!r} does not accept this file extension",
        )
    spooled = spool_and_hash(stream, get_settings().max_upload_bytes)
    with spooled.file:
        existing = find_batch(db, tenant_id, kind.name, spooled.sha256)
        if existing is not None:
            _audit(db, TenantEvent.import_duplicate, existing, actor_user_id, actor_role, meta)
            return existing, True
        # Raw before normalized: the object exists before the row is committed. A
        # store failure leaves no row (the transaction is rolled back by the 503).
        try:
            object_key = store.put(
                tenant_id, f"imports/{spooled.sha256}.{safe_extension(name)}", spooled.file
            )
        except Exception as exc:  # noqa: BLE001 - logged by type; nothing about the file
            log.error("object store put failed: %s", type(exc).__name__)
            raise UploadRefused(
                503,
                "The file could not be stored. Try again in a minute.",
                f"object store put failed: {type(exc).__name__}",
            ) from None
    batch = ImportBatch(
        tenant_id=tenant_id,
        source_kind=kind.name,
        sha256=spooled.sha256,
        byte_size=spooled.byte_size,
        original_filename=name,
        content_type=content_type[:120] if content_type else None,
        object_key=object_key,
        uploaded_by=actor_user_id,
        status="received",
    )
    try:
        with db.begin_nested():
            db.add(batch)
            db.flush()
    except IntegrityError:
        # A concurrent upload of the same bytes won the race: theirs is the batch.
        existing = find_batch(db, tenant_id, kind.name, spooled.sha256)
        if existing is None:
            raise
        _audit(db, TenantEvent.import_duplicate, existing, actor_user_id, actor_role, meta)
        return existing, True
    enqueue(
        db,
        tenant_id,
        PROCESS_BATCH,
        {"import_batch_id": str(batch.id)},
        dedupe_key=str(batch.id),
    )
    _audit(db, TenantEvent.import_uploaded, batch, actor_user_id, actor_role, meta)
    return batch, False


def _audit(
    db: Session,
    action: TenantEvent,
    batch: ImportBatch,
    actor_user_id: UUID | None,
    actor_role: Role | None,
    meta: RequestMeta | None,
) -> None:
    write_tenant_audit(
        db,
        tenant_id=batch.tenant_id,
        action=action,
        entity_type="import_batch",
        entity_id=batch.id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        detail={
            "source_kind": batch.source_kind,
            "sha256": batch.sha256,
            "byte_size": batch.byte_size,
        },
        meta=meta,
    )


def audit_download(
    db: Session,
    batch: ImportBatch,
    *,
    actor_user_id: UUID | None,
    actor_role: Role | None,
    meta: RequestMeta | None,
) -> None:
    _audit(db, TenantEvent.import_downloaded, batch, actor_user_id, actor_role, meta)


@contextmanager
def open_batch_object(store: ObjectStore, batch: ImportBatch) -> Iterator[BinaryIO]:
    with store.open(batch.tenant_id, relative_key(batch)) as f:
        yield f


# --- the task ---------------------------------------------------------------------------------


@dataclass
class _Counts:
    loaded: int = 0
    rejected: int = 0


# A row the store refuses is one rejected row (a value too long for its column, a
# float in the payload, a key that is not a string). A lost connection is not: it
# propagates and the task retries.
ROW_ERRORS = (DataError, IntegrityError, JSONEncodeError, ValueError, TypeError)


def _parse_items(kind, stream):
    """Iterate ``kind.parse``; an exception raised by the source is permanent."""
    try:
        iterator = iter(kind.parse(stream))
        while True:
            try:
                item = next(iterator)
            except StopIteration:
                return
            yield item
    except PermanentTaskError:
        raise
    except Exception as exc:
        raise PermanentTaskError("parse failed", exc) from exc


def process_batch_now(
    engine: Engine, store: ObjectStore, tenant_id: UUID, import_batch_id: UUID
) -> None:
    """The work of ``import.process_batch``, callable without the queue (tests).

    Outcomes (owner rule, F03 close-out): a ``parse`` that raises marks the batch
    ``failed`` and the task ``failed`` with no retry; failing to open the object or
    to talk to the database, or a source kind this process has not registered (an
    environment fault, not a data fault), leaves the batch ``received`` with the
    error noted and the task retries with backoff. A batch that is ``failed`` never
    goes back to ``processing``: upload the corrected file as a new batch. Every
    exception after the ``processing`` commit passes through the ``finally`` below,
    so no batch is left at ``processing``.
    """
    with tenant_session(engine, tenant_id) as s:
        batch = s.get(ImportBatch, import_batch_id)
        if batch is None:
            raise LookupError("import batch not found in this tenant")
        if batch.status == "failed":
            raise PermanentTaskError("batch already failed")
        batch.status = "processing"
        batch.error = None
        source_kind, key = batch.source_kind, relative_key(batch)
    counts = _Counts()
    outcome: str | None = None  # None = loaded; "failed" = permanent; "retry" = transient
    error: str | None = None
    try:
        # Inside the handled region: the batch is already committed as "processing",
        # so nothing may raise between that commit and this ``try``. A kind this
        # process does not know is an environment fault (transient rule).
        kind = get_source_kind(source_kind)
        with store.open(tenant_id, key) as stream, tenant_session(engine, tenant_id) as s:
            origin = RawOrigin(import_batch_id=import_batch_id)
            for item in _parse_items(kind, stream):
                if not isinstance(item, RawItem):
                    counts.rejected += 1  # RejectedItem, or something the source should not yield
                    continue
                try:
                    with s.begin_nested():
                        store_raw(
                            s,
                            tenant_id,
                            kind.source,
                            item.entity_type,
                            item.external_id,
                            item.payload,
                            origin,
                            deleted=item.deleted,
                        )
                except ROW_ERRORS as exc:
                    counts.rejected += 1
                    log.info("batch=%s rejected item: %s", import_batch_id, type(exc).__name__)
                else:
                    counts.loaded += 1
    except PermanentTaskError as exc:
        outcome, error = "failed", describe_error(exc)
        raise
    except Exception as exc:
        outcome, error = "retry", f"{describe_error(exc)} (will retry)"
        raise
    finally:
        with tenant_session(engine, tenant_id) as s:
            batch = s.get(ImportBatch, import_batch_id)
            if batch is not None:
                batch.rows_loaded = counts.loaded
                batch.rows_rejected = counts.rejected
                batch.processed_at = datetime.now(UTC)
                if outcome == "failed":
                    batch.status = "failed"
                    batch.error = error
                elif outcome == "retry":
                    batch.status = "received"
                    batch.error = error
                else:
                    batch.status = "loaded_with_issues" if counts.rejected else "loaded"
                    if kind.after_load and counts.loaded > 0:
                        # Never for a batch with no loaded row: a follow-on that reads
                        # "the file holds nothing" as "everything was removed" would
                        # change records from a file nobody could read.
                        # Same tenant, same transaction as the status (plan call 3): the
                        # follow-on commits only with it; the dedupe key stops a second
                        # run if this batch task is ever re-executed.
                        dedupe = f"{kind.after_load}:{import_batch_id}"
                        task_row = enqueue(
                            s,
                            tenant_id,
                            kind.after_load,
                            {"import_batch_id": str(import_batch_id)},
                            dedupe_key=dedupe,
                        )
                        if task_row is None:
                            task_row = open_task(s, kind.after_load, dedupe)
                        if task_row is not None:
                            batch.followup_task_id = task_row.id


@task(PROCESS_BATCH)
def process_batch(tenant_id: UUID, *, engine: Engine, import_batch_id: str) -> None:
    from app.core.storage import build_object_store

    process_batch_now(engine, build_object_store(get_settings()), tenant_id, UUID(import_batch_id))
