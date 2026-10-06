"""The billing figures of one job (F08; BLUEPRINT §8.2 column 10 and the memo columns,
§9 reports 1 and 2; D-02, D-37, D-39). Pure: ``Decimal`` in and out, no database, no
clock (``today`` is an argument). Nothing here is stored; ``board.py`` reads the F05 rows
and calls this.

A job's documents are the ``billing`` rows whose customer row is aliased to the job;
a credit memo is negative (``sign``). Voided and deleted documents and payments are in
no figure and stay in the history rows, marked.

- **Billed to date**: Σ over counted documents of sign × (total − sales tax − fuel
  surcharge lines). A fuel surcharge line is a line whose item id is one of the tenant's
  fuel surcharge items (D-39); sales tax is out (owner, 2026-10-06).
- **Fuel surcharge billed**: Σ of those lines, signed.
- **Deposit invoiced**: Σ billed over the deposit documents: invoices numbered
  ``<estimate number>_DEP`` for an estimate on the job **and** whose sales-item lines,
  other than surcharge lines, are all on a deposit item (D-02; owner's answer 2). One
  part without the other is not a deposit, still counts, and is reported.
- **Deposit received**: Σ applications against the deposit documents.
- **Collected to date**: Σ signed applications against the job's documents, dated by
  the payment, plus applications of the job's own payments that name no document the
  copy holds (owner, 2026-10-06, point 3). A sales receipt's collected side is its
  self-application (F05), so a receipt is billed once and collected once.
- **Open A/R**: Σ sign × balance over counted documents.
- **Unapplied payments**: Σ ``unapplied_amount`` of the job's own payments (D-02).
- **Remaining to bill**: revised contract − billed to date on a fixed-price job.

When either policy key is undecided the figures that need it are ``None``; nothing is
assumed in their place (no key has a default).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

ZERO = Decimal("0.00")
SALES_ITEM = "SalesItemLineDetail"  # QuickBooks' DetailType of a priced line
DEPOSIT_SUFFIX = "_DEP"  # D-26, D-02: <estimate number>_DEP
KIND_LABELS = {"invoice": "Invoice", "credit_memo": "Credit memo", "sales_receipt": "Sales receipt"}
PAYMENT_KIND_LABELS = {"payment": "Payment", "sales_receipt": "Sales receipt"}


def sign(kind: str) -> int:
    return -1 if kind == "credit_memo" else 1


def words(amount: Decimal) -> str:
    """Money as a sentence prints it (D-22): cents, thousands separated, negatives in
    parentheses, zero as 0.00. Digits only."""
    q = amount.quantize(Decimal("0.01"))
    text = f"{abs(q):,.2f}"
    return f"({text})" if q < 0 else text


# --- inputs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LineIn:
    line_kind: str
    item_external_id: str | None
    amount: Decimal
    description: str | None = None


@dataclass(frozen=True)
class DocIn:
    id: str
    kind: str  # invoice | credit_memo | sales_receipt
    external_id: str
    doc_number: str | None
    txn_date: date
    total: Decimal
    tax_total: Decimal
    balance: Decimal
    voided: bool
    deleted: bool
    lines: tuple[LineIn, ...] = ()


@dataclass(frozen=True)
class AppIn:
    """One payment line against a document: the payment it is on, and the link."""

    payment_id: str
    payment_kind: str  # payment | sales_receipt
    payment_external_id: str
    payment_date: date
    payment_total: Decimal
    payment_deleted: bool
    linked_txn_type: str
    billing_id: str | None  # the document, when the copy holds it
    amount: Decimal  # as QuickBooks gives it; a credit-memo application is signed here


@dataclass(frozen=True)
class PaymentIn:
    id: str
    kind: str
    external_id: str
    txn_date: date
    total: Decimal
    unapplied_amount: Decimal
    deleted: bool
    applications: tuple[AppIn, ...] = ()


@dataclass(frozen=True)
class BillingPolicy:
    deposit_items: frozenset[str] | None  # None: not decided
    surcharge_items: frozenset[str] | None  # None: not decided

    @property
    def decided(self) -> bool:
        return self.deposit_items is not None and self.surcharge_items is not None


# --- per document --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DocFigures:
    doc: DocIn
    counted: bool  # not voided, not deleted
    sign: int
    tax: Decimal  # signed; 0.00 when not counted
    surcharge: Decimal | None  # signed; None when the surcharge items are undecided
    billed: Decimal | None  # signed; None when undecided
    balance: Decimal  # signed; 0.00 when not counted
    is_deposit: bool
    deposit_reason: str | None  # why a document that looks like a deposit is not one

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.doc.kind, self.doc.kind)

    @property
    def state_label(self) -> str:
        if self.doc.deleted:
            return "Deleted"
        if self.doc.voided:
            return "Voided"
        return ""


def signed_application(app: AppIn) -> Decimal:
    """A line that applies a credit memo arrives positive and counts against the
    invoice lines (F05, S-01 extra 2)."""
    return -app.amount if app.linked_txn_type == "CreditMemo" else app.amount


def _deposit_parts(
    doc: DocIn, policy: BillingPolicy, estimate_numbers: frozenset[str]
) -> tuple[bool, str | None]:
    """(is the deposit, or why it is not although it looks like one)."""
    number = (doc.doc_number or "").strip()
    dep_number = number.endswith(DEPOSIT_SUFFIX) and len(number) > len(DEPOSIT_SUFFIX)
    estimate = number[: -len(DEPOSIT_SUFFIX)] if dep_number else None
    sales = [ln for ln in doc.lines if ln.line_kind == SALES_ITEM]
    assert policy.deposit_items is not None and policy.surcharge_items is not None
    priced = [ln for ln in sales if ln.item_external_id not in policy.surcharge_items]
    on_deposit_item = bool(priced) and all(
        ln.item_external_id in policy.deposit_items for ln in priced
    )
    any_deposit_item = any(ln.item_external_id in policy.deposit_items for ln in sales)
    if dep_number:
        if doc.kind != "invoice":
            return False, f"it is a {KIND_LABELS[doc.kind].lower()}, and a deposit is an invoice"
        if estimate not in estimate_numbers:
            return False, f"its number names estimate {estimate}, which is not on this job"
        if not on_deposit_item:
            return False, "its number ends in _DEP but its lines are not all on a deposit item"
        return True, None
    if any_deposit_item:
        return False, (
            "its lines are on a deposit item but its number is not "
            f"<estimate number>{DEPOSIT_SUFFIX}"
        )
    return False, None


def document_figures(
    doc: DocIn, policy: BillingPolicy, estimate_numbers: frozenset[str]
) -> DocFigures:
    counted = not (doc.voided or doc.deleted)
    s = sign(doc.kind)
    if not counted:
        return DocFigures(
            doc,
            False,
            s,
            ZERO,
            ZERO if policy.decided else None,
            ZERO if policy.decided else None,
            ZERO,
            False,
            None,
        )
    if not policy.decided:
        return DocFigures(doc, True, s, s * doc.tax_total, None, None, s * doc.balance, False, None)
    assert policy.surcharge_items is not None
    surcharge_lines = sum(
        (ln.amount for ln in doc.lines if ln.item_external_id in policy.surcharge_items), ZERO
    )
    is_deposit, reason = _deposit_parts(doc, policy, estimate_numbers)
    return DocFigures(
        doc=doc,
        counted=True,
        sign=s,
        tax=s * doc.tax_total,
        surcharge=s * surcharge_lines,
        billed=s * (doc.total - doc.tax_total - surcharge_lines),
        balance=s * doc.balance,
        is_deposit=is_deposit,
        deposit_reason=reason,
    )


# --- per job -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentHistoryRow:
    payment_id: str
    kind: str
    external_id: str
    txn_date: date
    total: Decimal
    applied: tuple[tuple[str, Decimal], ...]  # (document number or id, signed amount)
    unapplied: Decimal | None  # None: a payment on another customer row (applied here only)
    on_this_job: bool
    deleted: bool

    @property
    def kind_label(self) -> str:
        return PAYMENT_KIND_LABELS.get(self.kind, self.kind)


@dataclass(frozen=True)
class JobFigures:
    billed_to_date: Decimal | None
    fuel_surcharge_billed: Decimal | None
    deposit_invoiced: Decimal | None
    deposit_received: Decimal | None
    collected_to_date: Decimal
    open_ar: Decimal
    unapplied_payments: Decimal
    unapplied_count: int
    remaining_to_bill: Decimal | None
    tax_billed: Decimal  # signed Σ sales tax over counted documents (the tie-out)
    last_billing_date: date | None
    last_payment_date: date | None
    days_since_activity: int | None
    documents: tuple[DocFigures, ...]  # every document, newest first
    payments: tuple[PaymentHistoryRow, ...]  # newest first
    billed_by_month: dict[str, Decimal] = field(default_factory=dict)  # Σ sign × total
    collected_by_month: dict[str, Decimal] = field(default_factory=dict)

    @property
    def deposit_mismatches(self) -> tuple[DocFigures, ...]:
        return tuple(d for d in self.documents if d.counted and d.deposit_reason)

    @property
    def over_contract(self) -> Decimal | None:
        """By how much billed to date exceeds the revised contract, or None."""
        if self.remaining_to_bill is None or self.remaining_to_bill >= ZERO:
            return None
        return -self.remaining_to_bill


def month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _add(table: dict[str, Decimal], key: str, amount: Decimal) -> None:
    table[key] = table.get(key, ZERO) + amount


def job_figures(
    docs: Sequence[DocIn],
    payments: Sequence[PaymentIn],
    incoming: Sequence[AppIn],
    policy: BillingPolicy,
    *,
    estimate_numbers: frozenset[str] = frozenset(),
    revenue_method: str | None = None,
    revised_contract: Decimal | None = None,
    today: date,
) -> JobFigures:
    """``docs``: the job's documents; ``payments``: the payments on the job's customer
    rows; ``incoming``: every application, from any payment, against one of ``docs``."""
    figures = [document_figures(d, policy, estimate_numbers) for d in docs]
    counted = [f for f in figures if f.counted]
    decided = policy.decided
    deposit_ids = {f.doc.id for f in counted if f.is_deposit}
    live_in = [a for a in incoming if not a.payment_deleted]
    own = [p for p in payments if not p.deleted]
    own_ids = {p.id for p in own}
    loose = [a for p in own for a in p.applications if a.billing_id is None]

    collected = sum((signed_application(a) for a in live_in), ZERO) + sum(
        (signed_application(a) for a in loose), ZERO
    )
    deposit_received = (
        sum((signed_application(a) for a in live_in if a.billing_id in deposit_ids), ZERO)
        if decided
        else None
    )
    billed = sum((f.billed for f in counted), ZERO) if decided else None  # type: ignore[misc]
    surcharge = sum((f.surcharge for f in counted), ZERO) if decided else None  # type: ignore[misc]
    deposit_invoiced = (
        sum((f.billed for f in counted if f.is_deposit), ZERO) if decided else None  # type: ignore[misc]
    )
    unapplied = sum((p.unapplied_amount for p in own), ZERO)
    unapplied_count = sum(1 for p in own if p.unapplied_amount != ZERO)
    remaining = None
    if revenue_method == "fixed_price" and revised_contract is not None and billed is not None:
        remaining = revised_contract - billed

    last_billing = max((f.doc.txn_date for f in counted), default=None)
    payment_dates = [a.payment_date for a in live_in] + [p.txn_date for p in own]
    last_payment = max(payment_dates, default=None)
    last = max((d for d in (last_billing, last_payment) if d is not None), default=None)
    days = (today - last).days if last is not None else None

    billed_by_month: dict[str, Decimal] = {}
    for f in counted:
        _add(billed_by_month, month_key(f.doc.txn_date), f.sign * f.doc.total)
    collected_by_month: dict[str, Decimal] = {}
    for a in live_in:
        _add(collected_by_month, month_key(a.payment_date), signed_application(a))
    for p in own:
        for a in p.applications:
            if a.billing_id is None:
                _add(collected_by_month, month_key(p.txn_date), signed_application(a))
        if p.unapplied_amount != ZERO:
            _add(collected_by_month, month_key(p.txn_date), p.unapplied_amount)

    return JobFigures(
        billed_to_date=billed,
        fuel_surcharge_billed=surcharge,
        deposit_invoiced=deposit_invoiced,
        deposit_received=deposit_received,
        collected_to_date=collected,
        open_ar=sum((f.balance for f in counted), ZERO),
        unapplied_payments=unapplied,
        unapplied_count=unapplied_count,
        remaining_to_bill=remaining,
        tax_billed=sum((f.tax for f in counted), ZERO),
        last_billing_date=last_billing,
        last_payment_date=last_payment,
        days_since_activity=days,
        documents=tuple(
            sorted(figures, key=lambda f: (f.doc.txn_date, f.doc.external_id), reverse=True)
        ),
        payments=_payment_history(payments, incoming, own_ids, {f.doc.id: f for f in figures}),
        billed_by_month=billed_by_month,
        collected_by_month=collected_by_month,
    )


def _payment_history(
    payments: Sequence[PaymentIn],
    incoming: Sequence[AppIn],
    own_ids: set[str],
    docs: dict[str, DocFigures],
) -> tuple[PaymentHistoryRow, ...]:
    def label(a: AppIn) -> str:
        f = docs.get(a.billing_id or "")
        if f is not None:
            return f.doc.doc_number or f.doc.external_id
        return f"{a.linked_txn_type} {a.billing_id or ''}".strip()

    rows: dict[str, PaymentHistoryRow] = {}
    for p in payments:
        here = tuple(
            (label(a), signed_application(a))
            for a in p.applications
            if a.billing_id in docs or a.billing_id is None
        )
        rows[p.id] = PaymentHistoryRow(
            p.id,
            p.kind,
            p.external_id,
            p.txn_date,
            p.total,
            here,
            p.unapplied_amount,
            True,
            p.deleted,
        )
    for a in incoming:
        if a.payment_id in own_ids or a.payment_id in rows:
            continue
        here = tuple(
            (label(b), signed_application(b)) for b in incoming if b.payment_id == a.payment_id
        )
        rows[a.payment_id] = PaymentHistoryRow(
            a.payment_id,
            a.payment_kind,
            a.payment_external_id,
            a.payment_date,
            a.payment_total,
            here,
            None,
            False,
            a.payment_deleted,
        )
    return tuple(sorted(rows.values(), key=lambda r: (r.txn_date, r.external_id), reverse=True))


# --- totals --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Totals:
    billed_to_date: Decimal | None
    fuel_surcharge_billed: Decimal | None
    deposit_invoiced: Decimal | None
    deposit_received: Decimal | None
    collected_to_date: Decimal
    open_ar: Decimal
    unapplied_payments: Decimal
    remaining_to_bill: Decimal | None  # over the jobs that have one; None when none has


def _opt_sum(values: Iterable[Decimal | None]) -> Decimal | None:
    total, seen = ZERO, False
    for v in values:
        if v is None:
            continue
        total += v
        seen = True
    return total if seen else None


def totals(rows: Sequence[JobFigures]) -> Totals:
    return Totals(
        billed_to_date=_opt_sum(r.billed_to_date for r in rows),
        fuel_surcharge_billed=_opt_sum(r.fuel_surcharge_billed for r in rows),
        deposit_invoiced=_opt_sum(r.deposit_invoiced for r in rows),
        deposit_received=_opt_sum(r.deposit_received for r in rows),
        collected_to_date=sum((r.collected_to_date for r in rows), ZERO),
        open_ar=sum((r.open_ar for r in rows), ZERO),
        unapplied_payments=sum((r.unapplied_payments for r in rows), ZERO),
        remaining_to_bill=_opt_sum(r.remaining_to_bill for r in rows),
    )


def months_billed(rows: Sequence[JobFigures]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in rows:
        for m, v in r.billed_by_month.items():
            _add(out, m, v)
    return out


def months_collected(rows: Sequence[JobFigures]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in rows:
        for m, v in r.collected_by_month.items():
            _add(out, m, v)
    return out
