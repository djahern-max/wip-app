"""Reads and write actions for jobs and the crosswalk (F07). Every function requires
``app.tenant_id`` on the session (RLS). Reads write nothing. Each write action is one
person's decision: it writes the rows it names, by id, and exactly one ``audit_log``
row naming the rows it touched (owner's answer 14). Names only ever reach the pure
suggestion functions; no write path compares a name.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.core.audit import TenantEvent
from app.domain.billing.models import Billing, Customer, Payment
from app.domain.billing.sync import SOURCE as QBO_SOURCE
from app.domain.config.audit import Actor, audit
from app.domain.config.models import Division
from app.domain.config.policy import (
    CHANGE_ORDER_EVIDENCE,
    POLICY_KEYS,
    TIMEZONE,
    evidence_required,
    get_policy,
)
from app.domain.estimates.exceptions import Issue
from app.domain.estimates.models import (
    STATUS_LABELS,
    WORK_AREA_KINDS,
    Estimate,
    EstimateVersion,
    EstimateWorkArea,
)
from app.domain.estimates.service import (
    EstimateView,
    Grid,
    estimate_burden,
    load_grid,
    load_views,
)
from app.domain.estimates.totals import with_burden
from app.domain.estimates.versions import same_name
from app.domain.jobs.contract import (
    ApprovalIn,
    ApprovalState,
    AreaIn,
    AttachedIn,
    Contract,
    RowIn,
    VersionIn,
    approval_state,
    job_contract,
)
from app.domain.jobs.duplicates import NamedRow, duplicate_pairs
from app.domain.jobs.issues import (
    JobState,
    LedgerRow,
    approval_not_carried_issue,
    job_issues,
    ledger_issue,
    ledger_issues,
    second_estimate_issue,
    unattached_issue,
)
from app.domain.jobs.models import (
    JOB_STATUSES,
    NO_ESTIMATE_METHODS,
    REVENUE_METHODS,
    ROLES,
    ChangeOrderApproval,
    Job,
    JobAlias,
    JobEstimate,
)
from app.domain.jobs.suggest import (
    AttachCandidate,
    CustomerRow,
    EstimateText,
    JobForMatch,
    QboCandidate,
    attach_candidates,
    qbo_candidates,
    suggest_division,
    suggested_kind,
)
from app.tenancy.models import User

LMN = "lmn_estimate"
QBO = "qbo_customer"
SEARCH_LIMIT = 25
PAGE_SIZE = 50  # F07.2: the picker's search, one page
LISTED_STATUSES = frozenset({"sold", "in_progress", "substantially_complete"})  # F07.4: the list


class JobError(Exception):
    """One sentence for the person (D-22) and the HTTP status the API answers with."""

    status = 422


class NotFound(JobError):
    status = 404


class Conflict(JobError):
    status = 409


class Invalid(JobError):
    status = 422


# --- small reads ---------------------------------------------------------------------


def tenant_today(db: Session) -> date:
    """Today in the tenant's time zone when the policy is set, else in UTC."""
    tz = get_policy(db, TIMEZONE)
    now = datetime.now(UTC)
    return now.astimezone(ZoneInfo(str(tz.value))).date() if tz is not None else now.date()


def _local_date(db: Session, moment: datetime) -> date:
    tz = get_policy(db, TIMEZONE)
    return moment.astimezone(ZoneInfo(str(tz.value))).date() if tz is not None else moment.date()


def _customer_row(c: Customer, names: dict[UUID, str]) -> CustomerRow:
    return CustomerRow(
        c.id,
        c.external_id,
        c.display_name,
        c.parent_customer_id,
        names.get(c.parent_customer_id) if c.parent_customer_id else None,
        c.is_project,
        c.active,
        c.tracked_at is not None,
    )


def customer_rows(db: Session) -> list[CustomerRow]:
    customers = list(db.execute(select(Customer).where(Customer.source == QBO_SOURCE)).scalars())
    names = {c.id: c.display_name for c in customers}
    return [_customer_row(c, names) for c in customers]


def _qbo_aliased(db: Session) -> dict[str, UUID]:
    return {
        a.external_id: a.job_id
        for a in db.execute(select(JobAlias).where(JobAlias.system == QBO)).scalars()
    }


def _user_names(db: Session, ids: set[UUID | None]) -> dict[UUID, str]:
    wanted = [i for i in ids if i is not None]
    if not wanted:
        return {}
    return {
        u.id: u.display_name for u in db.execute(select(User).where(User.id.in_(wanted))).scalars()
    }


def _text(e: Estimate) -> EstimateText:
    return EstimateText(e.external_id, e.client_name, e.jobsite, e.name)


def estimate_eac(db: Session, view: EstimateView, grid: Grid) -> Decimal | None:
    """The estimate's EAC in the WIP basis as its detail shows it (F06.1: burdened);
    None when it is not computed (no work areas, basis not decided, no rate)."""
    if view.work_areas is None:
        return None
    burden = estimate_burden(db, view, grid)
    return with_burden(view.totals(grid), grid.basis, burden.result.total).eac_in_basis


def _areas(
    view: EstimateView, approvals: dict[tuple[UUID, int], "ApprovalView"] | None = None
) -> tuple[AreaIn, ...] | None:
    if view.work_areas is None:
        return None
    approvals = approvals or {}
    eid = view.estimate.id
    return tuple(
        AreaIn(
            w.row.order_no,
            w.row.kept,
            w.row.price,
            w.row.kind,
            approved=(eid, w.row.order_no) in approvals
            and approvals[(eid, w.row.order_no)].applies,
        )
        for w in view.work_areas
    )


# --- F07.4 (D-42): approvals, read against the versions after them ---------------------------


@dataclass
class ApprovalView:
    """One work area's approval state on read: the latest event for
    ``(estimate_id, order_no)`` and, for an approval, whether it still applies (rule C)."""

    latest: ChangeOrderApproval
    state: ApprovalState | None  # None when the latest event is a withdrawal

    @property
    def applies(self) -> bool:
        return self.latest.action == "approved" and self.state is not None and self.state.applies

    @property
    def ended(self) -> str | None:
        """Why an approval no longer applies; None while it does or after a withdrawal."""
        if self.latest.action != "approved" or self.state is None or self.state.applies:
            return None
        return self.state.ended


ApprovalKey = tuple[UUID, int]


def _approval_events(db: Session, job_ids: Sequence[UUID]) -> list[ChangeOrderApproval]:
    if not job_ids:
        return []
    return list(
        db.execute(
            select(ChangeOrderApproval)
            .where(ChangeOrderApproval.job_id.in_(job_ids))
            .order_by(ChangeOrderApproval.recorded_at, ChangeOrderApproval.id)
        ).scalars()
    )


