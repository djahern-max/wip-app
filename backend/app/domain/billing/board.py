"""The read behind the Sold Jobs Board and the job page's Billing section (F08). Reads
the F05 rows of the listed jobs' own QuickBooks rows (documents by customer, the
applications against those documents, the payments on those customers), attributes
each to its job through the ``qbo_customer`` aliases F07 writes, and hands the pure
``figures`` module the inputs. The "not on a job" totals (D-37: a row with no job,
tracked or not, and a construction job's parent customer, D-35) and every per-month
sum the tie-out compares are summed by the database (F08.2), never by loading the
tenant's rows into Python. Nothing is stored; nothing here writes. Requires
``app.tenant_id`` on the session (RLS)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.domain.billing.figures import (
    CREDIT_TXN_TYPES,
    AppIn,
    BillingPolicy,
    DocIn,
    JobFigures,
    LineIn,
    PaymentIn,
    job_figures,
)
from app.domain.billing.models import Billing, BillingLine, Customer, Payment, PaymentApplication
from app.domain.billing.totals import (
    billing_counted,
    billing_sign,
    decimal,
    month_of,
    month_totals,
)
from app.domain.config.policy import (
    DEPOSIT_IDENTIFICATION,
    FUEL_SURCHARGE_TREATMENT,
    POLICY_KEYS,
    deposit_items,
    surcharge_treatment,
)
from app.domain.jobs.models import JobAlias
from app.domain.jobs.service import QBO, JobView, tenant_today

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class Board:
    per_job: dict[UUID, JobFigures]
    not_on_a_job: JobFigures | None  # None when the caller asked for the jobs only
    policy: BillingPolicy
    today: date

    @property
    def policy_note(self) -> str | None:
        """One sentence when a key the figures need is not decided (nothing is assumed)."""
        missing = []
        if self.policy.deposit_items is None:
            missing.append(POLICY_KEYS[DEPOSIT_IDENTIFICATION].label)
        if self.policy.surcharge_items is None:
            missing.append(POLICY_KEYS[FUEL_SURCHARGE_TREATMENT].label)
        if not missing:
            return None
        keys = " and ".join(missing)
        return (
            f"Billed to date, fuel surcharge billed, the deposit figures and remaining to bill "
            f"wait for the policy {'key' if len(missing) == 1 else 'keys'} {keys} "
            "(Configuration, Policy)."
        )


def load_policy(db: Session) -> BillingPolicy:
    treatment = surcharge_treatment(db)
    return BillingPolicy(
        deposit_items=deposit_items(db),
        surcharge_items=treatment.item_ids if treatment is not None else None,
    )


def _estimate_numbers(view: JobView) -> frozenset[str]:
    return frozenset(a.estimate.external_id for a in view.attached if a.link.role != "ignored")


# --- which rows belong to a job (any job) --------------------------------------------------


def aliased_customer_ids():
    """Customer rows linked to any job (every alias, not only the listed jobs', so a
    document on another job's row never counts as not on a job)."""
    return select(Customer.id).join(
        JobAlias, and_(JobAlias.system == QBO, JobAlias.external_id == Customer.external_id)
    )


def on_a_job(model):
    return model.customer_id.in_(aliased_customer_ids())


def not_on_a_job(model):
    return or_(model.customer_id.is_(None), model.customer_id.not_in(aliased_customer_ids()))


def application_signed():
    """``figures.signed_application`` in SQL: the one constant decides the sign."""
    return case(
        (PaymentApplication.linked_txn_type.in_(CREDIT_TXN_TYPES), -PaymentApplication.amount),
        else_=PaymentApplication.amount,
    )


# --- the listed jobs, from their own rows ---------------------------------------------------


def _job_inputs(
    db: Session, views: Sequence[JobView]
) -> tuple[dict[UUID, list[DocIn]], dict[UUID, list[PaymentIn]], dict[UUID, list[AppIn]]]:
    """Documents, own payments and incoming applications per job, from five reads
    bounded by the listed jobs' customer rows (never by the tenant's history)."""
    job_of_customer: dict[UUID, UUID] = {}
    for v in views:
        for row in v.alias_rows.values():
            job_of_customer[row.id] = v.job.id
    docs: dict[UUID, list[DocIn]] = {}
    own: dict[UUID, list[PaymentIn]] = {}
    incoming: dict[UUID, list[AppIn]] = {}
    if not job_of_customer:
        return docs, own, incoming
    customer_ids = list(job_of_customer)
    doc_ids = select(Billing.id).where(Billing.customer_id.in_(customer_ids))
    payment_ids = select(Payment.id).where(Payment.customer_id.in_(customer_ids))

    lines: dict[UUID, list[LineIn]] = {}
    for ln in db.execute(
        select(BillingLine).where(BillingLine.billing_id.in_(doc_ids)).order_by(BillingLine.line_no)
    ).scalars():
        lines.setdefault(ln.billing_id, []).append(
            LineIn(ln.line_kind, ln.item_external_id, ln.amount, ln.description)
        )
    doc_job: dict[str, UUID] = {}
    for b in db.execute(
        select(Billing)
        .where(Billing.customer_id.in_(customer_ids))
        .order_by(Billing.txn_date, Billing.external_id)
    ).scalars():
        job_id = job_of_customer[b.customer_id]
        doc_job[str(b.id)] = job_id
        docs.setdefault(job_id, []).append(
            DocIn(
                id=str(b.id),
                kind=b.kind,
                external_id=b.external_id,
                doc_number=b.doc_number,
                txn_date=b.txn_date,
                total=b.total,
                tax_total=b.tax_total,
                balance=b.balance,
                voided=b.voided,
                deleted=b.deleted_at is not None,
                lines=tuple(lines.get(b.id, ())),
            )
        )

    def app_in(a: PaymentApplication, p: Payment) -> AppIn:
        return AppIn(
            payment_id=str(p.id),
            payment_kind=p.kind,
            payment_external_id=p.external_id,
            payment_date=p.txn_date,
            payment_total=p.total,
            payment_deleted=p.deleted_at is not None,
            linked_txn_type=a.linked_txn_type,
            billing_id=str(a.billing_id) if a.billing_id else None,
            amount=a.amount,
        )

    # Applications against the jobs' documents, from any payment (the parent's too).
    for a, p in db.execute(
        select(PaymentApplication, Payment)
        .join(Payment, PaymentApplication.payment_id == Payment.id)
        .where(PaymentApplication.billing_id.in_(doc_ids))
        .order_by(Payment.txn_date, Payment.external_id, PaymentApplication.line_no)
    ).all():
        incoming.setdefault(doc_job[str(a.billing_id)], []).append(app_in(a, p))
    # The jobs' own payments with every line they carry.
    payments = {
        p.id: p
        for p in db.execute(
            select(Payment)
            .where(Payment.customer_id.in_(customer_ids))
            .order_by(Payment.txn_date, Payment.external_id)
        ).scalars()
    }
    apps_of_payment: dict[UUID, list[AppIn]] = {}
    for a in db.execute(
        select(PaymentApplication)
        .where(PaymentApplication.payment_id.in_(payment_ids))
        .order_by(PaymentApplication.line_no)
    ).scalars():
        apps_of_payment.setdefault(a.payment_id, []).append(app_in(a, payments[a.payment_id]))
    for p in payments.values():
        own.setdefault(job_of_customer[p.customer_id], []).append(
            PaymentIn(
                id=str(p.id),
                kind=p.kind,
                external_id=p.external_id,
                txn_date=p.txn_date,
                total=p.total,
                unapplied_amount=p.unapplied_amount,
                deleted=p.deleted_at is not None,
                applications=tuple(apps_of_payment.get(p.id, ())),
            )
        )
    return docs, own, incoming


# --- not on a job, summed by the database ----------------------------------------------------


def _incoming_stmt(side, *columns):
    """Over the applications of live payments against the side's documents."""
    return (
        select(*columns)
        .select_from(PaymentApplication)
        .join(Payment, PaymentApplication.payment_id == Payment.id)
        .join(Billing, PaymentApplication.billing_id == Billing.id)
        .where(Payment.deleted_at.is_(None), side(Billing))
    )


def _loose_stmt(side, *columns):
    """Over the applications of the side's live payments naming a document the copy
    does not hold."""
    return (
        select(*columns)
        .select_from(PaymentApplication)
        .join(Payment, PaymentApplication.payment_id == Payment.id)
        .where(PaymentApplication.billing_id.is_(None), Payment.deleted_at.is_(None), side(Payment))
    )


def month_sums(db: Session, side) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """``(billed_by_month, collected_by_month)`` for one side (``on_a_job`` or
    ``not_on_a_job``), the sums ``figures.job_figures`` keeps per job, by the database:
    billed is sign × total over counted documents by document month; collected is the
    signed applications against the side's documents and the loose applications of
    the side's payments, by payment month, plus the payments' unapplied money."""
    billed: dict[str, Decimal] = {}
    month = month_of(Billing.txn_date)
    for m, total in db.execute(
        select(month, func.sum(billing_sign() * Billing.total))
        .where(billing_counted(), side(Billing))
        .group_by(month)
    ).all():
        billed[m] = billed.get(m, ZERO) + decimal(total)
    collected: dict[str, Decimal] = {}
    month = month_of(Payment.txn_date)
    for build in (_incoming_stmt, _loose_stmt):
        for m, total in db.execute(
            build(side, month, func.sum(application_signed())).group_by(month)
        ).all():
            collected[m] = collected.get(m, ZERO) + decimal(total)
    for m, total in db.execute(
        select(month, func.sum(Payment.unapplied_amount))
        .where(Payment.deleted_at.is_(None), Payment.unapplied_amount != 0, side(Payment))
        .group_by(month)
    ).all():
        collected[m] = collected.get(m, ZERO) + decimal(total)
    return billed, collected


def other_figures(db: Session, policy: BillingPolicy, today: date) -> JobFigures:
    """The not-on-a-job row: the same figures ``job_figures`` gives, summed by the
    database over the documents and payments whose customer row is on no job. No
    estimate is on this row, so nothing on it is a deposit (0.00 once the keys are
    decided), and it carries no history rows."""
    side = not_on_a_job
    counted = and_(billing_counted(), side(Billing))
    sign = billing_sign()
    billed_total, tax, open_ar, last_billing = db.execute(
        select(
            func.sum(sign * (Billing.total - Billing.tax_total)),
            func.sum(sign * Billing.tax_total),
            func.sum(sign * Billing.balance),
            func.max(Billing.txn_date),
        ).where(counted)
    ).one()
    surcharge = None
    if policy.decided:
        assert policy.surcharge_items is not None
        surcharge = decimal(
            db.execute(
                select(func.sum(sign * BillingLine.amount))
                .select_from(BillingLine)
                .join(Billing, BillingLine.billing_id == Billing.id)
                .where(counted, BillingLine.item_external_id.in_(policy.surcharge_items))
            ).scalar_one()
        )
    collected = ZERO
    last_incoming = None
    for build in (_incoming_stmt, _loose_stmt):
        total, latest = db.execute(
            build(side, func.sum(application_signed()), func.max(Payment.txn_date))
        ).one()
        collected += decimal(total)
        if latest is not None and (last_incoming is None or latest > last_incoming):
            last_incoming = latest
    unapplied, unapplied_count, last_own = db.execute(
        select(
            func.sum(Payment.unapplied_amount),
            func.count(case((Payment.unapplied_amount != 0, 1))),
            func.max(Payment.txn_date),
        ).where(Payment.deleted_at.is_(None), side(Payment))
    ).one()
    payment_dates = [d for d in (last_incoming, last_own) if d is not None]
    last_payment = max(payment_dates) if payment_dates else None
    last = max((d for d in (last_billing, last_payment) if d is not None), default=None)
    billed_by_month, collected_by_month = month_sums(db, side)
    decided = policy.decided
    return JobFigures(
        billed_to_date=decimal(billed_total) - surcharge if decided else None,
        fuel_surcharge_billed=surcharge,
        deposit_invoiced=ZERO if decided else None,
        deposit_received=ZERO if decided else None,
        collected_to_date=collected,
        open_ar=decimal(open_ar),
        unapplied_payments=decimal(unapplied),
        unapplied_count=int(unapplied_count or 0),
        remaining_to_bill=None,
        tax_billed=decimal(tax),
        last_billing_date=last_billing,
        last_payment_date=last_payment,
        days_since_activity=(today - last).days if last is not None else None,
        documents=(),
        payments=(),
        billed_by_month=billed_by_month,
        collected_by_month=collected_by_month,
    )


# --- the board -------------------------------------------------------------------------------


def load_board(
    db: Session, tenant_id: UUID, views: Sequence[JobView], *, other: bool = True
) -> Board:
    """Figures for ``views`` from their own rows; with ``other``, the not-on-a-job row."""
    today = tenant_today(db)
    policy = load_policy(db)
    docs, own, incoming = _job_inputs(db, views)
    per_job: dict[UUID, JobFigures] = {}
    for v in views:
        per_job[v.job.id] = job_figures(
            docs.get(v.job.id, ()),
            own.get(v.job.id, ()),
            incoming.get(v.job.id, ()),
            policy,
            estimate_numbers=_estimate_numbers(v),
            revenue_method=v.job.revenue_method,
            revised_contract=v.contract.revised_contract,
            today=today,
        )
    return Board(
        per_job=per_job,
        not_on_a_job=other_figures(db, policy, today) if other else None,
        policy=policy,
        today=today,
    )


def money_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.01")), "f")


# --- the tie-out (§8.5 item 1 on the billing side; the XLSX tab; the status on screen) -------


@dataclass(frozen=True)
class TieRow:
    month: str
    jobs_billed: Decimal  # Σ over jobs of sign × total: billed + surcharge + sales tax
    other_billed: Decimal  # not on a job
    ledger_billed: Decimal  # Connections: invoices − credit memos + sales receipts
    jobs_collected: Decimal  # applications by payment month + unapplied, over jobs
    other_collected: Decimal
    ledger_collected: Decimal  # Connections: payments + sales receipts

    @property
    def billed_difference(self) -> Decimal:
        return self.jobs_billed + self.other_billed - self.ledger_billed

    @property
    def collected_difference(self) -> Decimal:
        return self.jobs_collected + self.other_collected - self.ledger_collected

    @property
    def balanced(self) -> bool:
        return self.billed_difference == ZERO and self.collected_difference == ZERO


def tie_out(db: Session, tenant_id: UUID) -> list[TieRow]:
    """Every month, over every job (no filter): three sums by the database on each
    side, and nothing of the board's rows is loaded for it (F08.2)."""
    jobs_b, jobs_c = month_sums(db, on_a_job)
    other_b, other_c = month_sums(db, not_on_a_job)
    ledger = {m.month: m for m in month_totals(db, tenant_id)}
    months = sorted(set(jobs_b) | set(jobs_c) | set(other_b) | set(other_c) | set(ledger))
    rows = []
    for m in months:
        led = ledger.get(m)
        rows.append(
            TieRow(
                month=m,
                jobs_billed=jobs_b.get(m, ZERO),
                other_billed=other_b.get(m, ZERO),
                ledger_billed=(led.invoices + led.credit_memos + led.sales_receipts)
                if led
                else ZERO,
                jobs_collected=jobs_c.get(m, ZERO),
                other_collected=other_c.get(m, ZERO),
                ledger_collected=(led.payments + led.sales_receipts) if led else ZERO,
            )
        )
    return rows


def tie_out_status(rows: Sequence[TieRow]) -> str:
    off = [r.month for r in rows if not r.balanced]
    if not rows:
        return "Nothing to tie out yet: no billing or payments held."
    if not off:
        return f"Ties to the cent to the QuickBooks month totals ({len(rows)} months)."
    return f"Does not tie in {len(off)} of {len(rows)} months: {', '.join(off)}."
