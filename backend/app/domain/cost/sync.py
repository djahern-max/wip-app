"""Apply raw QuickBooks versions to ``ledger_line`` (F10; the shape of
``app/domain/billing/sync.py``; D-20, D-48, D-51).

``apply_raw`` takes the **latest** raw version of one Bill, VendorCredit, Purchase,
JournalEntry or Deposit and makes its ``ledger_line`` rows match it: one row per line that
posts to an expense-side account, written in place by the line's QuickBooks id, the lines
the document no longer has deleted (the F05 rule). Idempotent: applying the same version
twice changes nothing. A CDC delete stub or a void sets ``deleted_at`` on every line of the
document; the raw versions stay. A payload the normalizer cannot read is ``skipped`` with
the reason. D-48: a document dated before the history start builds no row.

What the normalizer is told about accounts (``load_lookup``, once per task): the
expense-side QuickBooks account ids (a chart row whose ledger type is Cost of Goods Sold or
Expense, any account whose confirmed ``account_map`` row maps it as job cost or to a cost
category, or a QuickBooks account of those types the chart does not hold), the chart row per
QuickBooks id, each held item's expense
account, and the job per customer id (``job_alias``, system ``qbo_customer``).

Order independence: a line names its customer by QuickBooks id and gets ``job_id`` when the
row is aliased to a job; ``refill_job_ids`` (the cost run after a link or an unlink) fills
and clears ``job_id`` from the aliases in two set-based statements.

Every function requires ``app.tenant_id`` on the session (RLS). Nothing here writes to
QuickBooks.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.domain.billing.sync import SOURCE, Outcome, before_history_start
from app.domain.config.models import AccountMap, GlAccount
from app.domain.cost.models import LedgerLine
from app.domain.jobs.models import JobAlias
from app.ingest.models import RawRecord
from app.ingest.raw import latest_raw_versions
from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.cost import AccountSide, CostDoc, is_cost_entity, normalize_cost

log = logging.getLogger("app.cost")

QBO_CUSTOMER = "qbo_customer"
BEFORE_HISTORY_START = "before_history_start"
# The ledger types that make an account expense-side: the chart's words ("Expenses" in the
# Rye Beach chart, "Expense" in QuickBooks' AccountType). "Other Expense" is below the line
# and outside job cost (D-04), so it is not read.
EXPENSE_SIDE_TYPES: frozenset[str] = frozenset({"Cost of Goods Sold", "Expense", "Expenses"})


@dataclass(frozen=True)
class Lookup:
    side: AccountSide
    gl_account_by_external_id: dict[str, UUID]
    job_by_customer: dict[str, UUID]
    # QuickBooks accounts of an expense-side type that the chart does not hold: id → words.
    not_in_chart: dict[str, str] = field(default_factory=dict)


def _raw_accounts(db: Session, tenant_id: UUID) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for raw in latest_raw_versions(db, tenant_id, SOURCE, "Account"):
        if raw.is_deleted or not isinstance(raw.payload, dict):
            continue
        account_id = raw.payload.get("Id")
        if isinstance(account_id, str) and account_id:
            out[account_id] = raw.payload
    return out


def item_accounts(db: Session, tenant_id: UUID) -> dict[str, str | None]:
    """Each held item's ``ExpenseAccountRef`` (``None``: the item has none)."""
    out: dict[str, str | None] = {}
    for raw in latest_raw_versions(db, tenant_id, SOURCE, "Item"):
        if raw.is_deleted or not isinstance(raw.payload, dict):
            continue
        item_id = raw.payload.get("Id")
        if not isinstance(item_id, str) or not item_id:
            continue
        ref = raw.payload.get("ExpenseAccountRef")
        value = ref.get("value") if isinstance(ref, dict) else None
        out[item_id] = value if isinstance(value, str) and value else None
    return out


def job_by_customer(db: Session) -> dict[str, UUID]:
    return {
        a.external_id: a.job_id
        for a in db.execute(select(JobAlias).where(JobAlias.system == QBO_CUSTOMER)).scalars()
    }