def _later_versions(db: Session, after: dict[UUID, int]) -> dict[UUID, list[VersionIn]]:
    """Per estimate, the versions with work areas received after version ``after[eid]``,
    in order, as the pure chain reads them."""
    if not after:
        return {}
    versions = list(
        db.execute(
            select(EstimateVersion)
            .where(
                EstimateVersion.estimate_id.in_(list(after)),
                EstimateVersion.work_areas_raw_record_id.is_not(None),
            )
            .order_by(EstimateVersion.estimate_id, EstimateVersion.version_no)
        ).scalars()
    )
    versions = [v for v in versions if v.version_no > after[v.estimate_id]]
    rows_by_version: dict[UUID, list[RowIn]] = {v.id: [] for v in versions}
    if versions:
        for w in db.execute(
            select(EstimateWorkArea)
            .where(EstimateWorkArea.estimate_version_id.in_(list(rows_by_version)))
            .order_by(EstimateWorkArea.order_no)
        ).scalars():
            rows_by_version[w.estimate_version_id].append(
                RowIn(w.order_no, w.name, w.kept, w.price, w.kind)
            )
    out: dict[UUID, list[VersionIn]] = {}
    for v in versions:
        out.setdefault(v.estimate_id, []).append(
            VersionIn(v.version_no, tuple(rows_by_version[v.id]))
        )
    return out


def _approvals(
    db: Session, job_ids: Sequence[UUID], roles: dict[UUID, str]
) -> tuple[dict[UUID, dict[ApprovalKey, ApprovalView]], dict[UUID, list[ChangeOrderApproval]]]:
    """Per job: the approval state per ``(estimate_id, order_no)`` and the history, newest
    first. ``roles``: estimate id → its role on the job. Three reads for every job listed."""
    events = _approval_events(db, job_ids)
    if not events:
        return {}, {}
    latest: dict[tuple[UUID, ApprovalKey], ChangeOrderApproval] = {}
    for e in events:
        latest[(e.job_id, (e.estimate_id, e.order_no))] = e
    approved = [e for e in latest.values() if e.action == "approved"]
    version_of: dict[UUID, int] = {}
    if approved:
        version_of = dict(
            db.execute(
                select(EstimateWorkArea.id, EstimateVersion.version_no)
                .join(EstimateVersion, EstimateVersion.id == EstimateWorkArea.estimate_version_id)
                .where(EstimateWorkArea.id.in_([e.estimate_work_area_id for e in approved]))
            ).all()
        )
    after: dict[UUID, int] = {}
    for e in approved:
        v = version_of.get(e.estimate_work_area_id)
        if v is not None:
            after[e.estimate_id] = min(after.get(e.estimate_id, v), v)
    later = _later_versions(db, after)
    views: dict[UUID, dict[ApprovalKey, ApprovalView]] = {}
    for (job_id, key), e in latest.items():
        state = None
        if e.action == "approved":
            made_on = version_of.get(e.estimate_work_area_id, 0)
            chain = [v for v in later.get(e.estimate_id, []) if v.version_no > made_on]
            state = approval_state(
                ApprovalIn(e.order_no, e.work_area_name, e.price, made_on),
                chain,
                roles.get(e.estimate_id, "original"),
            )
        views.setdefault(job_id, {})[key] = ApprovalView(e, state)
    history: dict[UUID, list[ChangeOrderApproval]] = {}
    for e in reversed(events):
        history.setdefault(e.job_id, []).append(e)
    return views, history


