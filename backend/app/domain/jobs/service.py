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

from app.core.audit import TenantEvent
from app.domain.billing.models import Billing, Customer, Payment
from app.domain.billing.sync import SOURCE as QBO_SOURCE
from app.domain.config.audit import Actor, audit
from app.domain.config.models import Division
from app.domain.config.policy import TIMEZONE, get_policy
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
from app.domain.jobs.contract import AreaIn, AttachedIn, Contract, job_contract
from app.domain.jobs.duplicates import NamedRow, duplicate_pairs
from app.domain.jobs.issues import (
    JobState,
    LedgerRow,
    job_issues,
    ledger_issues,
    second_estimate_issue,
    unattached_issue,
)
from app.domain.jobs.models import (
    JOB_STATUSES,
    REVENUE_METHODS,
    ROLES,
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
)
from app.tenancy.models import User

LMN = "lmn_estimate"
QBO = "qbo_customer"
SEARCH_LIMIT = 25


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


def customer_rows(db: Session) -> list[CustomerRow]:
    customers = list(db.execute(select(Customer).where(Customer.source == QBO_SOURCE)).scalars())
    names = {c.id: c.display_name for c in customers}
    return [
        CustomerRow(
            c.id,
            c.external_id,
            c.display_name,
            c.parent_customer_id,
            names.get(c.parent_customer_id) if c.parent_customer_id else None,
            c.is_project,
            c.active,
        )
        for c in customers
    ]


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


def _areas(view: EstimateView) -> tuple[AreaIn, ...] | None:
    if view.work_areas is None:
        return None
    return tuple(
        AreaIn(w.row.order_no, w.row.kept, w.row.price, w.row.kind) for w in view.work_areas
    )


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

    @property
    def original(self) -> AttachedView | None:
        return next((a for a in self.attached if a.link.role == "original"), None)

    @property
    def sold_on_set_when_created(self) -> bool:
        """Owner's answer 17: the date did not come from the original estimate."""
        o = self.original
        return o is None or o.estimate.estimate_date != self.job.sold_on

    @property
    def qbo_aliases(self) -> list[JobAlias]:
        return [a for a in self.aliases if a.system == QBO]


def load_job_views(db: Session, tenant_id: UUID, jobs: Sequence[Job]) -> list[JobView]:
    if not jobs:
        return []
    grid = load_grid(db, tenant_id)
    ids = [j.id for j in jobs]
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
    users = _user_names(
        db,
        {j.created_by for j in jobs}
        | {ln.attached_by for ln in links}
        | {a.linked_by for a in aliases},
    )
    out: list[JobView] = []
    for job in jobs:
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
            [AttachedIn(a.link.role, a.estimate.price, _areas(a.view), a.eac) for a in attached],
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
                issues=job_issues(state),
                users=users,
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


def ledger_items(db: Session) -> list[Issue]:
    """LEDGER_PROJECT_NO_JOB for every active project or sub-customer with at least one
    billing or payment row (owner's answers 3 and 20) and no alias."""
    aliased = _qbo_aliased(db)
    counts = _document_counts(db)
    rows = [
        LedgerRow(
            r.id,
            r.external_id,
            r.display_name,
            r.kind_label,
            sum(counts.get(r.id, (0, 0))),
            r.external_id in aliased,
        )
        for r in customer_rows(db)
        if r.active and r.parent_id is not None
    ]
    return ledger_issues(sorted(rows, key=lambda r: r.display_name.casefold()))


def _document_counts(db: Session) -> dict[UUID, tuple[int, int]]:
    billing = dict(
        db.execute(
            select(Billing.customer_id, func.count())
            .where(Billing.customer_id.is_not(None))
            .group_by(Billing.customer_id)
        ).all()
    )
    payment = dict(
        db.execute(
            select(Payment.customer_id, func.count())
            .where(Payment.customer_id.is_not(None))
            .group_by(Payment.customer_id)
        ).all()
    )
    return {cid: (billing.get(cid, 0), payment.get(cid, 0)) for cid in set(billing) | set(payment)}


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
    db: Session, tenant_id: UUID, *, name: str | None, division_id: UUID | None, actor: Actor
) -> Job:
    division = _division(db, division_id)
    job = Job(
        tenant_id=tenant_id,
        name=_clean_name(name),
        division_id=division.id,
        revenue_method="pool",
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


PATCH_FIELDS = ("name", "division_id", "revenue_method", "status", "notes")


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
        if method == "pool" and job.revenue_method != "pool":
            has_estimates = db.execute(
                select(func.count()).select_from(JobEstimate).where(JobEstimate.job_id == job.id)
            ).scalar_one()
            if has_estimates:
                raise Invalid(
                    "A pool has no estimate; detach this job's estimates before making it a pool."
                )
        job.revenue_method = method
    if "status" in changes:
        if changes["status"] not in JOB_STATUSES:
            raise Invalid("Choose a status from the list.")
        job.status = changes["status"]
    if "notes" in changes:
        job.notes = (changes["notes"] or "").strip()[:4000] or None
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


def link_alias(
    db: Session, tenant_id: UUID, job_id: UUID, *, system: str, external_id: str, actor: Actor
) -> JobAlias:
    """A person links one QuickBooks row, by its id. Never called with a name."""
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
    if job.customer_id is None:
        job.customer_id = row.parent_customer_id or row.id
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


def confirm_kind(
    db: Session, tenant_id: UUID, job_id: UUID, work_area_id: UUID, kind: str, actor: Actor
) -> EstimateWorkArea:
    """D-01: a person confirms a kept work area's kind on the latest version of the
    job's original estimate. A change-order estimate's kinds come from its role."""
    job = _job(db, job_id)
    if kind not in WORK_AREA_KINDS:
        raise Invalid("Choose original or change order.")
    original = db.execute(
        select(JobEstimate).where(JobEstimate.job_id == job.id, JobEstimate.role == "original")
    ).scalar_one_or_none()
    if original is None:
        raise NotFound("This job has no original estimate.")
    est = db.get(Estimate, original.estimate_id)
    view = load_views(db, [est], load_grid(db, tenant_id))[0]
    area = next((w.row for w in view.work_areas or () if w.row.id == work_area_id), None)
    if area is None:
        raise NotFound(
            "That work area is not on the latest version of this job's original estimate."
        )
    if not area.kept:
        raise Invalid(f"Work area #{area.order_no} is omitted; only kept work areas have a kind.")
    before = area.kind
    area.kind = kind
    area.kind_confirmed_by = actor.user_id
    area.kind_confirmed_at = datetime.now(UTC)
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
    return area
