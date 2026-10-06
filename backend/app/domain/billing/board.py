"""The read behind the Sold Jobs Board and the job page's Billing section (F08). Loads
the tenant's F05 rows once (documents, lines, payments, applications), attributes each
to a job through the ``qbo_customer`` aliases F07 writes (D-37: a row with no job,
tracked or not, and a construction job's parent customer, D-35, are "not on a job"),
and hands the pure ``figures`` module the inputs. Nothing is stored; nothing here
writes. Requires ``app.tenant_id`` on the session (RLS)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.billing.figures import (
    AppIn,
    BillingPolicy,
    DocIn,
    JobFigures,
    LineIn,
    PaymentIn,
    job_figures,
    months_billed,
    months_collected,
)
from app.domain.billing.models import Billing, BillingLine, Customer, Payment, PaymentApplication
from app.domain.config.policy import (
    DEPOSIT_IDENTIFICATION,
    FUEL_SURCHARGE_TREATMENT,
    POLICY_KEYS,
    deposit_items,
    surcharge_treatment,
)
from app.domain.jobs.models import JobAlias
from app.domain.jobs.service import QBO, JobView, tenant_today


@dataclass(frozen=True)
class Board:
    per_job: dict[UUID, JobFigures]
    not_on_a_job: JobFigures
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


def load_board(db: Session, tenant_id: UUID, views: Sequence[JobView]) -> Board:
    """Figures for ``views`` and the not-on-a-job totals, from one read of the rows."""
    today = tenant_today(db)
    policy = load_policy(db)

    # customer row → job, through the aliases (every alias, not only the listed jobs',
    # so a document on another job's row never lands in not-on-a-job).
    by_external = {
        a.external_id: a.job_id
        for a in db.execute(select(JobAlias).where(JobAlias.system == QBO)).scalars()
    }
    job_of_customer: dict[UUID, UUID] = {}
    for c in db.execute(select(Customer.id, Customer.external_id)).all():
        if c.external_id in by_external:
            job_of_customer[c.id] = by_external[c.external_id]

    lines: dict[UUID, list[LineIn]] = {}
    for ln in db.execute(select(BillingLine).order_by(BillingLine.line_no)).scalars():
        lines.setdefault(ln.billing_id, []).append(
            LineIn(ln.line_kind, ln.item_external_id, ln.amount, ln.description)
        )
    docs: dict[UUID | None, list[DocIn]] = {}  # job id (None: not on a job) → documents
    doc_job: dict[str, UUID | None] = {}
    for b in db.execute(select(Billing).order_by(Billing.txn_date, Billing.external_id)).scalars():
        job_id = job_of_customer.get(b.customer_id) if b.customer_id else None
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

    payments = {
        p.id: p
        for p in db.execute(
            select(Payment).order_by(Payment.txn_date, Payment.external_id)
        ).scalars()
    }
    apps_of_payment: dict[UUID, list[AppIn]] = {}
    incoming: dict[UUID | None, list[AppIn]] = {}  # job id of the document → applications
    for a in db.execute(select(PaymentApplication).order_by(PaymentApplication.line_no)).scalars():
        p = payments.get(a.payment_id)
        if p is None:
            continue
        app = AppIn(
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
        apps_of_payment.setdefault(p.id, []).append(app)
        if a.billing_id is not None:
            incoming.setdefault(doc_job.get(str(a.billing_id)), []).append(app)
    own: dict[UUID | None, list[PaymentIn]] = {}
    for p in payments.values():
        job_id = job_of_customer.get(p.customer_id) if p.customer_id else None
        own.setdefault(job_id, []).append(
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
    not_on_a_job = job_figures(
        docs.get(None, ()), own.get(None, ()), incoming.get(None, ()), policy, today=today
    )
    return Board(per_job=per_job, not_on_a_job=not_on_a_job, policy=policy, today=today)


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


ZERO = Decimal("0.00")


def tie_out(db: Session, tenant_id: UUID, board: Board) -> list[TieRow]:
    """``board`` must be over every job (no filter), or the sums are short."""
    from app.domain.billing.totals import month_totals

    jobs_b = months_billed(list(board.per_job.values()))
    jobs_c = months_collected(list(board.per_job.values()))
    other_b = board.not_on_a_job.billed_by_month
    other_c = board.not_on_a_job.collected_by_month
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