def _approval_issues(
    approvals: dict[ApprovalKey, ApprovalView],
    external_ids: dict[UUID, str],
    users: dict[UUID, str],
) -> list[Issue]:
    """One ``CO_APPROVAL_NOT_CARRIED`` sentence per approval a later version ended that no
    one has withdrawn or replaced."""
    out: list[Issue] = []
    for (eid, _order), view in sorted(approvals.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        ended = view.ended
        if ended is None:
            continue
        e = view.latest
        out.append(
            approval_not_carried_issue(
                order_no=e.order_no,
                name=e.work_area_name,
                estimate=external_ids.get(eid, ""),
                agreed_on=e.agreed_on or e.recorded_at.date(),
                approved=e.price,
                who=users.get(e.recorded_by, "a user"),
                ended=ended,
                approval_id=e.id,
            )
        )
    return out


# --- job views -----------------------------------------------------------------------


@dataclass
class AttachedView:
    link: JobEstimate
    estimate: Estimate
    view: EstimateView
    eac: Decimal | None


@dataclass
class JobView:
    job: Job
    customer: Customer | None
    division: Division | None
    attached: list[AttachedView]
    aliases: list[JobAlias]
    alias_rows: dict[str, CustomerRow]
    contract: Contract
    issues: list[Issue]
    users: dict[UUID, str] = field(default_factory=dict)
    sold_on_set_by_person: bool = False  # F07.1: a ``job_updated`` row names ``sold_on``
    # F07.4 (D-42): per (estimate id, order number), the approval state; the history, newest first.
    approvals: dict[ApprovalKey, ApprovalView] = field(default_factory=dict)
    approval_history: list[ChangeOrderApproval] = field(default_factory=list)

    def approval_for(self, estimate_id: UUID, order_no: int) -> ApprovalView | None:
        return self.approvals.get((estimate_id, order_no))

    @property
    def original(self) -> AttachedView | None:
        return next((a for a in self.attached if a.link.role == "original"), None)

    @property
    def sold_on_set_when_created(self) -> bool:
        """Owner's answer 17: the date did not come from the original estimate; F07.1
        (owner, 2026-10-01): and no person has set it since, which the audit log records."""
        if self.sold_on_set_by_person:
            return False
        o = self.original
        return o is None or o.estimate.estimate_date != self.job.sold_on

    @property
    def qbo_aliases(self) -> list[JobAlias]:
        return [a for a in self.aliases if a.system == QBO]


def _sold_on_set_by_person(db: Session, job_ids: Sequence[UUID]) -> set[str]:
    """F07.1: the jobs whose ``sold_on`` a person has set (``PATCH``), read from the
    append-only audit log: a ``job_updated`` row with ``sold_on`` among its changed
    fields. No column is added for it (owner, 2026-10-01)."""
    return set(
        db.execute(
            select(AuditLog.entity_id).where(
                AuditLog.entity_type == "job",
                AuditLog.action == TenantEvent.job_updated,
                AuditLog.entity_id.in_([str(i) for i in job_ids]),
                AuditLog.detail.contains({"changed_fields": ["sold_on"]}),
            )
        ).scalars()
    )


def load_job_views(db: Session, tenant_id: UUID, jobs: Sequence[Job]) -> list[JobView]:
    if not jobs:
        return []
    grid = load_grid(db, tenant_id)
    ids = [j.id for j in jobs]
    set_by_person = _sold_on_set_by_person(db, ids)
    links = list(
        db.execute(
            select(JobEstimate).where(JobEstimate.job_id.in_(ids)).order_by(JobEstimate.attached_at)
        ).scalars()
    )
    estimates = (
        {
            e.id: e
            for e in db.execute(
                select(Estimate).where(Estimate.id.in_([ln.estimate_id for ln in links]))
            ).scalars()
        }
        if links
        else {}
    )
    views = {v.estimate.id: v for v in load_views(db, list(estimates.values()), grid)}
    aliases = list(
        db.execute(
            select(JobAlias).where(JobAlias.job_id.in_(ids)).order_by(JobAlias.linked_at)
        ).scalars()
    )
    rows = {r.external_id: r for r in customer_rows(db)}
    customers = {
        c.id: c
        for c in db.execute(
            select(Customer).where(Customer.id.in_([j.customer_id for j in jobs if j.customer_id]))
        ).scalars()
    }
    divisions = {d.id: d for d in db.execute(select(Division)).scalars()}
    roles = {ln.estimate_id: ln.role for ln in links}
    approvals_by_job, history_by_job = _approvals(db, ids, roles)
    external_ids = {e.id: e.external_id for e in estimates.values()}
    users = _user_names(
        db,
        {j.created_by for j in jobs}
        | {ln.attached_by for ln in links}
        | {a.linked_by for a in aliases}
        | {e.recorded_by for h in history_by_job.values() for e in h},
    )
    out: list[JobView] = []
    for job in jobs:
        approvals = approvals_by_job.get(job.id, {})
        attached = [
            AttachedView(
                ln,
                estimates[ln.estimate_id],
                views[ln.estimate_id],
                None if ln.role == "ignored" else estimate_eac(db, views[ln.estimate_id], grid),
            )
            for ln in links
            if ln.job_id == job.id
        ]
        job_aliases = [a for a in aliases if a.job_id == job.id]
        contract = job_contract(
            job.revenue_method,
            [
                AttachedIn(a.link.role, a.estimate.price, _areas(a.view, approvals), a.eac)
                for a in attached
            ],
        )
        state = JobState(
            job.id,
            job.name,
            job.status,
            job.division_id,
            sum(1 for a in job_aliases if a.system == QBO),
        )
        out.append(
            JobView(
                job=job,
                customer=customers.get(job.customer_id) if job.customer_id else None,
                division=divisions.get(job.division_id) if job.division_id else None,
                attached=attached,
                aliases=job_aliases,
                alias_rows={
                    a.external_id: rows[a.external_id]
                    for a in job_aliases
                    if a.external_id in rows and a.system == QBO
                },
                contract=contract,
                issues=[*job_issues(state), *_approval_issues(approvals, external_ids, users)],
                users=users,
                sold_on_set_by_person=str(job.id) in set_by_person,
                approvals=approvals,
                approval_history=history_by_job.get(job.id, []),
            )
        )
    return out


def list_jobs(
    db: Session,
    tenant_id: UUID,
    *,
    status: str | None = None,
    division_id: UUID | None = None,
    revenue_method: str | None = None,
    no_link: bool = False,
) -> list[JobView]:
    stmt = select(Job).order_by(func.lower(Job.name), Job.created_at)
    if status:
        stmt = stmt.where(Job.status == status)
    if division_id:
        stmt = stmt.where(Job.division_id == division_id)
    if revenue_method:
        stmt = stmt.where(Job.revenue_method == revenue_method)
    views = load_job_views(db, tenant_id, list(db.execute(stmt).scalars()))
    if no_link:
        views = [v for v in views if not v.qbo_aliases]
    return views


def job_detail(db: Session, tenant_id: UUID, job_id: UUID) -> JobView:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("That job does not exist.")
    return load_job_views(db, tenant_id, [job])[0]


def _ledger_row(r: CustomerRow, documents: int, aliased: bool) -> LedgerRow:
    return LedgerRow(
        r.id, r.external_id, r.display_name, r.kind_label, documents, aliased, r.tracked, r.active
    )


def ledger_items(db: Session) -> list[Issue]:
    """LEDGER_PROJECT_NO_JOB (D-37): every tracked, active row with no alias, with or
    without documents; a row nobody picked raises nothing."""
    aliased = _qbo_aliased(db)
    tracked = [r for r in customer_rows(db) if r.tracked and r.active]
    counts = _document_counts(db, [r.id for r in tracked])
    rows = [
        _ledger_row(r, sum(counts.get(r.id, (0, 0))), r.external_id in aliased) for r in tracked
    ]
    return ledger_issues(sorted(rows, key=lambda r: r.display_name.casefold()))


def _document_counts(db: Session, ids: Sequence[UUID] | None = None) -> dict[UUID, tuple[int, int]]:
    if ids is not None and len(ids) == 0:
        return {}

    def counted(model):
        stmt = (
            select(model.customer_id, func.count())
            .where(model.customer_id.is_not(None))
            .group_by(model.customer_id)
        )
        if ids is not None:
            stmt = stmt.where(model.customer_id.in_(list(ids)))
        return dict(db.execute(stmt).all())

    billing, payment = counted(Billing), counted(Payment)
    return {cid: (billing.get(cid, 0), payment.get(cid, 0)) for cid in set(billing) | set(payment)}


# --- the picker (F07.2, D-37) -------------------------------------------------------------


@dataclass(frozen=True)
class PickRow:
    row: CustomerRow
    billing_count: int
    payment_count: int
    job: tuple[UUID, str] | None
    needs_job: str | None  # the LEDGER_PROJECT_NO_JOB sentence, when this row raises it


@dataclass(frozen=True)
class CustomersPage:
    rows: list[PickRow]
    page: int
    pages: int
    total: int


# Projects, then sub-customers, then plain customers; by name, then id.
_PICK_ORDER = (
    Customer.is_project.desc(),
    Customer.parent_customer_id.is_not(None).desc(),
    func.lower(Customer.display_name),
    Customer.external_id,
)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pick_rows(db: Session, customers: Sequence[Customer]) -> list[PickRow]:
    if not customers:
        return []
    parent_ids = {c.parent_customer_id for c in customers if c.parent_customer_id}
    names: dict[UUID, str] = {}
    if parent_ids:
        names = dict(
            db.execute(
                select(Customer.id, Customer.display_name).where(Customer.id.in_(list(parent_ids)))
            ).all()
        )
    counts = _document_counts(db, [c.id for c in customers])
    jobs = {
        ext: (job_id, name)
        for ext, job_id, name in db.execute(
            select(JobAlias.external_id, Job.id, Job.name)
            .join(Job, Job.id == JobAlias.job_id)
            .where(
                JobAlias.system == QBO, JobAlias.external_id.in_([c.external_id for c in customers])
            )
        ).all()
    }
    out: list[PickRow] = []
    for c in customers:
        row = _customer_row(c, names)
        b, p = counts.get(c.id, (0, 0))
        job = jobs.get(c.external_id)
        issue = ledger_issue(_ledger_row(row, b + p, job is not None))
        out.append(PickRow(row, b, p, job, issue.message if issue else None))
    return out


def search_customers(db: Session, q: str, page: int) -> CustomersPage:
    """The picker's search: the tenant's active QuickBooks rows whose name contains the
    text (a read; the name reaches no write path), one page at a time. Empty text
    returns no rows (owner, 2026-10-04): the picker stays quiet."""
    needle = " ".join((q or "").split())
    if not needle:
        return CustomersPage([], 1, 0, 0)
    where = (
        Customer.source == QBO_SOURCE,
        Customer.active.is_(True),
        Customer.display_name.ilike(f"%{_escape_like(needle)}%", escape="\\"),
    )
    total = db.execute(select(func.count()).select_from(Customer).where(*where)).scalar_one()
    pages = -(-total // PAGE_SIZE)
    page = min(max(page, 1), max(pages, 1))
    customers = list(
        db.execute(
            select(Customer)
            .where(*where)
            .order_by(*_PICK_ORDER)
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        ).scalars()
    )
    return CustomersPage(_pick_rows(db, customers), page, pages, total)


def tracked_customers(db: Session) -> list[PickRow]:
    """Every tracked row, active or not, with its job or the sentence that it needs one."""
    customers = list(
        db.execute(
            select(Customer)
            .where(Customer.source == QBO_SOURCE, Customer.tracked_at.is_not(None))
            .order_by(*_PICK_ORDER)
        ).scalars()
    )
    return _pick_rows(db, customers)


# --- the review queue (D-03) ---------------------------------------------------------


@dataclass
class QueueEntry:
    view: EstimateView
    candidates: list[AttachCandidate]
    division_suggestion: UUID | None
    issues: list[Issue]


def _jobs_for_match(db: Session) -> list[JobForMatch]:
    jobs = list(db.execute(select(Job)).scalars())
    originals = dict(
        db.execute(
            select(JobEstimate.job_id, Estimate)
            .join(Estimate, Estimate.id == JobEstimate.estimate_id)
            .where(JobEstimate.role == "original")
        ).all()
    )
    names = {
        c.id: c.display_name
        for c in db.execute(
            select(Customer).where(Customer.id.in_([j.customer_id for j in jobs if j.customer_id]))
        ).scalars()
    }
    return [
        JobForMatch(
            j.id,
            j.name,
            j.revenue_method,
            j.customer_id,
            names.get(j.customer_id) if j.customer_id else None,
            _text(originals[j.id]) if j.id in originals else None,
        )
        for j in jobs
    ]


def _sold_since(db: Session, estimate_ids: list[UUID]) -> dict[UUID, date]:
    if not estimate_ids:
        return {}
    firsts = db.execute(
        select(EstimateVersion.estimate_id, func.min(EstimateVersion.received_at))
        .where(
            EstimateVersion.estimate_id.in_(estimate_ids),
            EstimateVersion.status_norm == "sold",
        )
        .group_by(EstimateVersion.estimate_id)
    ).all()
    return {eid: _local_date(db, at) for eid, at in firsts}


def review_queue(db: Session, tenant_id: UUID) -> list[QueueEntry]:
    """Every sold estimate on no job (brief, Review queue). Pending, lost and unknown
    estimates never appear. Candidates are suggestions; nothing is attached here."""
    attached = select(JobEstimate.estimate_id)
    estimates = list(
        db.execute(
            select(Estimate)
            .where(Estimate.status_norm == "sold", Estimate.id.not_in(attached))
            .order_by(Estimate.external_id)
        ).scalars()
    )
    grid = load_grid(db, tenant_id)
    views = load_views(db, estimates, grid)
    jobs = _jobs_for_match(db)
    rows = customer_rows(db)
    since = _sold_since(db, [e.id for e in estimates])
    today = tenant_today(db)
    out: list[QueueEntry] = []
    for view in views:
        e = view.estimate
        candidates = attach_candidates(_text(e), jobs, rows)
        issues = [unattached_issue(e.external_id, since.get(e.id), today)]
        same = [(c.job.id, c.job.name) for c in candidates if c.same_customer]
        if same:
            issues.append(second_estimate_issue(e.external_id, same))
        costs = [
            (lv.line.division_id, lv.line.amount)
            for w in view.work_areas or ()
            if w.as_in().counts
            for lv in w.lines
        ]
        out.append(QueueEntry(view, candidates, suggest_division(costs), issues))
    return out


def estimate_job(db: Session, estimate_id: UUID) -> tuple[Job, JobEstimate] | None:
    """The job an estimate is on, for the estimate detail's "Job:" line."""
    row = db.execute(
        select(Job, JobEstimate)
        .join(JobEstimate, JobEstimate.job_id == Job.id)
        .where(JobEstimate.estimate_id == estimate_id)
    ).first()
    return None if row is None else (row[0], row[1])


# --- QuickBooks candidates and search --------------------------------------------------


def job_qbo_candidates(db: Session, job_id: UUID) -> list[QboCandidate]:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("That job does not exist.")
    estimates = list(
        db.execute(
            select(Estimate)
            .join(JobEstimate, JobEstimate.estimate_id == Estimate.id)
            .where(JobEstimate.job_id == job.id)
        ).scalars()
    )
    return qbo_candidates([_text(e) for e in estimates], customer_rows(db), set(_qbo_aliased(db)))


def qbo_search(db: Session, q: str) -> list[CustomerRow]:
    """Owner's answer 21: any active, unaliased customer row whose name contains the
    text, listed for a person to choose from; the link is then made by id."""
    needle = " ".join((q or "").split()).casefold()
    aliased = set(_qbo_aliased(db))
    rows = [
        r
        for r in customer_rows(db)
        if r.active and r.external_id not in aliased and needle in r.display_name.casefold()
    ]
    return sorted(rows, key=lambda r: (r.display_name.casefold(), r.external_id))[:SEARCH_LIMIT]


# --- customer duplicates (read-only) ------------------------------------------------------


@dataclass(frozen=True)
class DuplicateSide:
    row: CustomerRow
    billing_count: int
    payment_count: int


def customer_duplicates(db: Session) -> list[tuple[DuplicateSide, DuplicateSide]]:
    tops = [r for r in customer_rows(db) if r.active and r.parent_id is None]
    by_id = {r.id: r for r in tops}
    counts = _document_counts(db)

    def side(row_id: UUID) -> DuplicateSide:
        b, p = counts.get(row_id, (0, 0))
        return DuplicateSide(by_id[row_id], b, p)

    pairs = duplicate_pairs([NamedRow(r.id, r.display_name) for r in tops])
    return [(side(a.id), side(b.id)) for a, b in pairs]


# --- write actions (one audit row each) -------------------------------------------------


def _job_fields(job: Job) -> dict:
    return {
        "name": job.name,
        "division_id": str(job.division_id) if job.division_id else None,
        "revenue_method": job.revenue_method,
        "status": job.status,
        "sold_on": job.sold_on.isoformat(),
        "notes": job.notes,
        "customer_id": str(job.customer_id) if job.customer_id else None,
    }


def _alias_fields(a: JobAlias) -> dict:
    return {"id": str(a.id), "system": a.system, "external_id": a.external_id}


def _link_fields(link: JobEstimate, est: Estimate) -> dict:
    return {
        "id": str(link.id),
        "estimate_id": str(est.id),
        "estimate": est.external_id,
        "role": link.role,
        "note": link.note,
    }


def _job(db: Session, job_id: UUID) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound("That job does not exist.")
    return job


def _division(db: Session, division_id: UUID | None) -> Division:
    if division_id is None:
        raise Invalid("Choose the job's division.")
    d = db.get(Division, division_id)
    if d is None or not d.active:
        raise Invalid("Choose a division from the list.")
    return d


def _sold_estimate(db: Session, estimate_id: UUID) -> Estimate:
    est = db.get(Estimate, estimate_id)
    if est is None:
        raise NotFound("That estimate does not exist.")
    if est.status_norm != "sold":
        label = STATUS_LABELS.get(est.status_norm or "", est.status)
        raise Invalid(f"Estimate {est.external_id} is {label}; only a sold estimate goes on a job.")
    on = estimate_job(db, est.id)
    if on is not None:
        raise Conflict(f'Estimate {est.external_id} is already on job "{on[0].name}".')
    return est


def _clean_name(name: str | None) -> str:
    text = " ".join((name or "").split())
    if not text:
        raise Invalid("Give the job a name.")
    return text[:500]


def _flush(db: Session, sentence: str) -> None:
    """Flush in a savepoint: a unique key another request won first answers 409."""
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise Conflict(sentence) from None


def create_job_from_estimate(
    db: Session,
    tenant_id: UUID,
    *,
    estimate_id: UUID,
    division_id: UUID | None,
    name: str | None,
    actor: Actor,
) -> Job:
    est = _sold_estimate(db, estimate_id)
    division = _division(db, division_id)
    job = Job(
        tenant_id=tenant_id,
        name=_clean_name(name if name is not None and name.strip() else est.name),
        division_id=division.id,
        revenue_method="fixed_price",
        status="sold",
        sold_on=est.estimate_date or tenant_today(db),
        created_by=actor.user_id,
    )
    db.add(job)
    db.flush()
    link = JobEstimate(
        tenant_id=tenant_id,
        job_id=job.id,
        estimate_id=est.id,
        role="original",
        attached_by=actor.user_id,
    )
    alias = JobAlias(
        tenant_id=tenant_id,
        job_id=job.id,
        system=LMN,
        external_id=est.external_id,
        linked_by=actor.user_id,
    )
    db.add_all([link, alias])
    _flush(db, f"Estimate {est.external_id} was put on a job a moment ago; reload the page.")
    audit(
        db,
        tenant_id,
        TenantEvent.job_created,
        "job",
        job.id,
        actor,
        after=_job_fields(job),
        rows={
            "job": str(job.id),
            "job_estimate": _link_fields(link, est),
            "job_alias": [_alias_fields(alias)],
        },
    )
    return job


def create_pool_job(
    db: Session,
    tenant_id: UUID,
    *,
    name: str | None,
    division_id: UUID | None,
    actor: Actor,
    revenue_method: str = "pool",
) -> Job:
    """A job made by hand with no estimate: a pool (D-30) or, with ``revenue_method =
    recurring_service``, a maintenance or snow program for one season (D-35)."""
    if revenue_method not in NO_ESTIMATE_METHODS:
        raise Invalid("Only a pool or a maintenance or snow program is made without an estimate.")
    division = _division(db, division_id)
    job = Job(
        tenant_id=tenant_id,
        name=_clean_name(name),
        division_id=division.id,
        revenue_method=revenue_method,
        status="sold",
        sold_on=tenant_today(db),
        created_by=actor.user_id,
    )
    db.add(job)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.job_created,
        "job",
        job.id,
        actor,
        after=_job_fields(job),
        rows={"job": str(job.id), "job_estimate": None, "job_alias": []},
    )
    return job


PATCH_FIELDS = ("name", "division_id", "revenue_method", "status", "notes", "sold_on")


def update_job(db: Session, tenant_id: UUID, job_id: UUID, changes: dict, actor: Actor) -> Job:
    """``changes`` holds only the fields the person sent. One ``job_updated`` row with
    the changed fields before and after; nothing changed, no row."""
    job = _job(db, job_id)
    before = _job_fields(job)
    if "name" in changes:
        job.name = _clean_name(changes["name"])
    if "division_id" in changes:
        job.division_id = _division(db, changes["division_id"]).id
    if "revenue_method" in changes:
        method = changes["revenue_method"]
        if method not in REVENUE_METHODS:
            raise Invalid("Choose a revenue method from the list.")
        if method in NO_ESTIMATE_METHODS and job.revenue_method not in NO_ESTIMATE_METHODS:
            has_estimates = db.execute(
                select(func.count()).select_from(JobEstimate).where(JobEstimate.job_id == job.id)
            ).scalar_one()
            if has_estimates:
                raise Invalid(
                    "A pool or a program has no estimate; detach this job's estimates first."
                )
        job.revenue_method = method
    if "status" in changes:
        if changes["status"] not in JOB_STATUSES:
            raise Invalid("Choose a status from the list.")
        job.status = changes["status"]
    if "notes" in changes:
        job.notes = (changes["notes"] or "").strip()[:4000] or None
    if "sold_on" in changes:
        # F07.1: a person corrects the date (the estimate carried none, or the wrong one).
        sold_on = changes["sold_on"]
        if sold_on is None:
            raise Invalid("Give the sold-on date.")
        today = tenant_today(db)
        if sold_on > today:
            raise Invalid(f"Sold on cannot be after today, {today.isoformat()}.")
        job.sold_on = sold_on
    after = _job_fields(job)
    changed = sorted(k for k in after if after[k] != before[k])
    if changed:
        db.flush()
        audit(
            db,
            tenant_id,
            TenantEvent.job_updated,
            "job",
            job.id,
            actor,
            before={k: before[k] for k in changed},
            after={k: after[k] for k in changed},
            changed_fields=changed,
            rows={"job": str(job.id)},
        )
    return job


def attach_estimate(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    *,
    estimate_id: UUID,
    role: str,
    note: str | None,
    actor: Actor,
) -> JobEstimate:
    job = _job(db, job_id)
    if job.revenue_method == "pool":
        raise Invalid("A pool has no estimate; attach this estimate to a job instead (D-30).")
    if job.revenue_method == "recurring_service" and role != "ignored":
        # Owner, 2026-09-29: a sold maintenance or snow estimate goes on the season's
        # program job as ignored, with a note; the program is billed as service (D-35).
        raise Invalid(
            "A maintenance or snow program takes an estimate only as ignored, with a note "
            "such as: maintenance contract; billed as service (D-35)."
        )
    if role not in ROLES:
        raise Invalid("Choose a role: original, change order or ignored.")
    note = (note or "").strip()[:2000] or None
    if role == "ignored" and note is None:
        raise Invalid("Give the reason in the note: an estimate is ignored only with a reason.")
    est = _sold_estimate(db, estimate_id)
    if role == "original":
        has_original = db.execute(
            select(JobEstimate.id).where(
                JobEstimate.job_id == job.id, JobEstimate.role == "original"
            )
        ).first()
        if has_original:
            raise Conflict(
                f'Job "{job.name}" already has its original estimate; attach this one as a '
                "change order or ignored."
            )
    link = JobEstimate(
        tenant_id=tenant_id,
        job_id=job.id,
        estimate_id=est.id,
        role=role,
        note=note,
        attached_by=actor.user_id,
    )
    alias = JobAlias(
        tenant_id=tenant_id,
        job_id=job.id,
        system=LMN,
        external_id=est.external_id,
        linked_by=actor.user_id,
    )
    db.add_all([link, alias])
    _flush(db, f"Estimate {est.external_id} was put on a job a moment ago; reload the page.")
    audit(
        db,
        tenant_id,
        TenantEvent.job_estimate_attached,
        "job",
        job.id,
        actor,
        after=_link_fields(link, est),
        rows={"job_estimate": str(link.id), "job_alias": [_alias_fields(alias)]},
    )
    return link


def detach_estimate(
    db: Session, tenant_id: UUID, job_id: UUID, estimate_id: UUID, actor: Actor
) -> None:
    job = _job(db, job_id)
    link = db.execute(
        select(JobEstimate).where(
            JobEstimate.job_id == job.id, JobEstimate.estimate_id == estimate_id
        )
    ).scalar_one_or_none()
    if link is None:
        raise NotFound("That estimate is not on this job.")
    if link.role == "original":
        others = db.execute(
            select(func.count())
            .select_from(JobEstimate)
            .where(JobEstimate.job_id == job.id, JobEstimate.id != link.id)
        ).scalar_one()
        if others:
            raise Conflict(
                "Detach the job's other estimates first: the original stays while any other "
                "estimate is attached."
            )
    est = db.get(Estimate, link.estimate_id)
    alias = db.execute(
        select(JobAlias).where(
            JobAlias.job_id == job.id,
            JobAlias.system == LMN,
            JobAlias.external_id == est.external_id,
        )
    ).scalar_one_or_none()
    before = _link_fields(link, est)
    removed = [_alias_fields(alias)] if alias is not None else []
    db.delete(link)
    if alias is not None:
        db.delete(alias)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.job_estimate_detached,
        "job",
        job.id,
        actor,
        before=before,
        rows={"job_estimate": before["id"], "job_alias": removed},
    )


