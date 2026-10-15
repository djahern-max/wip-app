"""What the Connections page shows about the copy (F05): per-entity counts of current
raw records, skipped payloads, and the raw-side month re-sum the drift check uses.

Skipped payloads are **derived, never counted**: a latest raw version of a normalized
entity that no canonical row points at was skipped (or has not been applied yet).
A deleted record never held is not skipped: there was nothing to build.

F05.3 (D-48): every figure here takes the company's ``history_start``. A transactional
record whose latest payload is dated before it (held raw from a change poll) is counted
under "Before history start", apart from the current and the unreadable ones, and left
out of the raw re-sum, so the nightly check compares like with like. ``None`` is a
company that has not decided its start: nothing is pulled for it, and nothing is left
out of what it already holds.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, false, func, select
from sqlalchemy.orm import Session

from app.domain.billing.models import Billing, Customer, Payment
from app.ingest.models import RawRecord
from app.ingest.raw import latest_raw_versions
from app.integrations.qbo.entities import ENTITIES, NORMALIZED, TRANSACTIONAL

SOURCE = "qbo"
ZERO = Decimal("0.00")


def _latest_ids_subquery(tenant_id: UUID):
    latest = (
        select(
            RawRecord.entity_type,
            RawRecord.external_id,
            func.max(RawRecord.version).label("version"),
        )
        .where(RawRecord.tenant_id == tenant_id, RawRecord.source == SOURCE)
        .group_by(RawRecord.entity_type, RawRecord.external_id)
        .subquery()
    )
    return (
        select(
            RawRecord.id,
            RawRecord.entity_type,
            RawRecord.is_deleted,
            RawRecord.payload["TxnDate"].astext.label("txn_date"),
        )
        .join(
            latest,
            and_(
                RawRecord.entity_type == latest.c.entity_type,
                RawRecord.external_id == latest.c.external_id,
                RawRecord.version == latest.c.version,
            ),
        )
        .where(RawRecord.tenant_id == tenant_id, RawRecord.source == SOURCE)
        .subquery()
    )


def _before_start(latest, history_start: date | None):
    """The SQL condition for a transactional record dated before the history start
    (``TxnDate`` is ISO, so text order is date order); never true without a start."""
    if history_start is None:
        return false()
    return and_(
        latest.c.entity_type.in_(TRANSACTIONAL), latest.c.txn_date < history_start.isoformat()
    )


def current_counts(
    db: Session, tenant_id: UUID, *, history_start: date | None = None
) -> dict[str, int]:
    """Current, non-deleted raw records per entity (what the drift check compares
    with QuickBooks' own COUNT(*)); a transactional entity's from the history start."""
    latest = _latest_ids_subquery(tenant_id)
    rows = db.execute(
        select(latest.c.entity_type, func.count())
        .where(latest.c.is_deleted.is_(False), ~_before_start(latest, history_start))
        .group_by(latest.c.entity_type)
    ).all()
    counts = {entity: 0 for entity in ENTITIES}
    counts.update({e: n for e, n in rows if e in counts})
    return counts


def before_start_counts(
    db: Session, tenant_id: UUID, *, history_start: date | None
) -> dict[str, int]:
    """D-48: per transactional entity, current non-deleted raw records whose latest
    payload is dated before the history start (held raw, skipped)."""
    counts = {entity: 0 for entity in TRANSACTIONAL}
    if history_start is None:
        return counts
    latest = _latest_ids_subquery(tenant_id)
    rows = db.execute(
        select(latest.c.entity_type, func.count())
        .where(latest.c.is_deleted.is_(False), _before_start(latest, history_start))
        .group_by(latest.c.entity_type)
    ).all()
    counts.update({e: n for e, n in rows if e in counts})
    return counts


def skipped_counts(
    db: Session, tenant_id: UUID, *, history_start: date | None = None
) -> dict[str, int]:
    """Per normalized entity: latest non-deleted raw versions no canonical row points
    at, leaving out those dated before the history start (counted apart, D-48)."""
    latest = _latest_ids_subquery(tenant_id)
    referenced = set()
    for model in (Customer, Billing, Payment):
        referenced |= set(
            db.execute(select(model.raw_record_id).where(model.tenant_id == tenant_id)).scalars()
        )
    rows = db.execute(
        select(latest.c.id, latest.c.entity_type).where(
            latest.c.is_deleted.is_(False),
            latest.c.entity_type.in_(NORMALIZED),
            ~_before_start(latest, history_start),
        )
    ).all()
    counts = {entity: 0 for entity in NORMALIZED}
    for raw_id, entity in rows:
        if raw_id not in referenced:
            counts[entity] += 1
    return counts


@dataclass(frozen=True)
class RawMonth:
    month: str
    invoices: Decimal
    credit_memos: Decimal
    sales_receipts: Decimal
    payments: Decimal


def raw_month_totals(
    db: Session, tenant_id: UUID, *, history_start: date | None = None
) -> list[RawMonth]:
    """The same four columns, re-summed from the latest raw payloads (``TotalAmt`` by
    ``TxnDate`` month; credit memos negative; deleted versions and payloads dated before
    the history start excluded). Written apart from the normalizers on purpose: the
    drift check compares the two."""
    sums: dict[str, dict[str, Decimal]] = {}
    for entity, column, sign in (
        ("Invoice", "invoices", 1),
        ("CreditMemo", "credit_memos", -1),
        ("SalesReceipt", "sales_receipts", 1),
        ("Payment", "payments", 1),
    ):
        for raw in latest_raw_versions(db, tenant_id, SOURCE, entity):
            payload = raw.payload
            if raw.is_deleted or not isinstance(payload, dict):
                continue
            day, total = payload.get("TxnDate"), payload.get("TotalAmt")
            if not isinstance(day, str) or len(day) < 7:
                continue
            if history_start is not None and day[:10] < history_start.isoformat():
                continue
            if isinstance(total, bool) or not isinstance(total, int | Decimal):
                continue
            month = sums.setdefault(
                day[:7],
                dict.fromkeys(("invoices", "credit_memos", "sales_receipts", "payments"), ZERO),
            )
            month[column] += sign * Decimal(total)
    return [RawMonth(m, **sums[m]) for m in sorted(sums)]
