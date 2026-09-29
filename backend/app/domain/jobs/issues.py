"""Review items for jobs and the crosswalk (F07; BLUEPRINT §10 names where §10 has
one, owner's answer 18). Pure generators, computed on read; F09 persists them. Each
code has one sentence here (D-22: the sentence, never the code alone).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.domain.estimates.exceptions import Issue
from app.domain.jobs.models import OPEN_STATUSES

SENTENCES: dict[str, str] = {
    # §10 EST_UNATTACHED (the brief's EST_SOLD_UNREVIEWED)
    "EST_UNATTACHED": (
        "Estimate {external_id} is sold and not on any job{age}. Review it under Jobs, "
        "Review sold estimates: make it a new job or attach it to one (D-03)."
    ),
    # new: §10 has none
    "JOB_SECOND_ESTIMATE_FOR_CUSTOMER": (
        "Estimate {external_id} is for a customer who already has a job ({jobs}). Make it "
        "a new job if it is separate scope, or attach it as a change order if the "
        "customer treats it as one contract (D-03)."
    ),
    # §10 JOB_NO_LEDGER_LINK (the brief's JOB_NO_QBO_LINK)
    "JOB_NO_LEDGER_LINK": (
        'Job "{name}" is not linked to QuickBooks, so its billing cannot be read. Link '
        "its project on the job's QuickBooks section."
    ),
    # §10 LEDGER_PROJECT_NO_JOB (the brief's QBO_PROJECT_NO_JOB)
    "LEDGER_PROJECT_NO_JOB": (
        'QuickBooks {kind} "{name}" has {count} billing or payment document{s} and no '
        "job. Link it to its job, or make the job first."
    ),
    # new: §10 has none
    "JOB_DIVISION_UNSET": 'Job "{name}" has no division. Set the division on the job.',
    # §10 CUSTOMER_FUZZY: the duplicates list
    "CUSTOMER_FUZZY": (
        'QuickBooks customers "{a}" and "{b}" look like one customer. Merge in '
        "QuickBooks; the platform follows."
    ),
}


def sentence(code: str, /, **values) -> str:
    return SENTENCES[code].format(**values)


def _age(sold_since: date | None, today: date) -> str:
    if sold_since is None:
        return ""
    days = (today - sold_since).days
    if days <= 0:
        return " (first received as sold today)"
    return f" (first received as sold {days} day{'s' if days != 1 else ''} ago)"


def unattached_issue(external_id: str, sold_since: date | None, today: date) -> Issue:
    return Issue(
        "EST_UNATTACHED",
        sentence("EST_UNATTACHED", external_id=external_id, age=_age(sold_since, today)),
        {
            "estimate": external_id,
            "sold_since": sold_since.isoformat() if sold_since else None,
            "age_days": (today - sold_since).days if sold_since else None,
        },
    )


def second_estimate_issue(external_id: str, jobs: Sequence[tuple[UUID, str]]) -> Issue:
    names = ", ".join(f'"{name}"' for _id, name in jobs)
    return Issue(
        "JOB_SECOND_ESTIMATE_FOR_CUSTOMER",
        sentence("JOB_SECOND_ESTIMATE_FOR_CUSTOMER", external_id=external_id, jobs=names),
        {"estimate": external_id, "candidates": [str(i) for i, _n in jobs]},
    )


@dataclass(frozen=True)
class JobState:
    id: UUID
    name: str
    status: str
    division_id: UUID | None
    qbo_alias_count: int


def job_issues(job: JobState) -> list[Issue]:
    out: list[Issue] = []
    if job.division_id is None:
        out.append(Issue("JOB_DIVISION_UNSET", sentence("JOB_DIVISION_UNSET", name=job.name), {}))
    if job.status in OPEN_STATUSES and job.qbo_alias_count == 0:
        out.append(Issue("JOB_NO_LEDGER_LINK", sentence("JOB_NO_LEDGER_LINK", name=job.name), {}))
    return out


@dataclass(frozen=True)
class LedgerRow:
    id: UUID
    external_id: str
    display_name: str
    kind_label: str  # Project | Sub-customer
    documents: int  # billing and payment rows on this customer row
    aliased: bool


def ledger_issues(rows: Sequence[LedgerRow]) -> list[Issue]:
    """Owner's answers 3 and 20: a project or sub-customer with at least one billing or
    payment row and no job."""
    out: list[Issue] = []
    for r in rows:
        if r.documents > 0 and not r.aliased:
            out.append(
                Issue(
                    "LEDGER_PROJECT_NO_JOB",
                    sentence(
                        "LEDGER_PROJECT_NO_JOB",
                        kind=r.kind_label.lower(),
                        name=r.display_name,
                        count=r.documents,
                        s="" if r.documents == 1 else "s",
                    ),
                    {"customer_id": str(r.id), "external_id": r.external_id},
                )
            )
    return out