def _customer(db: Session, customer_id: UUID) -> Customer:
    row = db.get(Customer, customer_id)
    if row is None or row.source != QBO_SOURCE:
        raise NotFound(
            "That QuickBooks customer is not in the platform's copy; sync and try again."
        )
    return row


def _tracked_fields(c: Customer) -> dict:
    return {
        "customer_id": str(c.id),
        "external_id": c.external_id,
        "display_name": c.display_name,
        "tracked_at": c.tracked_at.isoformat() if c.tracked_at else None,
        "tracked_by": str(c.tracked_by) if c.tracked_by else None,
    }


def _track(row: Customer, actor: Actor) -> None:
    row.tracked_at = datetime.now(UTC)
    row.tracked_by = actor.user_id


def track_customer(db: Session, tenant_id: UUID, customer_id: UUID, actor: Actor) -> Customer:
    """D-37: a person picks one QuickBooks row, by its id. One audit row."""
    row = _customer(db, customer_id)
    if row.tracked_at is not None:
        raise Conflict(f'"{row.display_name}" is already tracked.')
    if not row.active:
        raise Invalid(f'"{row.display_name}" is inactive in QuickBooks; track an active row.')
    _track(row, actor)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.customer_tracked,
        "customer",
        row.id,
        actor,
        after=_tracked_fields(row),
    )
    return row


