"""The reads behind the cost figures (F10): ``ledger_line`` rows as ``LineIn``, the accounts
as ``AccountIn`` from ``gl_account``, the confirmed ``account_map``, the divisions and
categories, and the tenant's WIP basis as ``CostPolicy``; then the pure ``figures``.
Nothing is stored; nothing here writes. Requires ``app.tenant_id`` on the session (RLS).

Volume: a company's cost lines for a year are tens of thousands at most; one read of the
job's lines serves the job page, one read of every line serves the Cost page and the run
(the F08.2 pattern of pushing sums into SQL is kept for the drift check, ``report.py``).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.billing.models import Customer
from app.domain.config.models import AccountMap, CostCategory, Division, GlAccount
from app.domain.config.policy import WIP_BASIS, get_policy
from app.domain.cost import figures
from app.domain.cost.exceptions import division_mismatch_issue, unassigned_issue
from app.domain.cost.figures import (
    NO_ACCOUNT,
    AccountIn,
    CostPolicy,
    CostTieRow,
    JobCost,
    LaborRow,
    LineIn,
    UnassignedDoc,
)
from app.domain.cost.models import LedgerLine
from app.domain.cost.sync import EXPENSE_SIDE_TYPES, SOURCE, _raw_accounts
from app.domain.estimates.exceptions import Issue
from app.domain.jobs.models import Job
from app.domain.jobs.service import tenant_today
from app.ingest.raw import latest_raw_versions


def load_policy(db: Session, tenant_id: UUID) -> CostPolicy:
    from app.domain.config.categories import ensure_cost_categories

    ensure_cost_categories(db, tenant_id)
    cats = db.execute(
        select(CostCategory).where(CostCategory.active).order_by(CostCategory.sort_order)
    ).scalars()
    row = get_policy(db, WIP_BASIS)
    basis = frozenset(str(s) for s in row.value) if row is not None else None
    return CostPolicy(basis=basis, categories=tuple((c.slot, c.name) for c in cats))


def load_accounts(db: Session, tenant_id: UUID) -> dict[str, AccountIn]:
    """Every account a line can name, by QuickBooks id: the chart rows with their confirmed
    mapping, and the QuickBooks accounts the chart does not hold (from the raw Account
    records, for their words)."""
    divisions = {d.id: d for d in db.execute(select(Division)).scalars()}
    categories = {c.id: c for c in db.execute(select(CostCategory)).scalars()}
    maps = {
        m.gl_account_id: m
        for m in db.execute(select(AccountMap).where(AccountMap.status == "confirmed")).scalars()
    }
    out: dict[str, AccountIn] = {}
    for a in db.execute(select(GlAccount).where(GlAccount.external_id.is_not(None))).scalars():
        m = maps.get(a.id)
        division = divisions.get(m.division_id) if m is not None and m.division_id else None
        category = (
            categories.get(m.cost_category_id) if m is not None and m.cost_category_id else None
        )
        out[a.external_id] = AccountIn(
            key=a.external_id,
            gl_account_id=str(a.id),
            account_no=a.account_no,
            name=a.name,
            in_chart=True,
            mapped=m is not None,
            in_job_cost=bool(m is not None and m.in_job_cost),
            slot=category.slot if category else None,
            category_name=category.name if category else None,
            division_id=str(division.id) if division else None,
            division_code=division.code if division else None,
            division_name=division.name if division else None,
        )
    for account_id, payload in _raw_accounts(db, tenant_id).items():
        if account_id in out or payload.get("AccountType") not in EXPENSE_SIDE_TYPES:
            continue
        number, name = payload.get("AcctNum"), payload.get("Name")
        words = " ".join(str(p) for p in (number, name) if isinstance(p, str) and p)
        out[account_id] = AccountIn(
            key=account_id,
            gl_account_id=None,
            account_no=number if isinstance(number, str) else None,
            name=name if isinstance(name, str) else None,
            in_chart=False,
            mapped=False,
            in_job_cost=False,
            slot=None,
            category_name=None,
            division_id=None,
            division_code=None,
            division_name=None,
            quickbooks_words=words or None,
        )
    return out


def _line_in(r: LedgerLine) -> LineIn:
    return LineIn(
        id=str(r.id),
        txn_type=r.txn_type,
        txn_external_id=r.txn_external_id,
        line_external_id=r.line_external_id,
        line_no=r.line_no,
        doc_number=r.doc_number,
        txn_date=r.txn_date,
        account_key=r.account_external_id or NO_ACCOUNT,
        vendor_external_id=r.vendor_external_id,
        customer_external_id=r.customer_external_id,
        job_id=str(r.job_id) if r.job_id else None,
        amount=r.amount,
        description=r.description,
        memo=r.memo,
        deleted=r.deleted_at is not None,
    )


def load_lines(db: Session, *, job_ids: Sequence[UUID] | None = None) -> list[LineIn]:
    """Every line of the tenant, or the lines of the given jobs, in document order."""
    stmt = select(LedgerLine).where(LedgerLine.source == SOURCE)
    if job_ids is not None:
        if not job_ids:
            return []
        stmt = stmt.where(LedgerLine.job_id.in_(list(job_ids)))
    stmt = stmt.order_by(
        LedgerLine.txn_date, LedgerLine.txn_type, LedgerLine.txn_external_id, LedgerLine.line_no
    )
    return [_line_in(r) for r in db.execute(stmt).scalars()]


def lines_held(db: Session) -> int:
    from sqlalchemy import func

    return db.execute(
        select(func.count()).select_from(LedgerLine).where(LedgerLine.deleted_at.is_(None))
    ).scalar_one()


# --- per job -------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobCostView:
    cost: JobCost
    issues: list[Issue] = field(default_factory=list)  # COST_DIVISION_MISMATCH on this job


def job_divisions(db: Session, job_ids: Sequence[UUID]) -> dict[str, tuple[str | None, str]]:
    if not job_ids:
        return {}
    divisions = {d.id: d for d in db.execute(select(Division)).scalars()}
    out: dict[str, tuple[str | None, str]] = {}
    for j in db.execute(select(Job).where(Job.id.in_(list(job_ids)))).scalars():
        d = divisions.get(j.division_id) if j.division_id else None
        out[str(j.id)] = (str(d.id) if d else None, d.name if d else "")
    return out


def job_costs(
    db: Session,
    tenant_id: UUID,
    job_ids: Sequence[UUID],
    *,
    lines: Sequence[LineIn] | None = None,
    accounts: dict[str, AccountIn] | None = None,
    policy: CostPolicy | None = None,
    as_of: date | None = None,
) -> dict[UUID, JobCostView]:
    """Cost to date and the division-mismatch sentences per job, from one read of the jobs'
    lines (or the lines the caller already holds)."""
    if not job_ids:
        return {}
    accounts = accounts if accounts is not None else load_accounts(db, tenant_id)
    policy = policy if policy is not None else load_policy(db, tenant_id)
    as_of = as_of or tenant_today(db)
    lines = lines if lines is not None else load_lines(db, job_ids=job_ids)
    wanted = {str(i) for i in job_ids}
    by_job: dict[str, list[LineIn]] = {}
    for ln in lines:
        if ln.job_id in wanted:
            by_job.setdefault(ln.job_id, []).append(ln)
    mismatches = figures.division_mismatches(lines, accounts, policy, job_divisions(db, job_ids))
    issues: dict[str, list[Issue]] = {}
    for m in mismatches:
        issues.setdefault(m.job_id, []).append(division_mismatch_issue(m))
    return {
        job_id: JobCostView(
            figures.job_cost(by_job.get(str(job_id), ()), accounts, policy, as_of=as_of),
            issues.get(str(job_id), []),
        )
        for job_id in job_ids
    }


# --- the Cost page and the run -------------------------------------------------------------


@dataclass(frozen=True)
class UnassignedView:
    doc: UnassignedDoc
    vendor_name: str | None
    customer_id: str | None  # the copy's row, when held
    customer_name: str | None
    customer_tracked: bool

    @property
    def issue(self) -> Issue:
        return unassigned_issue(
            self.doc, self.customer_name, self.doc.customer_external_id is not None
        )


@dataclass(frozen=True)
class CostReport:
    as_of: date
    policy: CostPolicy
    accounts: dict[str, AccountIn]
    lines: list[LineIn]
    tie_out: list[CostTieRow]
    unassigned: list[UnassignedView]
    labor: list[LaborRow]
    lines_held: int

    @property
    def balanced(self) -> bool:
        return all(r.balanced for r in self.tie_out)


def vendor_names(db: Session, tenant_id: UUID) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in latest_raw_versions(db, tenant_id, SOURCE, "Vendor"):
        if raw.is_deleted or not isinstance(raw.payload, dict):
            continue
        vendor_id, name = raw.payload.get("Id"), raw.payload.get("DisplayName")
        if isinstance(vendor_id, str) and isinstance(name, str):
            out[vendor_id] = name
    return out


def _customers(db: Session, external_ids: set[str]) -> dict[str, Customer]:
    if not external_ids:
        return {}
    return {
        c.external_id: c
        for c in db.execute(
            select(Customer).where(
                Customer.source == SOURCE, Customer.external_id.in_(external_ids)
            )
        ).scalars()
    }


def unassigned_views(
    db: Session,
    tenant_id: UUID,
    lines: Sequence[LineIn],
    accounts: dict[str, AccountIn],
    policy: CostPolicy,
) -> list[UnassignedView]:
    docs = figures.unassigned_documents(lines, accounts, policy)
    customers = _customers(db, {d.customer_external_id for d in docs if d.customer_external_id})
    vendors = vendor_names(db, tenant_id) if docs else {}
    out = []
    for d in docs:
        c = customers.get(d.customer_external_id) if d.customer_external_id else None
        out.append(
            UnassignedView(
                d,
                vendors.get(d.vendor_external_id) if d.vendor_external_id else None,
                str(c.id) if c else None,
                c.display_name if c else None,
                bool(c and c.tracked),
            )
        )
    return out


def cost_report(db: Session, tenant_id: UUID) -> CostReport:
    policy = load_policy(db, tenant_id)
    accounts = load_accounts(db, tenant_id)
    lines = load_lines(db)
    return CostReport(
        as_of=tenant_today(db),
        policy=policy,
        accounts=accounts,
        lines=lines,
        tie_out=figures.tie_out(lines, accounts, policy),
        unassigned=unassigned_views(db, tenant_id, lines, accounts, policy),
        labor=figures.labor_in_ledger(lines, accounts, policy),
        lines_held=lines_held(db),
    )


def money_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.01")), "f")