def load_lookup(db: Session, tenant_id: UUID) -> Lookup:
    accounts = {
        a.external_id: a
        for a in db.execute(
            select(GlAccount).where(
                GlAccount.tenant_id == tenant_id, GlAccount.external_id.is_not(None)
            )
        ).scalars()
    }
    # A confirmed mapping that says something about cost: job cost, or a cost category. The
    # Rye Beach rules also confirm every balance-sheet account with ``in_job_cost`` false and
    # no category ("outside income and cost of goods sold"); that row makes no account
    # expense-side, or A/P and accrued payroll would be stored.
    mapped = set(
        db.execute(
            select(AccountMap.gl_account_id).where(
                AccountMap.tenant_id == tenant_id,
                AccountMap.status == "confirmed",
                (AccountMap.in_job_cost.is_(True)) | (AccountMap.cost_category_id.is_not(None)),
            )
        ).scalars()
    )
    expense_side = {
        ext for ext, a in accounts.items() if a.ledger_type in EXPENSE_SIDE_TYPES or a.id in mapped
    }
    not_in_chart: dict[str, str] = {}
    for account_id, payload in _raw_accounts(db, tenant_id).items():
        if account_id in accounts:
            continue
        if payload.get("AccountType") in EXPENSE_SIDE_TYPES and payload.get("Active") is not False:
            expense_side.add(account_id)
            number = payload.get("AcctNum")
            name = payload.get("Name")
            words = " ".join(str(p) for p in (number, name) if isinstance(p, str) and p)
            not_in_chart[account_id] = words or account_id
    return Lookup(
        side=AccountSide(
            expense_side=frozenset(expense_side), item_accounts=item_accounts(db, tenant_id)
        ),
        gl_account_by_external_id={ext: a.id for ext, a in accounts.items()},
        job_by_customer=job_by_customer(db),
        not_in_chart=not_in_chart,
    )


# --- writing one document ---------------------------------------------------------------------


def _existing(
    db: Session, tenant_id: UUID, txn_type: str, external_id: str
) -> dict[str, LedgerLine]:
    rows = db.execute(
        select(LedgerLine).where(
            LedgerLine.tenant_id == tenant_id,
            LedgerLine.source == SOURCE,
            LedgerLine.txn_type == txn_type,
            LedgerLine.txn_external_id == external_id,
        )
    ).scalars()
    return {r.line_external_id: r for r in rows}


def _write(db: Session, tenant_id: UUID, raw: RawRecord, doc: CostDoc, lookup: Lookup) -> int:
    """Rows written in place, missing lines deleted; returns how many rows changed."""
    existing = _existing(db, tenant_id, doc.txn_type, doc.external_id)
    deleted_at = (raw.fetched_at or datetime.now(UTC)) if doc.voided else None
    changed = 0
    seen: set[str] = set()
    for ln in doc.lines:
        seen.add(ln.line_external_id)
        row = existing.get(ln.line_external_id)
        values = {
            "line_no": ln.line_no,
            "txn_date": doc.txn_date,
            "doc_number": doc.doc_number,
            "gl_account_id": (
                lookup.gl_account_by_external_id.get(ln.account_external_id)
                if ln.account_external_id
                else None
            ),
            "account_external_id": ln.account_external_id,
            "vendor_external_id": ln.vendor_external_id,
            "customer_external_id": ln.customer_external_id,
            "job_id": (
                lookup.job_by_customer.get(ln.customer_external_id)
                if ln.customer_external_id
                else None
            ),
            "amount": ln.amount,
            "description": ln.description,
            "memo": doc.memo,
            "deleted_at": deleted_at,
            "raw_record_id": raw.id,
        }
        if row is None:
            db.add(
                LedgerLine(
                    tenant_id=tenant_id,
                    source=SOURCE,
                    txn_type=doc.txn_type,
                    txn_external_id=doc.external_id,
                    line_external_id=ln.line_external_id,
                    **values,
                )
            )
            changed += 1
            continue
        # A void keeps the first time the platform learned of it.
        if row.deleted_at is not None and deleted_at is not None:
            values["deleted_at"] = row.deleted_at
        differs = any(getattr(row, k) != v for k, v in values.items() if k != "raw_record_id")
        if differs:
            changed += 1
        for k, v in values.items():
            setattr(row, k, v)
    gone = [r.id for key, r in existing.items() if key not in seen]
    if gone:
        db.execute(delete(LedgerLine).where(LedgerLine.id.in_(gone)))
        changed += len(gone)
    db.flush()
    return changed