def untrack_customer(db: Session, tenant_id: UUID, customer_id: UUID, actor: Actor) -> Customer:
    """Refused while the row is linked to a job: unlink there first. One audit row."""
    row = _customer(db, customer_id)
    if row.tracked_at is None:
        raise Conflict(f'"{row.display_name}" is not tracked.')
    linked = db.execute(
        select(Job.name)
        .join(JobAlias, JobAlias.job_id == Job.id)
        .where(JobAlias.system == QBO, JobAlias.external_id == row.external_id)
    ).scalar_one_or_none()
    if linked is not None:
        raise Conflict(f'"{row.display_name}" is linked to job "{linked}"; unlink it there first.')
    before = _tracked_fields(row)
    row.tracked_at = None
    row.tracked_by = None
    db.flush()
    audit(db, tenant_id, TenantEvent.customer_untracked, "customer", row.id, actor, before=before)
    return row


def link_alias(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    *,
    system: str,
    external_id: str,
    actor: Actor,
    set_in_progress: bool = False,
) -> JobAlias:
    """A person links one QuickBooks row, by its id. Never called with a name. D-35: the
    project is created when the first money moves, so the person may set a sold job in
    progress in the same action (``set_in_progress``); one audit row covers both."""
    job = _job(db, job_id)
    if system != QBO:
        raise Invalid(
            "Only a QuickBooks link is made here; an estimate joins a job through the review."
        )
    row = db.execute(
        select(Customer).where(Customer.source == QBO_SOURCE, Customer.external_id == external_id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(
            "That QuickBooks customer is not in the platform's copy; sync and try again."
        )
    if not row.active:
        raise Invalid("That QuickBooks customer is inactive; link an active one.")
    taken = db.execute(
        select(JobAlias, Job)
        .join(Job, Job.id == JobAlias.job_id)
        .where(JobAlias.system == QBO, JobAlias.external_id == external_id)
    ).first()
    if taken is not None:
        other = taken[1]
        where = "this job" if other.id == job.id else f'job "{other.name}"'
        raise Conflict(f'"{row.display_name}" is already linked to {where}; unlink it there first.')
    before_customer = job.customer_id
    alias = JobAlias(
        tenant_id=tenant_id,
        job_id=job.id,
        system=QBO,
        external_id=external_id,
        linked_by=actor.user_id,
    )
    db.add(alias)
    before_tracked = row.tracked_at.isoformat() if row.tracked_at else None
    if row.tracked_at is None:
        _track(row, actor)  # D-37: the link tracks the row in the same action
    if job.customer_id is None:
        job.customer_id = row.parent_customer_id or row.id
    before_status = job.status
    if set_in_progress and job.status == "sold":
        job.status = "in_progress"
    _flush(db, f'"{row.display_name}" was linked to a job a moment ago; reload the page.')
    audit(
        db,
        tenant_id,
        TenantEvent.job_alias_linked,
        "job",
        job.id,
        actor,
        after={**_alias_fields(alias), "display_name": row.display_name},
        rows={
            "job_alias": [_alias_fields(alias)],
            "job.customer_id": [
                str(before_customer) if before_customer else None,
                str(job.customer_id) if job.customer_id else None,
            ],
            "job.status": [before_status, job.status],
            "customer.tracked_at": [before_tracked, row.tracked_at.isoformat()],
        },
    )
    return alias


def unlink_alias(db: Session, tenant_id: UUID, job_id: UUID, alias_id: UUID, actor: Actor) -> None:
    job = _job(db, job_id)
    alias = db.get(JobAlias, alias_id)
    if alias is None or alias.job_id != job.id:
        raise NotFound("That link is not on this job.")
    if alias.system != QBO:
        raise Invalid("An estimate's id leaves the job when the estimate is detached.")
    before = _alias_fields(alias)
    before_customer = job.customer_id
    db.delete(alias)
    db.flush()
    remaining = db.execute(
        select(func.count())
        .select_from(JobAlias)
        .where(JobAlias.job_id == job.id, JobAlias.system == QBO)
    ).scalar_one()
    if remaining == 0:
        job.customer_id = None  # set by the first link; cleared with the last
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.job_alias_unlinked,
        "job",
        job.id,
        actor,
        before=before,
        rows={
            "job_alias": [before],
            "job.customer_id": [
                str(before_customer) if before_customer else None,
                str(job.customer_id) if job.customer_id else None,
            ],
        },
    )


def _original_work_areas(
    db: Session, tenant_id: UUID, job: Job
) -> tuple[Estimate, list[EstimateWorkArea]]:
    """The work areas of the latest version of the job's original estimate."""
    original = db.execute(
        select(JobEstimate).where(JobEstimate.job_id == job.id, JobEstimate.role == "original")
    ).scalar_one_or_none()
    if original is None:
        raise NotFound("This job has no original estimate.")
    est = db.get(Estimate, original.estimate_id)
    view = load_views(db, [est], load_grid(db, tenant_id))[0]
    return est, [w.row for w in view.work_areas or ()]


def _confirm_area(
    db: Session,
    tenant_id: UUID,
    job: Job,
    est: Estimate,
    area: EstimateWorkArea,
    kind: str,
    actor: Actor,
    when: datetime,
) -> None:
    """Set one work area's kind and write its one ``work_area_kind_confirmed`` row."""
    before = area.kind
    area.kind = kind
    area.kind_confirmed_by = actor.user_id
    area.kind_confirmed_at = when
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.work_area_kind_confirmed,
        "job",
        job.id,
        actor,
        before={"kind": before},
        after={"kind": kind},
        rows={
            "estimate_work_area": str(area.id),
            "estimate": est.external_id,
            "order_no": area.order_no,
        },
    )


