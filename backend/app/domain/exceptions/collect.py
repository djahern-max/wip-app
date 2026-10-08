"""The one computation of a subject's review sentences (F09, Plan answer 4; D-22).

``job_attention`` and ``estimate_attention`` are the only two functions that assemble a
subject's sentences; the Jobs board, the job page, Home, the Estimates list, the
estimate detail and the run all call them, so every page reads one result. ``collect``
assembles a tenant's whole set for the run from one ``list_jobs``, one
``load_board(other=False)``, the review queue, the tracked rows and the estimates; the
pages never call it. Nothing here writes; nothing here computes money (the figures come
from ``app.domain.billing``). Requires ``app.tenant_id`` on the session.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.domain.billing.board import Board, load_board
from app.domain.billing.figures import JobFigures
from app.domain.billing.pay_applications import ApplicationView, tie_issues
from app.domain.billing.work_areas import JobWorkAreas
from app.domain.estimates.exceptions import Issue
from app.domain.estimates.service import (
    BurdenView,
    EstimateView,
    estimate_burden,
    list_estimates,
    load_burden_inputs,
)
from app.domain.exceptions.registry import (
    CUSTOMER,
    ESTIMATE,
    JOB,
    Key,
    identity,
    severity_of,
)
from app.domain.jobs import service as jobs
from app.domain.jobs.issues import billing_issues, unapproved_co_billing_issue


def job_attention(
    view: jobs.JobView,
    figures: JobFigures,
    work_areas: JobWorkAreas | None,
    applications: Sequence[ApplicationView] | None,
) -> list[Issue]:
    """A job's sentences in the board's order: the F07 job rules and the ended approvals
    (``view.issues``), the F08 billing items (D-02, D-41, §10), ``BILLING_UNAPPROVED_CO``
    (D-45), then the pay-application ties (D-36, D-39)."""
    flag = unapproved_co_billing_issue(view.job.name, work_areas) if work_areas else None
    return [
        *view.issues,
        *billing_issues(view.job.name, figures),
        *([flag] if flag else []),
        *tie_issues(view.job.name, applications or [], figures.documents),
    ]


def estimate_attention(view: EstimateView, burden: BurdenView) -> list[Issue]:
    """An estimate's sentences: the F06 set, then the F06.1 burden sentences (D-05)."""
    return [*view.issues(), *burden.issues]


@dataclass(frozen=True)
class Raised:
    subject_type: str
    subject_id: str
    issue: Issue
    severity: str

    @property
    def key(self) -> Key:
        return identity(self.subject_type, self.subject_id, self.issue)


def _fixed_price_estimates(views: Sequence[jobs.JobView]) -> dict[UUID, bool]:
    """estimate id → whether its job is fixed price (``ignored`` links count for nothing)."""
    out: dict[UUID, bool] = {}
    for v in views:
        for a in v.attached:
            if a.link.role != "ignored":
                out[a.estimate.id] = v.job.revenue_method == "fixed_price"
    return out


def collect(db: Session, tenant_id: UUID) -> list[Raised]:
    """Every sentence the generators raise for the tenant today, each with its subject
    and severity, in a fixed order (jobs, the review queue, the tracked rows, the
    estimates). Duplicates of one identity are kept here; the run keeps one."""
    out: list[Raised] = []
    views = jobs.list_jobs(db, tenant_id)
    board: Board = load_board(db, tenant_id, views, other=False)
    for v in views:
        sid = str(v.job.id)
        for issue in job_attention(
            v,
            board.per_job[v.job.id],
            board.work_areas.get(v.job.id),
            board.applications.get(v.job.id),
        ):
            out.append(Raised(JOB, sid, issue, severity_of(issue.code)))
    for entry in jobs.review_queue(db, tenant_id):
        sid = str(entry.view.estimate.id)
        for issue in entry.issues:
            out.append(Raised(ESTIMATE, sid, issue, severity_of(issue.code)))
    for issue in jobs.ledger_items(db):
        out.append(
            Raised(CUSTOMER, str(issue.detail["customer_id"]), issue, severity_of(issue.code))
        )
    fixed = _fixed_price_estimates(views)
    estimates, _estimators, grid = list_estimates(db, tenant_id)
    inputs = load_burden_inputs(db)
    for view in estimates:
        sid = str(view.estimate.id)
        burden = estimate_burden(db, view, grid, inputs)
        for issue in estimate_attention(view, burden):
            out.append(
                Raised(
                    ESTIMATE,
                    sid,
                    issue,
                    severity_of(issue.code, fixed_price=fixed.get(view.estimate.id)),
                )
            )
    return out
