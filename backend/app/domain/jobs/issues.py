"""Review items for jobs and the crosswalk (F07; BLUEPRINT §10 names where §10 has
one, owner's answer 18). Pure generators, computed on read; F09 persists them. Each
code has one sentence here (D-22: the sentence, never the code alone).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.domain.billing.figures import JobFigures, words
from app.domain.billing.work_areas import JobWorkAreas
from app.domain.estimates.exceptions import Issue
from app.domain.jobs.models import JOB_STATUS_LABELS, LINK_REQUIRED_STATUSES

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
        'Job "{name}" is {status} but not linked to QuickBooks, so its billing cannot be '
        "read. Link its project on the job's QuickBooks section; the project is created "
        "when the first money moves (D-35)."
    ),
    # §10 LEDGER_PROJECT_NO_JOB (the brief's QBO_PROJECT_NO_JOB); D-37: tracked rows only
    "LEDGER_PROJECT_NO_JOB": (
        'QuickBooks {kind} "{name}" is tracked and has no job{documents}. Link it to its '
        "job, or make the job first."
    ),
    # new: §10 has none
    "JOB_DIVISION_UNSET": 'Job "{name}" has no division. Set the division on the job.',
    # §10 CUSTOMER_FUZZY: the duplicates list
    "CUSTOMER_FUZZY": (
        'QuickBooks customers "{a}" and "{b}" look like one customer. Merge in '
        "QuickBooks; the platform follows."
    ),
    # F08 (D-02): money received on the job with no invoice to apply it to
    "PAYMENT_UNAPPLIED": (
        'Job "{name}" has {amount} received and not applied to any invoice ({payments}). '
        "It is not billed or collected to date until it is applied in QuickBooks (D-02)."
    ),
    # F08 (D-02, owner's answer 2): one of the two deposit marks without the other
    "DEPOSIT_NOT_IDENTIFIED": (
        '{kind} {document} on job "{name}" is not identified as the deposit: {reason}. '
        "It counts in billed to date; fix the item or the document number in QuickBooks "
        "(D-02)."
    ),
    # F08.2 (D-41): an invoice settled through a payment by something other than cash
    "PAYMENT_OTHER_CREDIT": (
        'Job "{name}" has {amount} of other credits applied on {date} (payment {payment}): '
        "an invoice was settled through a payment by something other than cash, such as a "
        "journal entry or a deposit. Collected to date leaves it out (D-41)."
    ),
    # F07.4 (D-42, rule C): an approval ended by a later version of the estimate
    "CO_APPROVAL_NOT_CARRIED": (
        'Work area #{order_no} "{name}" on estimate {estimate} was approved on {agreed_on} '
        "at {approved} by {who}, and {ended}, so it is unapproved until it is approved "
        "again (D-42)."
    ),
    # F08.1 (D-45): billing on change orders no one has approved; a flag, never an
    # adjustment, and it never mentions payment
    "BILLING_UNAPPROVED_CO": (
        '{amount} has been billed on job "{name}" on {count} that {verb} not approved: '
        "{labels} (D-45)."
    ),
    # §10 BILLED_OVER_CONTRACT
    "BILLED_OVER_CONTRACT": (
        'Job "{name}" is billed {over} over its revised contract ({billed} billed against '
        "{contract}): likely a change order not yet approved."
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
    if job.status in LINK_REQUIRED_STATUSES and job.qbo_alias_count == 0:
        status = JOB_STATUS_LABELS[job.status]
        out.append(
            Issue(
                "JOB_NO_LEDGER_LINK",
                sentence("JOB_NO_LEDGER_LINK", name=job.name, status=status),
                {"status": job.status},
            )
        )
    return out


@dataclass(frozen=True)
class LedgerRow:
    id: UUID
    external_id: str
    display_name: str
    kind_label: str  # Project | Sub-customer | Customer
    documents: int  # billing and payment rows on this customer row
    aliased: bool
    tracked: bool = False  # D-37: picked by a person or linked to a job
    active: bool = True  # D-37 (owner, 2026-10-04): an inactive tracked row raises nothing


def _documents(count: int) -> str:
    if count == 0:
        return ""
    return f" ({count} billing or payment document{'' if count == 1 else 's'})"


def ledger_issue(r: LedgerRow) -> Issue | None:
    """D-37: a tracked, active row with no job, whether or not it has documents yet; no
    other row raises anything (the F07 rule, any project or sub-customer with a
    document, is replaced)."""
    if not (r.tracked and r.active and not r.aliased):
        return None
    return Issue(
        "LEDGER_PROJECT_NO_JOB",
        sentence(
            "LEDGER_PROJECT_NO_JOB",
            kind=r.kind_label.lower(),
            name=r.display_name,
            documents=_documents(r.documents),
        ),
        {"customer_id": str(r.id), "external_id": r.external_id, "documents": r.documents},
    )


def ledger_issues(rows: Sequence[LedgerRow]) -> list[Issue]:
    return [issue for r in rows if (issue := ledger_issue(r)) is not None]


# --- F08: the billing side ------------------------------------------------------------------


def billing_issues(name: str, figures: JobFigures) -> list[Issue]:
    """The three F08 review items of one job, in the brief's order, then D-41's one
    sentence per payment with other credits applied; pure, from the figures
    ``app.domain.billing.figures`` computed."""
    out: list[Issue] = []
    if figures.unapplied_count:
        n = figures.unapplied_count
        out.append(
            Issue(
                "PAYMENT_UNAPPLIED",
                sentence(
                    "PAYMENT_UNAPPLIED",
                    name=name,
                    amount=words(figures.unapplied_payments),
                    payments=f"{n} payment{'' if n == 1 else 's'}",
                ),
                {"amount": str(figures.unapplied_payments), "payments": n},
            )
        )
    for d in figures.deposit_mismatches:
        out.append(
            Issue(
                "DEPOSIT_NOT_IDENTIFIED",
                sentence(
                    "DEPOSIT_NOT_IDENTIFIED",
                    kind=d.kind_label,
                    document=d.doc.doc_number or d.doc.external_id,
                    name=name,
                    reason=d.deposit_reason,
                ),
                {
                    "billing_id": d.doc.id,
                    "external_id": d.doc.external_id,
                    "reason": d.deposit_reason,
                },
            )
        )
    over = figures.over_contract
    if (
        over is not None
        and figures.billed_to_date is not None
        and figures.remaining_to_bill is not None
    ):
        contract = figures.billed_to_date + figures.remaining_to_bill
        out.append(
            Issue(
                "BILLED_OVER_CONTRACT",
                sentence(
                    "BILLED_OVER_CONTRACT",
                    name=name,
                    over=words(over),
                    billed=words(figures.billed_to_date),
                    contract=words(contract),
                ),
                {
                    "over": str(over),
                    "billed": str(figures.billed_to_date),
                    "contract": str(contract),
                },
            )
        )
    for p in sorted(figures.payments, key=lambda r: (r.txn_date, r.external_id)):
        if p.on_this_job and not p.deleted and p.other_credit:
            out.append(
                Issue(
                    "PAYMENT_OTHER_CREDIT",
                    sentence(
                        "PAYMENT_OTHER_CREDIT",
                        name=name,
                        amount=words(p.other_credit),
                        date=p.txn_date.isoformat(),
                        payment=p.external_id,
                    ),
                    {
                        "amount": str(p.other_credit),
                        "date": p.txn_date.isoformat(),
                        "payment_id": p.payment_id,
                        "external_id": p.external_id,
                    },
                )
            )
    return out


def approval_not_carried_issue(
    *,
    order_no: int,
    name: str,
    estimate: str,
    agreed_on: date,
    approved: Decimal,
    who: str,
    ended: str,
    approval_id: UUID,
) -> Issue:
    """F07.4 (D-42, rule C): one sentence per ended approval that no one has withdrawn or
    replaced, naming the approved price and date and what the later version did."""
    return Issue(
        "CO_APPROVAL_NOT_CARRIED",
        sentence(
            "CO_APPROVAL_NOT_CARRIED",
            order_no=order_no,
            name=name,
            estimate=estimate,
            agreed_on=agreed_on.isoformat(),
            approved=words(approved),
            who=who,
            ended=ended,
        ),
        {
            "order_no": order_no,
            "estimate": estimate,
            "approved_price": str(approved),
            "agreed_on": agreed_on.isoformat(),
            "approval": str(approval_id),
        },
    )


def unapproved_co_billing_issue(name: str, wa: JobWorkAreas) -> Issue | None:
    """F08.1 (D-45): one sentence per job naming the amount billed on kept change orders
    without an applying approval and their numbers; nothing when it is 0.00."""
    if wa.unapproved_billed <= Decimal("0.00"):
        return None
    n = len(wa.unapproved_labels)
    return Issue(
        "BILLING_UNAPPROVED_CO",
        sentence(
            "BILLING_UNAPPROVED_CO",
            amount=words(wa.unapproved_billed),
            name=name,
            count=f"{n} change order{'' if n == 1 else 's'}",
            verb="is" if n == 1 else "are",
            labels=", ".join(wa.unapproved_labels),
        ),
        {"amount": str(wa.unapproved_billed), "work_areas": list(wa.unapproved_labels)},
    )