def confirm_kind(
    db: Session, tenant_id: UUID, job_id: UUID, work_area_id: UUID, kind: str, actor: Actor
) -> EstimateWorkArea:
    """D-01: a person confirms a kept work area's kind on the latest version of the
    job's original estimate. A change-order estimate's kinds come from its role."""
    job = _job(db, job_id)
    if kind not in WORK_AREA_KINDS:
        raise Invalid("Choose original or change order.")
    est, rows = _original_work_areas(db, tenant_id, job)
    area = next((r for r in rows if r.id == work_area_id), None)
    if area is None:
        raise NotFound(
            "That work area is not on the latest version of this job's original estimate."
        )
    if not area.kept:
        raise Invalid(f"Work area #{area.order_no} is omitted; only kept work areas have a kind.")
    if kind == "original" and area.kind == "change_order":
        view = (
            _approvals(db, [job.id], {est.id: "original"})[0]
            .get(job.id, {})
            .get((est.id, area.order_no))
        )
        if view is not None and view.applies:  # F07.4 (D-42)
            raise Conflict(
                f"Work area #{area.order_no} is an approved change order; withdraw the approval "
                "before changing its kind."
            )
    _confirm_area(db, tenant_id, job, est, area, kind, actor, datetime.now(UTC))
    return area


