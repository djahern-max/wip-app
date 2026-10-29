"""What Connections and the nightly check read about the cost side (F10; the shape of
``app/domain/billing/report.py``).

- ``skipped_counts``: per cost entity, the latest non-deleted raw payloads dated on or after
  the history start that the pure normalizer refuses (``Unreadable``). Derived by re-reading
  the payloads, never counted: a document whose lines all post to balance-sheet accounts has
  no ``ledger_line`` and is not skipped, so "no row points at it" cannot be the rule here as
  it is for billing.
- ``ledger_sums`` and ``raw_sums``: the cost side of the nightly check, Σ amount by month and
  QuickBooks account id, from ``ledger_line`` by the database and from a re-read of the raw
  payloads through the same pure normalizer; a difference sets ``drift`` as today.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.billing.sync import before_history_start
from app.domain.billing.totals import decimal, month_of
from app.domain.cost.models import LedgerLine
from app.domain.cost.sync import SOURCE, Lookup, load_lookup
from app.ingest.raw import latest_raw_versions
from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.cost import COST_ENTITIES, normalize_cost

ZERO = Decimal("0.00")
NO_ACCOUNT_KEY = ""


def skipped_counts(
    db: Session, tenant_id: UUID, *, history_start: date | None = None, lookup: Lookup | None = None
) -> dict[str, int]:
    lookup = lookup or load_lookup(db, tenant_id)
    counts = dict.fromkeys(COST_ENTITIES, 0)
    for entity in COST_ENTITIES:
        for raw in latest_raw_versions(db, tenant_id, SOURCE, entity):
            if raw.is_deleted or not isinstance(raw.payload, dict):
                continue
            if history_start is not None and before_history_start(raw.payload, history_start):
                continue
            try:
                normalize_cost(entity, raw.payload, lookup.side)
            except Unreadable:
                counts[entity] += 1
    return counts


def ledger_sums(db: Session, tenant_id: UUID) -> dict[str, dict[str, Decimal]]:
    """month → account id → Σ amount of the live lines, by the database."""
    month = month_of(LedgerLine.txn_date)
    out: dict[str, dict[str, Decimal]] = {}
    for m, account, total in db.execute(
        select(month, LedgerLine.account_external_id, func.sum(LedgerLine.amount))
        .where(LedgerLine.tenant_id == tenant_id, LedgerLine.deleted_at.is_(None))
        .group_by(month, LedgerLine.account_external_id)
    ).all():
        out.setdefault(m, {})[account or NO_ACCOUNT_KEY] = decimal(total)
    return out


def raw_sums(
    db: Session, tenant_id: UUID, *, history_start: date | None = None, lookup: Lookup | None = None
) -> dict[str, dict[str, Decimal]]:
    """The same sums re-read from the latest raw payloads (deleted, voided and pre-start
    documents left out), apart from the rows on purpose: the check compares the two."""
    lookup = lookup or load_lookup(db, tenant_id)
    out: dict[str, dict[str, Decimal]] = {}
    for entity in COST_ENTITIES:
        for raw in latest_raw_versions(db, tenant_id, SOURCE, entity):
            if raw.is_deleted or not isinstance(raw.payload, dict):
                continue
            if history_start is not None and before_history_start(raw.payload, history_start):
                continue
            try:
                doc = normalize_cost(entity, raw.payload, lookup.side)
            except Unreadable:
                continue
            if doc.voided:
                continue
            month = out.setdefault(doc.txn_date.strftime("%Y-%m"), {})
            for ln in doc.lines:
                key = ln.account_external_id or NO_ACCOUNT_KEY
                month[key] = month.get(key, ZERO) + ln.amount
    return out


def cost_differences(
    ledger: dict[str, dict[str, Decimal]], raw: dict[str, dict[str, Decimal]]
) -> dict[str, dict[str, dict[str, str]]]:
    """month → account → {"lines": x, "raw": y} where the two differ (0.00 for a side
    with no row)."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    for month in sorted(set(ledger) | set(raw)):
        a, b = ledger.get(month, {}), raw.get(month, {})
        for account in sorted(set(a) | set(b)):
            x, y = a.get(account, ZERO), b.get(account, ZERO)
            if x != y:
                out.setdefault(month, {})[account or "no account"] = {
                    "lines": f"{x:.2f}",
                    "raw": f"{y:.2f}",
                }
    return out
