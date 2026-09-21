"""The raw store (D-20). ``raw_record`` is insert-only; a change is a new version.

``store_raw``: if the latest version for ``(tenant, source, entity_type,
external_id)`` has the same ``payload_sha256`` (and the same deleted flag),
nothing is written; otherwise a new row with ``version + 1``. A delete from the
source is a new version with ``is_deleted = true`` carrying the last known
payload. Prior versions are never touched. "Current" is the highest version.

The hash is computed over the canonical serialization before storage
(``app.core.jsoncodec``), never over what Postgres returns.

Every function requires ``app.tenant_id`` on the session (RLS).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.jsoncodec import payload_sha256
from app.ingest.models import RawRecord


@dataclass(frozen=True)
class RawOrigin:
    """Exactly one of the two (the table's CHECK)."""

    import_batch_id: UUID | None = None
    sync_run_id: UUID | None = None

    def __post_init__(self) -> None:
        if (self.import_batch_id is None) == (self.sync_run_id is None):
            raise ValueError("a raw record has exactly one origin: import_batch or sync_run")


@dataclass(frozen=True)
class StoreResult:
    record: RawRecord | None  # None: identical to the current version, nothing written
    version: int


def latest_raw(
    db: Session, tenant_id: UUID, source: str, entity_type: str, external_id: str
) -> RawRecord | None:
    return db.execute(
        select(RawRecord)
        .where(
            RawRecord.tenant_id == tenant_id,
            RawRecord.source == source,
            RawRecord.entity_type == entity_type,
            RawRecord.external_id == external_id,
        )
        .order_by(RawRecord.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def raw_history(
    db: Session, tenant_id: UUID, source: str, entity_type: str, external_id: str
) -> list[RawRecord]:
    return list(
        db.execute(
            select(RawRecord)
            .where(
                RawRecord.tenant_id == tenant_id,
                RawRecord.source == source,
                RawRecord.entity_type == entity_type,
                RawRecord.external_id == external_id,
            )
            .order_by(RawRecord.version)
        ).scalars()
    )


def store_raw(
    db: Session,
    tenant_id: UUID,
    source: str,
    entity_type: str,
    external_id: str,
    payload,
    origin: RawOrigin,
    *,
    deleted: bool = False,
) -> StoreResult:
    current = latest_raw(db, tenant_id, source, entity_type, external_id)
    if deleted and payload is None and current is not None:
        payload = current.payload  # a delete carries the last known payload
    digest = payload_sha256(payload)
    if current is not None and current.payload_sha256 == digest and current.is_deleted == deleted:
        return StoreResult(record=None, version=current.version)
    row = RawRecord(
        tenant_id=tenant_id,
        source=source,
        entity_type=entity_type,
        external_id=external_id,
        version=1 if current is None else current.version + 1,
        payload=payload,
        payload_sha256=digest,
        is_deleted=deleted,
        import_batch_id=origin.import_batch_id,
        sync_run_id=origin.sync_run_id,
    )
    db.add(row)
    db.flush()
    return StoreResult(record=row, version=row.version)


def latest_raw_versions(
    db: Session, tenant_id: UUID, source: str, entity_type: str
) -> list[RawRecord]:
    """The current (highest) version of every record of one entity."""
    return list(
        db.execute(
            select(RawRecord)
            .where(
                RawRecord.tenant_id == tenant_id,
                RawRecord.source == source,
                RawRecord.entity_type == entity_type,
            )
            .distinct(RawRecord.external_id)
            .order_by(RawRecord.external_id, RawRecord.version.desc())
        ).scalars()
    )


def store_delete(
    db: Session,
    tenant_id: UUID,
    source: str,
    entity_type: str,
    external_id: str,
    stub,
    origin: RawOrigin,
) -> StoreResult:
    """A delete reported by the source. A record we hold gets a new version flagged
    deleted that carries its last known payload (D-20); a record we never held keeps
    the source's stub as its only payload (owner, 2026-09-21), and no canonical row
    is ever built from it."""
    current = latest_raw(db, tenant_id, source, entity_type, external_id)
    payload = None if current is not None else stub
    return store_raw(db, tenant_id, source, entity_type, external_id, payload, origin, deleted=True)