@dataclass(frozen=True)
class AreaChoice:
    """One work area as the F07.1 selector sees it (pure; no row objects)."""

    order_no: int
    kept: bool
    kind: str | None  # confirmed kind, or None
    suggestion: str | None  # the platform's suggestion, or None


def as_suggested(areas: Sequence[AreaChoice]) -> tuple[list[AreaChoice], int]:
    """F07.1: the kept work areas with no confirmed kind and a suggestion, in order (each
    is confirmed at its suggestion), and the count of kept, unconfirmed work areas with
    no suggestion (skipped: they stay in "to confirm"). Confirmed and omitted work areas
    are never in either."""
    chosen = [a for a in areas if a.kept and a.kind is None and a.suggestion is not None]
    skipped = sum(1 for a in areas if a.kept and a.kind is None and a.suggestion is None)
    return chosen, skipped


@dataclass(frozen=True)
class ConfirmedSuggested:
    confirmed: int
    skipped: int


def confirm_suggested_kinds(
    db: Session, tenant_id: UUID, job_id: UUID, actor: Actor
) -> ConfirmedSuggested:
    """F07.1 (D-01): one press by a person confirms every kept, unconfirmed work area on
    the latest version of the job's original estimate at its suggested kind. One
    ``work_area_kind_confirmed`` row per work area, as the single action writes it, in
    the request's one transaction; never a summary row. Nothing left: nothing written."""
    job = _job(db, job_id)
    est, rows = _original_work_areas(db, tenant_id, job)
    by_order = {r.order_no: r for r in rows}
    chosen, skipped = as_suggested(
        [
            AreaChoice(
                r.order_no,
                r.kept,
                r.kind,
                suggested_kind(r.change_order_suggested) if r.kept else None,
            )
            for r in rows
        ]
    )
    when = datetime.now(UTC)
    for choice in chosen:
        _confirm_area(
            db, tenant_id, job, est, by_order[choice.order_no], choice.suggestion, actor, when
        )
    return ConfirmedSuggested(confirmed=len(chosen), skipped=skipped)


# --- F07.4 (D-42): approve a change order, withdraw an approval, the unapproved list ----------


@dataclass(frozen=True)
class _AreaOnJob:
    link: JobEstimate
    estimate: Estimate
    area: EstimateWorkArea


def _attached_work_areas(db: Session, tenant_id: UUID, job: Job) -> list[_AreaOnJob]:
    """Every kept or omitted work area on the latest version of each estimate attached to
    the job as original or change order (``ignored`` counts for nothing)."""
    links = list(
        db.execute(
            select(JobEstimate)
            .where(JobEstimate.job_id == job.id, JobEstimate.role != "ignored")
            .order_by(JobEstimate.attached_at)
        ).scalars()
    )
    if not links:
        return []
    ests = {
        e.id: e
        for e in db.execute(
            select(Estimate).where(Estimate.id.in_([ln.estimate_id for ln in links]))
        ).scalars()
    }
    views = {
        v.estimate.id: v for v in load_views(db, list(ests.values()), load_grid(db, tenant_id))
    }
    return [
        _AreaOnJob(ln, ests[ln.estimate_id], w.row)
        for ln in links
        for w in views[ln.estimate_id].work_areas or ()
    ]


NOT_ON_JOB = "That work area is not on the latest version of an estimate on this job."


def _find_area(db: Session, tenant_id: UUID, job: Job, work_area_id: UUID) -> _AreaOnJob:
    found = next(
        (a for a in _attached_work_areas(db, tenant_id, job) if a.area.id == work_area_id), None
    )
    if found is None:
        raise NotFound(NOT_ON_JOB)
    return found


def _job_approvals(db: Session, job: Job) -> dict[ApprovalKey, ApprovalView]:
    roles = dict(
        db.execute(
            select(JobEstimate.estimate_id, JobEstimate.role).where(JobEstimate.job_id == job.id)
        ).all()
    )
    return _approvals(db, [job.id], roles)[0].get(job.id, {})


def _approval_fields(row: ChangeOrderApproval) -> dict:
    return {
        "approved": row.action == "approved",
        "price": str(row.price) if row.action == "approved" else None,
        "agreed_on": row.agreed_on.isoformat() if row.agreed_on else None,
        "agreed_by": row.agreed_by,
        "evidence_ref": row.evidence_ref,
        "note": row.note,
        "reason": row.reason,
    }


def _clean(text: str | None, limit: int) -> str | None:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit] or None


def approve_change_order(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    work_area_id: UUID,
    *,
    agreed_on: date,
    agreed_by: str | None,
    evidence_ref: str | None,
    note: str | None,
    actor: Actor,
) -> ChangeOrderApproval:
    """D-42: a person with the role records that the customer agreed to a change order,
    at its price today; one row, one audit row. Refusals are one sentence and write
    nothing. The evidence the approval must carry is the tenant's policy, never a default."""
    job = _job(db, job_id)
    required = evidence_required(db)
    if required is None:
        raise Conflict(
            f"Decide the policy key {POLICY_KEYS[CHANGE_ORDER_EVIDENCE].label} before approving "
            "a change order (D-42)."
        )
    found = _find_area(db, tenant_id, job, work_area_id)
    area, est, link = found.area, found.estimate, found.link
    n = area.order_no
    if not area.kept:
        raise Invalid(f"Work area #{n} is omitted; an omitted work area cannot be approved.")
    if link.role == "original":
        if area.kind is None:
            raise Invalid(
                f"Work area #{n} is not confirmed; confirm it as a change order before "
                "approving it."
            )
        if area.kind != "change_order":
            raise Invalid(
                f"Work area #{n} is an original work area; only a change order is approved."
            )
    today = tenant_today(db)
    if agreed_on > today:
        raise Invalid(f"The date the customer agreed cannot be after today ({today.isoformat()}).")
    ref = _clean(evidence_ref, 500)
    if required and ref is None:
        raise Invalid(
            "A reference to the evidence is required by this company's policy "
            f"({POLICY_KEYS[CHANGE_ORDER_EVIDENCE].label}, D-42)."
        )
    current = _job_approvals(db, job).get((est.id, n))
    if current is not None and current.applies:
        who = _user_names(db, {current.latest.recorded_by}).get(
            current.latest.recorded_by, "a user"
        )
        when = current.latest.agreed_on.isoformat()
        raise Conflict(
            f"Work area #{n} is already approved ({when} by {who}); withdraw that approval first."
        )
    row = ChangeOrderApproval(
        tenant_id=tenant_id,
        job_id=job.id,
        estimate_id=est.id,
        order_no=n,
        work_area_name=area.name,
        estimate_work_area_id=area.id,
        action="approved",
        price=area.price,
        agreed_on=agreed_on,
        agreed_by=_clean(agreed_by, 200),
        evidence_ref=ref,
        note=_clean(note, 2000),
        recorded_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.change_order_approved,
        "job",
        job.id,
        actor,
        before={"approved": False, "price": None},
        after=_approval_fields(row),
        rows={
            "change_order_approval": str(row.id),
            "estimate": est.external_id,
            "order_no": n,
            "estimate_work_area": str(area.id),
        },
    )
    return row


