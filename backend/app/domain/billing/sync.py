"""Apply raw QuickBooks versions to the canonical billing rows (F05).

``apply_raw`` takes the **latest** raw version of one record and makes the canonical
rows match it: insert, update in place, or (for a source delete) ``deleted_at``.
Idempotent: applying the same version twice changes nothing. Lines and
applications are replaced wholesale, so a changed payment arriving with fewer
``LinkedTxn`` entries loses the applications it no longer has (owner, 2026-09-21).

Order independence: a document names its customer by external id and gets
``customer_id`` when the customer is there; a customer, once applied, fills the
documents that named it. Likewise ``payment_application.billing_id``.

A payload the normalizer cannot read is skipped (``Outcome.skipped`` with the
reason); the canonical row, if any, keeps pointing at the version it was built from,
which is how the Connections page counts skipped payloads without a counter.

Every function requires ``app.tenant_id`` on the session (RLS).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent, write_tenant_audit
from app.domain.billing.models import (
    BILLING_KIND_BY_ENTITY,
    Billing,
    BillingLine,
    Customer,
    Payment,
    PaymentApplication,
)
from app.domain.config.models import GlAccount
from app.ingest.models import RawRecord
from app.ingest.raw import latest_raw_versions, raw_history
from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.billing import BillingRow, normalize_billing
from app.integrations.qbo.normalize.customer import normalize_customer
from app.integrations.qbo.normalize.money import ZERO
from app.integrations.qbo.normalize.payment import (
    PaymentRow,
    normalize_payment,
    payment_from_sales_receipt,
)

log = logging.getLogger("app.billing")

SOURCE = "qbo"


@dataclass(frozen=True)
class Outcome:
    entity: str
    external_id: str
    result: str  # applied, deleted, skipped, ignored
    reason: str | None = None


# --- helpers ---------------------------------------------------------------------------------


def _customer_id(db: Session, tenant_id: UUID, external_id: str) -> UUID | None:
    return db.execute(
        select(Customer.id).where(
            Customer.tenant_id == tenant_id,
            Customer.source == SOURCE,
            Customer.external_id == external_id,
        )
    ).scalar_one_or_none()


def _billing_id(db: Session, tenant_id: UUID, linked_type: str, external_id: str) -> UUID | None:
    kind = BILLING_KIND_BY_ENTITY.get(linked_type)
    if kind is None:
        return None
    return db.execute(
        select(Billing.id).where(
            Billing.tenant_id == tenant_id,
            Billing.source == SOURCE,
            Billing.kind == kind,
            Billing.external_id == external_id,
        )
    ).scalar_one_or_none()


def _had_nonzero_total_before(history: list[RawRecord], current: RawRecord) -> bool:
    """Any earlier raw version whose ``TotalAmt`` was not zero (the void rule, S-01 (d))."""
    for version in history:
        if version.version >= current.version or not isinstance(version.payload, dict):
            continue
        total = version.payload.get("TotalAmt")
        if isinstance(total, int | Decimal) and not isinstance(total, bool) and total != 0:
            return True
    return False


# --- customer --------------------------------------------------------------------------------


def _apply_customer(db: Session, tenant_id: UUID, raw: RawRecord) -> Outcome:
    row = normalize_customer(raw.payload)
    existing = db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.source == SOURCE,
            Customer.external_id == row.external_id,
        )
    ).scalar_one_or_none()
    parent_id = (
        _customer_id(db, tenant_id, row.parent_external_id) if row.parent_external_id else None
    )
    if existing is None:
        existing = Customer(tenant_id=tenant_id, source=SOURCE, external_id=row.external_id)
    existing.display_name = row.display_name
    existing.parent_external_id = row.parent_external_id
    existing.parent_customer_id = parent_id
    existing.is_project = row.is_project
    existing.active = row.active
    existing.raw_record_id = raw.id
    db.add(existing)
    db.flush()
    # Fill in whoever named this customer before it existed.
    for model in (Customer,):
        db.execute(
            update(model)
            .where(
                model.tenant_id == tenant_id,
                model.source == SOURCE,
                model.parent_external_id == row.external_id,
                model.parent_customer_id.is_(None),
            )
            .values(parent_customer_id=existing.id)
        )
    for model in (Billing, Payment):
        db.execute(
            update(model)
            .where(
                model.tenant_id == tenant_id,
                model.source == SOURCE,
                model.customer_external_id == row.external_id,
                model.customer_id.is_(None),
            )
            .values(customer_id=existing.id)
        )
    return Outcome("Customer", row.external_id, "applied")


# --- billing ---------------------------------------------------------------------------------


def _write_billing(db: Session, tenant_id: UUID, raw: RawRecord, row: BillingRow) -> Billing:
    existing = db.execute(
        select(Billing).where(
            Billing.tenant_id == tenant_id,
            Billing.source == SOURCE,
            Billing.kind == row.kind,
            Billing.external_id == row.external_id,
        )
    ).scalar_one_or_none()
    customer_id = _customer_id(db, tenant_id, row.customer_external_id)
    if existing is None:
        existing = Billing(
            tenant_id=tenant_id, source=SOURCE, kind=row.kind, external_id=row.external_id
        )
    existing.doc_number = row.doc_number
    existing.txn_date = row.txn_date
    existing.due_date = row.due_date
    existing.customer_external_id = row.customer_external_id
    existing.customer_id = customer_id
    existing.subtotal = row.subtotal
    existing.discount_total = row.discount_total
    existing.tax_total = row.tax_total
    existing.total = row.total
    existing.balance = row.balance
    existing.voided = row.voided
    existing.deleted_at = None
    existing.raw_record_id = raw.id
    db.add(existing)
    db.flush()
    db.execute(
        delete(BillingLine).where(
            BillingLine.tenant_id == tenant_id, BillingLine.billing_id == existing.id
        )
    )
    for line in row.lines:
        db.add(
            BillingLine(
                tenant_id=tenant_id,
                billing_id=existing.id,
                line_no=line.line_no,
                external_line_id=line.external_line_id,
                line_kind=line.line_kind,
                description=line.description,
                item_external_id=line.item_external_id,
                amount=line.amount,
            )
        )
    entity = next(e for e, k in BILLING_KIND_BY_ENTITY.items() if k == row.kind)
    db.execute(
        update(PaymentApplication)
        .where(
            PaymentApplication.tenant_id == tenant_id,
            PaymentApplication.linked_txn_type == entity,
            PaymentApplication.linked_txn_external_id == row.external_id,
            PaymentApplication.billing_id.is_(None),
        )
        .values(billing_id=existing.id)
    )
    db.flush()
    return existing


def _write_payment(db: Session, tenant_id: UUID, raw: RawRecord, row: PaymentRow) -> Payment:
    existing = db.execute(
        select(Payment).where(
            Payment.tenant_id == tenant_id,
            Payment.source == SOURCE,
            Payment.kind == row.kind,
            Payment.external_id == row.external_id,
        )
    ).scalar_one_or_none()
    customer_id = _customer_id(db, tenant_id, row.customer_external_id)
    if existing is None:
        existing = Payment(
            tenant_id=tenant_id, source=SOURCE, kind=row.kind, external_id=row.external_id
        )
    existing.txn_date = row.txn_date
    existing.customer_external_id = row.customer_external_id
    existing.customer_id = customer_id
    existing.total = row.total
    existing.unapplied_amount = row.unapplied_amount
    existing.deposit_account_external_id = row.deposit_account_external_id
    existing.deleted_at = None
    existing.raw_record_id = raw.id
    db.add(existing)
    db.flush()
    db.execute(
        delete(PaymentApplication).where(
            PaymentApplication.tenant_id == tenant_id,
            PaymentApplication.payment_id == existing.id,
        )
    )
    resolved = [
        _billing_id(db, tenant_id, app_.linked_txn_type, app_.linked_txn_external_id)
        for app_ in row.applications
    ]
    for app_, billing_id in zip(row.applications, resolved, strict=True):
        db.add(
            PaymentApplication(
                tenant_id=tenant_id,
                payment_id=existing.id,
                line_no=app_.line_no,
                linked_txn_type=app_.linked_txn_type,
                linked_txn_external_id=app_.linked_txn_external_id,
                billing_id=billing_id,
                amount=app_.amount,
            )
        )
    db.flush()
    return existing


def _apply_billing(db: Session, tenant_id: UUID, raw: RawRecord) -> Outcome:
    history = raw_history(db, tenant_id, SOURCE, raw.entity_type, raw.external_id)
    row = normalize_billing(
        raw.entity_type,
        raw.payload,
        had_nonzero_total_before=_had_nonzero_total_before(history, raw),
    )
    _write_billing(db, tenant_id, raw, row)
    if row.kind == "sales_receipt":
        _write_payment(db, tenant_id, raw, payment_from_sales_receipt(row))
    return Outcome(raw.entity_type, row.external_id, "applied")


def _apply_payment(db: Session, tenant_id: UUID, raw: RawRecord) -> Outcome:
    row = normalize_payment(raw.payload)
    _write_payment(db, tenant_id, raw, row)
    return Outcome("Payment", row.external_id, "applied")


# --- deletes ---------------------------------------------------------------------------------


def _apply_delete(db: Session, tenant_id: UUID, raw: RawRecord) -> Outcome:
    """The source deleted the record: ``deleted_at`` on the rows we hold (a stub for a
    record never held changes nothing). ``deleted_at`` is when the platform learned
    of it; the stub says no more than that."""
    when = raw.fetched_at or datetime.now(UTC)
    touched = 0
    kind = BILLING_KIND_BY_ENTITY.get(raw.entity_type)
    if kind is not None:
        touched += db.execute(
            update(Billing)
            .where(
                Billing.tenant_id == tenant_id,
                Billing.source == SOURCE,
                Billing.kind == kind,
                Billing.external_id == raw.external_id,
            )
            .values(deleted_at=when, raw_record_id=raw.id)
        ).rowcount
    payment_kind = {"Payment": "payment", "SalesReceipt": "sales_receipt"}.get(raw.entity_type)
    if payment_kind is not None:
        touched += db.execute(
            update(Payment)
            .where(
                Payment.tenant_id == tenant_id,
                Payment.source == SOURCE,
                Payment.kind == payment_kind,
                Payment.external_id == raw.external_id,
            )
            .values(deleted_at=when, raw_record_id=raw.id)
        ).rowcount
    return Outcome(raw.entity_type, raw.external_id, "deleted" if touched else "ignored")


# --- entry point ---------------------------------------------------------------------------


def apply_raw(db: Session, tenant_id: UUID, raw: RawRecord) -> Outcome:
    """``raw`` must be the latest version of its record (the caller checks; an older
    version is ignored so a late task can never overwrite a newer one)."""
    if raw.source != SOURCE:
        return Outcome(raw.entity_type, raw.external_id, "ignored", "not_qbo")
    if raw.is_deleted:
        return _apply_delete(db, tenant_id, raw)
    try:
        if raw.entity_type == "Customer":
            return _apply_customer(db, tenant_id, raw)
        if raw.entity_type in BILLING_KIND_BY_ENTITY:
            return _apply_billing(db, tenant_id, raw)
        if raw.entity_type == "Payment":
            return _apply_payment(db, tenant_id, raw)
    except Unreadable as exc:
        log.info(
            "qbo skipped entity=%s reason=%s tenant=%s", raw.entity_type, exc.reason, tenant_id
        )
        return Outcome(raw.entity_type, raw.external_id, "skipped", exc.reason)
    return Outcome(raw.entity_type, raw.external_id, "ignored", "not_normalized")


# --- accounts ------------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountAttach:
    attached: int  # gl_account rows given (or already holding) a QuickBooks id
    changed: int  # rows whose external_id changed in this run
    without_number: int  # QuickBooks accounts with no AcctNum (stored raw only)
    duplicate_numbers: int  # AcctNum values held by more than one QuickBooks account
    unmatched: int  # numbered QuickBooks accounts with no gl_account of that number


@dataclass(frozen=True)
class AccountNumbers:
    """Active QuickBooks accounts only: an inactive account is not on the chart."""

    total: int
    without_number: int
    duplicate_numbers: int
    by_number: dict[str, list[str]]  # AcctNum → QuickBooks ids holding it


def account_number_summary(db: Session, tenant_id: UUID) -> AccountNumbers:
    """The account numbers QuickBooks gives, from the latest raw Account versions."""
    by_number: dict[str, list[str]] = {}
    without = total = 0
    for raw in latest_raw_versions(db, tenant_id, SOURCE, "Account"):
        if raw.is_deleted or not isinstance(raw.payload, dict):
            continue
        if raw.payload.get("Active") is False:
            continue
        total += 1
        number = raw.payload.get("AcctNum")
        if not isinstance(number, str) or not number.strip():
            without += 1
            continue
        by_number.setdefault(number.strip(), []).append(str(raw.payload.get("Id")))
    duplicates = sum(1 for ids in by_number.values() if len(ids) > 1)
    return AccountNumbers(total, without, duplicates, by_number)


@dataclass(frozen=True)
class ChartMatch:
    """How the numbered QuickBooks accounts and the tenant's chart line up (read only;
    what the Connections page reports). Numbers are identifiers, so they may be named."""

    numbers: AccountNumbers
    attached: list[str]  # numbers held by a gl_account row with the QuickBooks id
    unmatched: list[str]  # numbered in QuickBooks, no gl_account of that number
    chart_only: list[str]  # gl_account rows (active) whose number QuickBooks does not use


def chart_match(db: Session, tenant_id: UUID) -> ChartMatch:
    numbers = account_number_summary(db, tenant_id)
    accounts = {
        a.account_no: a
        for a in db.execute(select(GlAccount).where(GlAccount.tenant_id == tenant_id)).scalars()
    }
    attached = [
        n
        for n, ids in numbers.by_number.items()
        if len(ids) == 1 and n in accounts and accounts[n].external_id == ids[0]
    ]
    unmatched = [n for n, ids in numbers.by_number.items() if len(ids) == 1 and n not in accounts]
    chart_only = [n for n, a in accounts.items() if a.active and n not in numbers.by_number]
    return ChartMatch(numbers, sorted(attached), sorted(unmatched), sorted(chart_only))


def attach_account_ids(db: Session, tenant_id: UUID) -> AccountAttach:
    """``Account.Id`` → ``gl_account.external_id`` by account number (owner answer 10).
    No number: counted, no row. A number on two QuickBooks accounts: neither is
    attached. Nothing else on ``gl_account`` is touched; each change is audited."""
    numbers = account_number_summary(db, tenant_id)
    accounts = {
        a.account_no: a
        for a in db.execute(select(GlAccount).where(GlAccount.tenant_id == tenant_id)).scalars()
    }
    attached = changed = unmatched = 0
    for number, ids in numbers.by_number.items():
        if len(ids) > 1:
            continue
        account = accounts.get(number)
        if account is None:
            unmatched += 1
            continue
        attached += 1
        if account.external_id != ids[0]:
            previous = account.external_id
            account.external_id = ids[0]
            changed += 1
            write_tenant_audit(
                db,
                tenant_id=tenant_id,
                action=TenantEvent.gl_account_linked,
                entity_type="gl_account",
                entity_id=account.id,
                actor_user_id=None,
                actor_role=None,
                detail={"source": SOURCE, "had_external_id": previous is not None},
            )
    db.flush()
    return AccountAttach(
        attached, changed, numbers.without_number, numbers.duplicate_numbers, unmatched
    )


def unlinked_deposit_lines(db: Session, tenant_id: UUID) -> tuple[int, Decimal]:
    """Deposit lines with no ``LinkedTxn`` (owner answer 4): money received without a
    document. Count and total, from the latest raw versions; nothing is normalized."""
    count, total = 0, ZERO
    for raw in latest_raw_versions(db, tenant_id, SOURCE, "Deposit"):
        if raw.is_deleted or not isinstance(raw.payload, dict):
            continue
        for line in raw.payload.get("Line") or []:
            if isinstance(line, dict) and not line.get("LinkedTxn"):
                amount = line.get("Amount")
                if isinstance(amount, bool) or not isinstance(amount, int | Decimal):
                    continue
                count += 1
                total += amount
    return count, total