def _mark_deleted(
    db: Session, tenant_id: UUID, raw: RawRecord, *, reason: str = "deleted"
) -> Outcome:
    """The source deleted the document (or re-dated it before the history start, D-48):
    ``deleted_at`` on every line we hold; a stub for a document never held changes
    nothing."""
    when = raw.fetched_at or datetime.now(UTC)
    touched = db.execute(
        update(LedgerLine)
        .where(
            LedgerLine.tenant_id == tenant_id,
            LedgerLine.source == SOURCE,
            LedgerLine.txn_type == raw.entity_type,
            LedgerLine.txn_external_id == raw.external_id,
            LedgerLine.deleted_at.is_(None),
        )
        .values(deleted_at=when, raw_record_id=raw.id)
    ).rowcount
    if touched:
        return Outcome(raw.entity_type, raw.external_id, "deleted", reason)
    return Outcome(raw.entity_type, raw.external_id, "ignored", None)


def apply_raw(
    db: Session,
    tenant_id: UUID,
    raw: RawRecord,
    *,
    history_start: date | None,
    lookup: Lookup | None = None,
) -> Outcome:
    """``raw`` must be the latest version of its record (the caller checks). ``history_start``
    (D-48) is the company's (``None`` in a test that holds everything); a document dated
    before it builds no row. ``lookup`` is built once per task; a caller without one gets
    it read here."""
    if raw.source != SOURCE:
        return Outcome(raw.entity_type, raw.external_id, "ignored", "not_qbo")
    if not is_cost_entity(raw.entity_type):
        return Outcome(raw.entity_type, raw.external_id, "ignored", "not_a_cost_entity")
    if raw.is_deleted:
        return _mark_deleted(db, tenant_id, raw)
    if history_start is not None and before_history_start(raw.payload, history_start):
        outcome = _mark_deleted(db, tenant_id, raw, reason=BEFORE_HISTORY_START)
        if outcome.result == "deleted":
            return outcome
        return Outcome(raw.entity_type, raw.external_id, "skipped", BEFORE_HISTORY_START)
    if lookup is None:
        lookup = load_lookup(db, tenant_id)
    try:
        doc = normalize_cost(raw.entity_type, raw.payload, lookup.side)
    except Unreadable as exc:
        log.info(
            "cost skipped entity=%s reason=%s tenant=%s", raw.entity_type, exc.reason, tenant_id
        )
        return Outcome(raw.entity_type, raw.external_id, "skipped", exc.reason)
    changed = _write(db, tenant_id, raw, doc, lookup)
    return Outcome(
        raw.entity_type, doc.external_id, "applied", "changed" if changed else "unchanged"
    )


# --- after a link or an unlink -------------------------------------------------------------------


def refill_job_ids(db: Session, tenant_id: UUID) -> int:
    """``job_id`` from the aliases, for every line: filled where the line's customer row is
    aliased to a job, cleared where it no longer is. Two statements; returns rows changed."""
    alias_job = (
        select(JobAlias.job_id)
        .where(
            JobAlias.tenant_id == tenant_id,
            JobAlias.system == QBO_CUSTOMER,
            JobAlias.external_id == LedgerLine.customer_external_id,
        )
        .scalar_subquery()
    )
    filled = db.execute(
        update(LedgerLine)
        .where(
            LedgerLine.tenant_id == tenant_id,
            LedgerLine.customer_external_id.is_not(None),
            alias_job.is_not(None),
            (LedgerLine.job_id.is_(None)) | (LedgerLine.job_id != alias_job),
        )
        .values(job_id=alias_job)
    ).rowcount
    cleared = db.execute(
        update(LedgerLine)
        .where(
            LedgerLine.tenant_id == tenant_id,
            LedgerLine.job_id.is_not(None),
            (LedgerLine.customer_external_id.is_(None)) | alias_job.is_(None),
        )
        .values(job_id=None)
    ).rowcount
    return filled + cleared


def apply_everything(db: Session, tenant_id: UUID, *, history_start: date | None) -> dict[str, int]:
    """Every cost document's latest raw version through ``apply_raw`` (the worker's
    start-up run and the run after a backfill or an Item change), then the job ids."""
    lookup = load_lookup(db, tenant_id)
    counts = {"applied": 0, "changed": 0, "skipped": 0, "deleted": 0}
    from app.integrations.qbo.normalize.cost import COST_ENTITIES

    for entity in COST_ENTITIES:
        for raw in latest_raw_versions(db, tenant_id, SOURCE, entity):
            outcome = apply_raw(db, tenant_id, raw, history_start=history_start, lookup=lookup)
            if outcome.result == "applied":
                counts["applied"] += 1
                if outcome.reason == "changed":
                    counts["changed"] += 1
            elif outcome.result in counts:
                counts[outcome.result] += 1
    counts["changed"] += refill_job_ids(db, tenant_id)
    return counts