def withdraw_approval(
    db: Session,
    tenant_id: UUID,
    job_id: UUID,
    work_area_id: UUID,
    *,
    reason: str | None,
    actor: Actor,
) -> ChangeOrderApproval:
    """D-42: an approval is withdrawn with a reason; the work area is unapproved again. An
    approval a later version ended may be withdrawn too (the platform never writes one on
    its own); the work area id may be the one the approval row names when the latest
    version no longer carries the row."""
    job = _job(db, job_id)
    text = _clean(reason, 2000)
    if text is None:
        raise Invalid("Give the reason: an approval is withdrawn only with a reason.")
    try:
        found = _find_area(db, tenant_id, job, work_area_id)
        key: ApprovalKey = (found.estimate.id, found.area.order_no)
    except NotFound:
        named = db.execute(
            select(ChangeOrderApproval)
            .where(
                ChangeOrderApproval.job_id == job.id,
                ChangeOrderApproval.estimate_work_area_id == work_area_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if named is None:
            raise
        key = (named.estimate_id, named.order_no)
    current = _job_approvals(db, job).get(key)
    if current is None or current.latest.action != "approved":
        raise Conflict(f"Work area #{key[1]} is not approved; there is nothing to withdraw.")
    approval = current.latest
    row = ChangeOrderApproval(
        tenant_id=tenant_id,
        job_id=job.id,
        estimate_id=approval.estimate_id,
        order_no=approval.order_no,
        work_area_name=approval.work_area_name,
        estimate_work_area_id=approval.estimate_work_area_id,
        action="withdrawn",
        price=approval.price,
        reason=text,
        withdraws_id=approval.id,
        recorded_by=actor.user_id,
    )
    db.add(row)
    db.flush()
    est = db.get(Estimate, approval.estimate_id)
    audit(
        db,
        tenant_id,
        TenantEvent.change_order_approval_withdrawn,
        "job",
        job.id,
        actor,
        before={**_approval_fields(approval), "approval": str(approval.id)},
        after={"approved": False, "price": None, "reason": text},
        rows={
            "change_order_approval": str(row.id),
            "withdraws": str(approval.id),
            "estimate": est.external_id if est else None,
            "order_no": approval.order_no,
            "estimate_work_area": str(approval.estimate_work_area_id),
        },
    )
    return row


@dataclass(frozen=True)
class UnapprovedRow:
    job_id: UUID
    job_name: str
    estimate_number: str
    estimator: str | None
    order_no: int
    name: str
    price: Decimal
    first_seen: date  # the received date of the first version that carries the work area
    days: int
    approval_ended: bool  # an earlier approval no longer applies (rule C)


def _first_seen(
    db: Session, estimate_ids: Sequence[UUID]
) -> dict[tuple[UUID, int], tuple[str, datetime]]:
    """Per (estimate, order number): the latest version's name and the received time of
    the earliest version in the unbroken run of versions carrying a row with that name
    at that order number (a renamed row is a new row, as the kind carry says)."""
    if not estimate_ids:
        return {}
    versions = list(
        db.execute(
            select(EstimateVersion)
            .where(
                EstimateVersion.estimate_id.in_(list(estimate_ids)),
                EstimateVersion.work_areas_raw_record_id.is_not(None),
            )
            .order_by(EstimateVersion.estimate_id, EstimateVersion.version_no)
        ).scalars()
    )
    if not versions:
        return {}
    names: dict[UUID, dict[int, str]] = {v.id: {} for v in versions}
    for w in db.execute(
        select(
            EstimateWorkArea.estimate_version_id, EstimateWorkArea.order_no, EstimateWorkArea.name
        ).where(EstimateWorkArea.estimate_version_id.in_(list(names)))
    ).all():
        names[w.estimate_version_id][w.order_no] = w.name
    by_est: dict[UUID, list[EstimateVersion]] = {}
    for v in versions:
        by_est.setdefault(v.estimate_id, []).append(v)
    out: dict[tuple[UUID, int], tuple[str, datetime]] = {}
    for eid, vs in by_est.items():
        latest = vs[-1]
        for order_no, name in names[latest.id].items():
            since = latest.received_at
            for v in reversed(vs[:-1]):
                earlier = names[v.id].get(order_no)
                if earlier is None or not same_name(earlier, name):
                    break
                since = v.received_at
            out[(eid, order_no)] = (name, since)
    return out


def unapproved_change_orders(db: Session, tenant_id: UUID) -> list[UnapprovedRow]:
    """Every kept change-order work area without an applying approval on a job that is
    sold, in progress or substantially complete, in job order then by estimate and order
    number; computed on read."""
    views = [v for v in list_jobs(db, tenant_id) if v.job.status in LISTED_STATUSES]
    wanted: list[tuple[JobView, AttachedView, EstimateWorkArea]] = []
    for v in views:
        for a in v.attached:
            if a.link.role == "ignored" or a.view.work_areas is None:
                continue
            for w in a.view.work_areas:
                row = w.row
                if not row.kept:
                    continue
                if a.link.role == "original" and row.kind != "change_order":
                    continue
                approval = v.approval_for(a.estimate.id, row.order_no)
                if approval is not None and approval.applies:
                    continue
                wanted.append((v, a, row))
    first = _first_seen(db, sorted({a.estimate.id for _v, a, _r in wanted}, key=str))
    today = tenant_today(db)
    out: list[UnapprovedRow] = []
    for v, a, row in wanted:
        _name, since = first.get((a.estimate.id, row.order_no), (row.name, datetime.now(UTC)))
        seen = _local_date(db, since)
        approval = v.approval_for(a.estimate.id, row.order_no)
        out.append(
            UnapprovedRow(
                job_id=v.job.id,
                job_name=v.job.name,
                estimate_number=a.estimate.external_id,
                estimator=a.estimate.estimator,
                order_no=row.order_no,
                name=row.name,
                price=row.price,
                first_seen=seen,
                days=max((today - seen).days, 0),
                approval_ended=approval is not None and approval.ended is not None,
            )
        )
    return out
