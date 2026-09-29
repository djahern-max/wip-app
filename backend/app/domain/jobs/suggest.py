"""Suggestions for a person (F07; D-01, D-03, §5, §13.2). Pure: rows in, labelled
suggestions out. Nothing here decides anything or is called from a write path; the
person picks, and the service writes only what the person picked, by id.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from app.domain.jobs.names import (
    customer_name_key,
    estimate_id_digits,
    name_starts_with_estimate_id,
    normalized,
    street_tokens,
)

ZERO = Decimal("0.00")

# Reasons, as a person reads them (D-22).
SAME_CUSTOMER = "same customer"
BY_CLIENT_NAME = "by client name only"
BY_NAME = "by name only"
ID_IN_NAME = "estimate id in name"
CUSTOMER_NAME = "customer name"
ADDRESS = "address"
CUSTOMER_NAME_AND_ADDRESS = "customer name and address"


# --- division (the division that carries the most cost) ------------------------------


def suggest_division(costs: Iterable[tuple[UUID | None, Decimal]]) -> UUID | None:
    """``costs``: (division id, amount) for each cost line that counts toward the
    estimate's totals. The division with the most cost; a tie, or no cost, suggests
    nothing."""
    by_division: dict[UUID, Decimal] = {}
    for division_id, amount in costs:
        if division_id is not None:
            by_division[division_id] = by_division.get(division_id, ZERO) + amount
    if not by_division:
        return None
    ranked = sorted(by_division.items(), key=lambda kv: kv[1], reverse=True)
    if ranked[0][1] <= ZERO or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
        return None
    return ranked[0][0]


# --- work-area kind (D-01) ---------------------------------------------------------


def suggested_kind(change_order_suggested: bool) -> str:
    """F06's flag already carries both the "CO:" / "C/O" name rule and the D-01
    "appeared after the baseline" rule."""
    return "change_order" if change_order_suggested else "original"


# --- QuickBooks rows ---------------------------------------------------------------


@dataclass(frozen=True)
class CustomerRow:
    id: UUID
    external_id: str
    display_name: str
    parent_id: UUID | None
    parent_name: str | None
    is_project: bool
    active: bool

    @property
    def paying_customer_id(self) -> UUID:
        """The customer the job belongs to: the parent of a project or sub-customer,
        the row itself when it has none."""
        return self.parent_id or self.id

    @property
    def kind_label(self) -> str:
        if self.is_project:
            return "Project"
        return "Sub-customer" if self.parent_id else "Customer"


@dataclass(frozen=True)
class EstimateText:
    external_id: str
    client_name: str | None
    jobsite: str | None
    name: str | None


@dataclass(frozen=True)
class QboCandidate:
    row: CustomerRow
    reason: str
    rank: int  # 0 id in name, 1 customer name and address, 2 one of the two


def _suggestable(row: CustomerRow, aliased: set[str]) -> bool:
    """Suggestions come from projects and sub-customers only (owner's answer 3)."""
    return (
        row.active
        and (row.is_project or row.parent_id is not None)
        and (row.external_id not in aliased)
    )


def qbo_candidates(
    estimates: Sequence[EstimateText], rows: Sequence[CustomerRow], aliased: set[str]
) -> list[QboCandidate]:
    """The three exact rules (brief, QuickBooks link; owner's answer 11), each
    labelled. A row matching no rule is not suggested; nothing is linked here."""
    digits = [d for d in (estimate_id_digits(e.external_id) for e in estimates) if d]
    client_keys = {k for k in (customer_name_key(e.client_name) for e in estimates) if k}
    places: set[str] = set()
    for e in estimates:
        places |= street_tokens(e.jobsite) | street_tokens(e.name)
    out: list[QboCandidate] = []
    for row in rows:
        if not _suggestable(row, aliased):
            continue
        by_id = any(name_starts_with_estimate_id(row.display_name, d) for d in digits)
        owner_key = customer_name_key(row.parent_name if row.parent_id else row.display_name)
        by_name = owner_key is not None and owner_key in client_keys
        by_address = bool(street_tokens(row.display_name) & places)
        if by_id:
            out.append(QboCandidate(row, ID_IN_NAME, 0))
        elif by_name and by_address:
            out.append(QboCandidate(row, CUSTOMER_NAME_AND_ADDRESS, 1))
        elif by_name:
            out.append(QboCandidate(row, CUSTOMER_NAME, 2))
        elif by_address:
            out.append(QboCandidate(row, ADDRESS, 2))
    return sorted(out, key=lambda c: (c.rank, c.row.display_name.casefold(), c.row.external_id))


# --- attach candidates for a sold estimate in the queue (D-03) ------------------------


@dataclass(frozen=True)
class JobForMatch:
    id: UUID
    name: str
    revenue_method: str
    customer_id: UUID | None
    customer_name: str | None
    original: EstimateText | None


@dataclass(frozen=True)
class AttachCandidate:
    job: JobForMatch
    reasons: tuple[str, ...]

    @property
    def same_customer(self) -> bool:
        """D-03: the estimate's customer already has this job (by id or by client
        name); raises JOB_SECOND_ESTIMATE_FOR_CUSTOMER."""
        return SAME_CUSTOMER in self.reasons or BY_CLIENT_NAME in self.reasons


def attach_candidates(
    estimate: EstimateText, jobs: Sequence[JobForMatch], rows: Sequence[CustomerRow]
) -> list[AttachCandidate]:
    """Jobs a person may want to attach ``estimate`` to, each with its reasons:
    "same customer" (a QuickBooks row named with this estimate's id belongs to the
    job's customer), "by client name only" (client text equals the job's customer's
    name or its original estimate's client text), "by name only" (jobsite or name
    text equal to the job's original estimate's). Pool jobs are never candidates."""
    digits = estimate_id_digits(estimate.external_id)
    id_customers = (
        {r.paying_customer_id for r in rows if name_starts_with_estimate_id(r.display_name, digits)}
        if digits
        else set()
    )
    client_key = customer_name_key(estimate.client_name)
    jobsite = normalized(estimate.jobsite)
    name = normalized(estimate.name)
    out: list[AttachCandidate] = []
    for job in jobs:
        if job.revenue_method == "pool":
            continue
        reasons: list[str] = []
        if job.customer_id is not None and job.customer_id in id_customers:
            reasons.append(SAME_CUSTOMER)
        if client_key is not None and client_key in {
            customer_name_key(job.customer_name),
            customer_name_key(job.original.client_name if job.original else None),
        }:
            reasons.append(BY_CLIENT_NAME)
        if job.original is not None and (
            (jobsite and jobsite == normalized(job.original.jobsite))
            or (name and name == normalized(job.original.name))
        ):
            reasons.append(BY_NAME)
        if reasons:
            out.append(AttachCandidate(job, tuple(reasons)))
    return sorted(out, key=lambda c: (not c.same_customer, c.job.name.casefold(), str(c.job.id)))
