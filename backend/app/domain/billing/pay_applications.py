"""The pay application (F08.1 Part 2; D-26, D-36, D-39, D-42, D-43; BLUEPRINT §9). Pure:
``Decimal`` in and out, no database, no clock. ``pay_application_service.py`` reads the
rows and calls this; the API and the PDF are built from the same ``ApplicationView``.

- **Schedule of values** (D-36): every kept work area confirmed original on the job's
  original estimate and every kept change-order work area with an approval that applies
  on the application date (D-42). Unapproved change orders and omitted work areas are not
  listed; no retainage (D-43).
- **A billing request** (D-26): cumulative percent complete per work area, 0.00 to 100.00,
  and optionally one percent for every listed work area not named. Exceptions, one
  sentence each, none stopping the other lines: over 100 (``BILLING_OVER_100``), a work
  area priced 0.00 (``BILLING_UNPRICED_CO``), an omitted work area
  (``BILLING_OMITTED_AREA``), a percent whose earned to date is below what is already
  earned on the work area (``BILLING_NEGATIVE``; F08.3: the comparison is on amounts,
  whatever the source of the previous figure, so no application ever shows a negative
  "earned this application"). A work area with one of the first three is left off the
  draft; one that raises ``BILLING_NEGATIVE`` stays listed at its previous percent with
  earned this application 0.00 and the sentence beside it, the entered percent in the
  sentence only (the owner's answer A, 2026-10-07). The check runs when the draft is
  saved and again whenever a draft is read (``held_lines``), so a draft saved before
  F08.3 reads the same way.
- **Per line**: scheduled value (the price that day); percent complete to date; earned
  to date = scheduled value × percent, ``ROUND_HALF_UP`` to the cent per work area;
  earned on previous applications (the latest earlier issued application that lists the
  work area; on a job's first application the scheduled value × the percent that the
  "#n" and assigned lines' billing stands for, half up to the hundredth, D-45 and
  F08.3 question 2, option 3: the choice the owner delegated to the design partner on
  2026-10-07; the amount due is unchanged under every option because billed before is
  the job's whole billed to date); earned this application; balance to finish. That
  percent is also the form's prefill (``prefill_percent``).
- **Summary**: total earned to date; less billed to date before this application (the
  job's whole billed to date as QuickBooks has it over every counted document dated on
  or before the application date, the owner's answer A, 2026-10-07, never counting the
  invoice keyed from this application or a later one); amount due, never below 0.00;
  when billed exceeds earned, "Billed ahead by x" and no invoice is due. The fuel
  surcharge (D-39): amount due × the rate, ``ROUND_HALF_UP`` to the cent, when the
  person says it applies; then the total to invoice.
- **Numbering** (owner's answer B): a job's first application takes one more than the
  highest ``_PMT`` number on its invoices; later ones continue.
- **The tie** (D-36, D-39): an invoice numbered ``<estimate number>_PMT<n>`` is the
  application's; its fuel surcharge lines equal the printed surcharge and its total less
  those lines equals the amount due. ``PAYAPP_NOT_INVOICED``, ``INVOICE_NO_PAYAPP``
  (invoices dated on or after the job's first issued application only) and
  ``PAYAPP_INVOICE_MISMATCH`` are review sentences, computed on read.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.domain.billing.figures import DEPOSIT_SUFFIX, ZERO, DocFigures, words
from app.domain.billing.work_areas import AreaKey, AreaRef
from app.domain.estimates.exceptions import Issue
from app.domain.jobs.issues import sentence

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
PMT = re.compile(r"^(?P<estimate>.+)_PMT(?P<n>\d+)$")


def pmt_number(doc_number: str | None) -> tuple[str, int] | None:
    m = PMT.match((doc_number or "").strip())
    return (m.group("estimate"), int(m.group("n"))) if m else None


def invoice_number(estimate_number: str, n: int) -> str:
    return f"{estimate_number}_PMT{n}"


def next_number(
    doc_numbers: Iterable[str | None], estimate_numbers: frozenset[str], taken: Iterable[int]
) -> int:
    """Owner's answer B: one more than the highest ``_PMT`` number on the job's documents
    (for one of its estimates) and than any application number already taken."""
    highest = 0
    for number in doc_numbers:
        parsed = pmt_number(number)
        if parsed and parsed[0] in estimate_numbers:
            highest = max(highest, parsed[1])
    return max(highest, *taken, 0) + 1


def earned(scheduled_value: Decimal, percent: Decimal) -> Decimal:
    """Scheduled value × percent, quantized half up to the cent per work area (D-36)."""
    return (scheduled_value * percent / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def surcharge_amount(amount_due: Decimal, rate: Decimal) -> Decimal:
    return (amount_due * rate).quantize(CENT, rounding=ROUND_HALF_UP)


def prefill_percent(scheduled_value: Decimal, billed: Decimal) -> Decimal:
    """F08.3 (question 2, option 3): the percent an earlier billing stands for, billed over
    the scheduled value, ``ROUND_HALF_UP`` to the hundredth, held between 0.00 and 100.00
    (a credit beyond the billing, or billing above the price, is read as 0.00 or 100.00:
    the residue sits in the summary's billed before, the job's whole billed to date)."""
    if scheduled_value <= ZERO or billed <= ZERO:
        return ZERO
    pct = (billed / scheduled_value * HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)
    return min(pct, HUNDRED)


# --- the schedule and the request -----------------------------------------------------------


def schedule(
    areas: Sequence[AreaRef], application_date: date, approved_on: Mapping[AreaKey, date]
) -> list[AreaRef]:
    """D-36, D-42: the kept originals and the kept change orders approved on the date;
    ``approved_on``: per work area with an applying approval, the date the customer agreed."""
    out = []
    for a in areas:
        if not a.kept:
            continue
        if a.role == "original" and a.kind == "original":
            out.append(a)
        elif (
            a.change_order
            and a.approved
            and approved_on.get(a.key, application_date) <= application_date
        ):
            out.append(a)
    out.sort(key=lambda a: (a.role != "original", a.estimate_number, a.order_no))
    return out


@dataclass(frozen=True)
class PercentIn:
    estimate_work_area_id: str
    percent: Decimal


@dataclass(frozen=True)
class RequestIn:
    application_date: date
    surcharge_applies: bool | None
    percents: tuple[PercentIn, ...]
    apply_all: Decimal | None = None  # D-26: one percent for every listed work area not named


@dataclass(frozen=True)
class PreviousIn:
    """What the latest earlier issued application says about a work area, or, when none
    lists it, the percent the "#n" and assigned lines' billing stands for and the
    scheduled value at that percent (D-45; F08.3 option 3)."""

    percent: Decimal | None  # the previous percent; None only when nothing is known
    earned: Decimal  # earned on previous applications
    number: int | None = None  # the earlier application; None before the first one


@dataclass(frozen=True)
class DraftLine:
    area: AreaRef
    percent: Decimal  # the percent the line stands at (the previous one when held)
    issue: Issue | None = None  # BILLING_NEGATIVE, beside the line (answer A)


@dataclass(frozen=True)
class Draft:
    lines: tuple[DraftLine, ...]
    issues: tuple[Issue, ...]


class NotOnSchedule(ValueError):
    """A percent for a work area the schedule of values does not list (the form never
    offers one; the service refuses the request in one sentence)."""


def _exception(code: str, area: AreaRef, **values) -> Issue:
    text = sentence(code, order_no=area.order_no, name=area.name, **values)
    return Issue(
        code,
        text,
        {
            "order_no": area.order_no,
            "estimate": area.estimate_number,
            **{k: str(v) for k, v in values.items()},
        },
    )


def negative_issue(
    area: AreaRef, percent: Decimal, prev: PreviousIn | None
) -> tuple[Decimal, Issue | None]:
    """D-26, F08.3: a percent whose earned to date is below what is already earned on the
    work area raises ``BILLING_NEGATIVE``; the line then stands at its previous percent
    (answer A). Returns the percent the line stands at and the sentence, if any."""
    if prev is None or prev.percent is None or earned(area.price, percent) >= prev.earned:
        return percent, None
    stays = prev.percent
    where = (
        f"on pay application {prev.number}"
        if prev.number is not None
        else "by the invoice lines tied to it"
    )
    return stays, _exception(
        "BILLING_NEGATIVE",
        area,
        percent=f"{percent.quantize(CENT)}",
        earns=words(earned(area.price, percent)),
        previous=words(prev.earned),
        where=where,
        stays=f"{stays.quantize(CENT)}",
    )


def draft_lines(
    request: RequestIn,
    listed: Sequence[AreaRef],
    all_areas: Sequence[AreaRef],
    previous: Mapping[AreaKey, PreviousIn],
) -> Draft:
    """The draft's lines from a request: every listed work area with a percent (named,
    else ``apply_all``, else the previous percent, else 0.00), the D-26 exceptions one
    sentence each; a work area over 100.00, priced 0.00 or omitted is left off; one whose
    earned to date would fall stays at its previous percent with the sentence beside it
    (answer A); the entered percent is stored so the check can run again on read."""
    by_id = {a.work_area_id: a for a in all_areas}
    on_schedule = {a.key for a in listed}
    named: dict[AreaKey, Decimal] = {}
    issues: list[Issue] = []
    for p in request.percents:
        area = by_id.get(p.estimate_work_area_id)
        if area is None:
            raise NotOnSchedule("That work area is not on an estimate of this job.")
        if not area.kept:
            issues.append(_exception("BILLING_OMITTED_AREA", area))
            continue
        if area.key not in on_schedule:
            raise NotOnSchedule(
                f"Work area #{area.order_no} is not on the schedule of values: it is a change "
                "order that is not approved, or a work area not confirmed as original (D-36)."
            )
        named[area.key] = p.percent
    lines: list[DraftLine] = []
    for area in listed:
        if area.key in named:
            percent, requested = named[area.key], True
        elif request.apply_all is not None:
            percent, requested = request.apply_all, True
        else:
            prev = previous.get(area.key)
            percent = prev.percent if prev is not None and prev.percent is not None else ZERO
            requested = False
        prev = previous.get(area.key)
        if percent > HUNDRED:
            issues.append(_exception("BILLING_OVER_100", area, percent=f"{percent.quantize(CENT)}"))
            continue
        if area.price == ZERO:
            if requested:
                issues.append(_exception("BILLING_UNPRICED_CO", area))
            continue
        _stays, issue = negative_issue(area, percent, prev)
        if issue is not None:
            issues.append(issue)
        lines.append(DraftLine(area, percent.quantize(CENT), issue))
    return Draft(tuple(lines), tuple(issues))


# --- the application as shown and printed ----------------------------------------------------


@dataclass(frozen=True)
class LineView:
    estimate_id: str
    estimate_number: str
    role: str
    work_area_id: str
    order_no: int
    name: str
    scheduled_value: Decimal
    percent: Decimal
    earned_to_date: Decimal
    previous: Decimal
    this_application: Decimal
    balance: Decimal
    issue: Issue | None = None  # BILLING_NEGATIVE beside a held line (a draft; answer A)

    @property
    def key(self) -> AreaKey:
        return (self.estimate_id, self.order_no)

    @property
    def label(self) -> str:
        return (
            f"#{self.order_no}"
            if self.role == "original"
            else f"#{self.order_no} of {self.estimate_number}"
        )


@dataclass(frozen=True)
class StoredLine:
    estimate_id: str
    estimate_number: str
    role: str
    work_area_id: str
    order_no: int
    name: str
    scheduled_value: Decimal
    percent: Decimal


def _area_of(ln: StoredLine) -> AreaRef:
    return AreaRef(
        estimate_id=ln.estimate_id,
        estimate_number=ln.estimate_number,
        role=ln.role,
        work_area_id=ln.work_area_id,
        order_no=ln.order_no,
        name=ln.name,
        kept=True,
        price=ln.scheduled_value,
        kind=None,
    )


def line_views(
    lines: Sequence[StoredLine], previous: Mapping[AreaKey, PreviousIn], *, draft: bool = False
) -> list[LineView]:
    """The lines as shown and printed. For a draft the negative check runs again
    (``held_lines``), so a line whose stored percent earns less than what is already
    earned stands at its previous percent with 0.00 this application and its sentence;
    an issued application's lines were held at issue and are read as stored."""
    out = []
    for ln in lines:
        prev = previous.get((ln.estimate_id, ln.order_no))
        percent, issue = (
            negative_issue(_area_of(ln), ln.percent, prev) if draft else (ln.percent, None)
        )
        to_date = earned(ln.scheduled_value, percent)
        before = prev.earned if prev is not None else ZERO
        out.append(
            LineView(
                estimate_id=ln.estimate_id,
                estimate_number=ln.estimate_number,
                role=ln.role,
                work_area_id=ln.work_area_id,
                order_no=ln.order_no,
                name=ln.name,
                scheduled_value=ln.scheduled_value,
                percent=percent,
                earned_to_date=to_date,
                previous=before,
                this_application=to_date - before,
                balance=ln.scheduled_value - to_date,
                issue=issue,
            )
        )
    return out


def held_lines(views: Sequence[LineView]) -> list[Issue]:
    """The ``BILLING_NEGATIVE`` sentences of a draft's held lines, in line order."""
    return [v.issue for v in views if v.issue is not None]


@dataclass(frozen=True)
class Summary:
    earned_to_date: Decimal
    billed_before: Decimal
    amount_due: Decimal  # never below 0.00
    billed_ahead: Decimal | None  # when billed before exceeds earned
    surcharge_applies: bool | None
    surcharge_rate: Decimal | None  # the tenant's rate; None when undecided or not applying
    surcharge: Decimal | None  # the printed line; None when it does not apply
    total_to_invoice: Decimal | None  # None when no invoice is due

    @property
    def no_invoice_due(self) -> bool:
        return self.amount_due <= ZERO


def summary(
    earned_total: Decimal,
    billed_before: Decimal,
    surcharge_applies: bool | None,
    rate: Decimal | None,
) -> Summary:
    due = earned_total - billed_before
    ahead = -due if due < ZERO else None
    due = max(due, ZERO)
    charge = None
    if surcharge_applies and rate is not None and due > ZERO:
        charge = surcharge_amount(due, rate)
    total = None if due <= ZERO else due + (charge or ZERO)
    return Summary(
        earned_to_date=earned_total,
        billed_before=billed_before,
        amount_due=due,
        billed_ahead=ahead,
        surcharge_applies=surcharge_applies,
        surcharge_rate=rate if surcharge_applies else None,
        surcharge=charge,
        total_to_invoice=total,
    )


def billed_before(
    documents: Sequence[DocFigures],
    application_date: date,
    estimate_numbers: frozenset[str],
    number: int,
) -> Decimal | None:
    """Owner's answer A: the job's whole billed to date over every counted document dated
    on or before the application date (the F08 figure per document), never counting an
    invoice numbered ``_PMT<m>`` with m ≥ this application's number. None while the
    policy keys the figure needs are undecided."""
    total = ZERO
    for d in documents:
        if not d.counted or d.doc.txn_date > application_date:
            continue
        parsed = pmt_number(d.doc.doc_number)
        if parsed and parsed[0] in estimate_numbers and parsed[1] >= number:
            continue
        if d.billed is None:
            return None
        total += d.billed
    return total


@dataclass(frozen=True)
class ApplicationView:
    id: str
    number: int
    estimate_number: str  # the original estimate's, for the invoice number
    application_date: date
    status: str  # draft | issued | void
    surcharge_applies: bool | None
    lines: tuple[LineView, ...]
    summary: Summary
    billed_before_undecided: bool  # a draft on a job whose policy keys are undecided
    rate_undecided: bool  # the surcharge is chosen and the tenant's rate is not decided
    created_by: str | None
    created_at: str
    issued_by: str | None
    issued_at: str | None
    voided_by: str | None
    voided_at: str | None
    void_reason: str | None
    issues: tuple[Issue, ...] = ()  # the request's exceptions (on the draft response)
    invoice: DocFigures | None = None  # the matched invoice, when the copy holds it

    @property
    def invoice_number(self) -> str | None:
        return (
            None
            if self.summary.no_invoice_due
            else invoice_number(self.estimate_number, self.number)
        )

    @property
    def total_this_application(self) -> Decimal:
        return sum((ln.this_application for ln in self.lines), ZERO)

    @property
    def allocations(self) -> dict[AreaKey, Decimal]:
        """D-36: billed to date per work area from this application, earned this application."""
        return {ln.key: ln.this_application for ln in self.lines}


# --- the tie (D-36, D-39) --------------------------------------------------------------------


def match_invoice(view: ApplicationView, documents: Sequence[DocFigures]) -> DocFigures | None:
    number = invoice_number(view.estimate_number, view.number)
    for d in documents:
        if d.counted and d.doc.kind == "invoice" and (d.doc.doc_number or "").strip() == number:
            return d
    return None


def tie_issues(
    name: str, views: Sequence[ApplicationView], documents: Sequence[DocFigures]
) -> list[Issue]:
    issued = [v for v in views if v.status == "issued"]
    out: list[Issue] = []
    matched: set[str] = set()
    for v in sorted(issued, key=lambda v: v.number):
        inv = v.invoice
        if inv is None:
            if not v.summary.no_invoice_due:
                out.append(
                    Issue(
                        "PAYAPP_NOT_INVOICED",
                        sentence(
                            "PAYAPP_NOT_INVOICED",
                            number=v.number,
                            name=name,
                            date=v.application_date.isoformat(),
                            amount=words(v.summary.total_to_invoice or v.summary.amount_due),
                            invoice=invoice_number(v.estimate_number, v.number),
                        ),
                        {"application": v.id, "number": v.number},
                    )
                )
            continue
        matched.add(inv.doc.id)
        printed = v.summary.surcharge or ZERO
        lines = inv.surcharge if inv.surcharge is not None else ZERO
        total_less = inv.doc.total - lines
        parts = []
        if total_less != v.summary.amount_due:
            parts.append(
                f"the invoice less its fuel surcharge lines is {words(total_less)} against an "
                f"amount due of {words(v.summary.amount_due)}, a difference of "
                f"{words(total_less - v.summary.amount_due)}"
            )
        if lines != printed:
            parts.append(
                f"its fuel surcharge lines are {words(lines)} against {words(printed)} printed"
            )
        if parts:
            out.append(
                Issue(
                    "PAYAPP_INVOICE_MISMATCH",
                    sentence(
                        "PAYAPP_INVOICE_MISMATCH",
                        invoice=inv.doc.doc_number,
                        name=name,
                        number=v.number,
                        detail="; ".join(parts),
                    ),
                    {
                        "application": v.id,
                        "billing_id": inv.doc.id,
                        "difference": str(total_less - v.summary.amount_due),
                    },
                )
            )
    if issued:
        first = min(v.application_date for v in issued)
        for d in documents:
            if (
                d.counted
                and d.doc.kind == "invoice"
                and d.doc.txn_date >= first
                and d.doc.id not in matched
                and not d.is_deposit
                and not (d.doc.doc_number or "").strip().endswith(DEPOSIT_SUFFIX)
            ):
                out.append(
                    Issue(
                        "INVOICE_NO_PAYAPP",
                        sentence(
                            "INVOICE_NO_PAYAPP",
                            invoice=d.doc.doc_number or f"QuickBooks id {d.doc.external_id}",
                            name=name,
                            date=d.doc.txn_date.isoformat(),
                            first=first.isoformat(),
                        ),
                        {"billing_id": d.doc.id},
                    )
                )
    return out


@dataclass(frozen=True)
class AppAllocation:
    """Part 1's slot: the matched invoice of an issued application and what the
    application billed per work area (earned this application)."""

    billing_id: str
    number: int
    allocations: dict[AreaKey, Decimal] = field(default_factory=dict)


def allocations(views: Sequence[ApplicationView]) -> list[AppAllocation]:
    return [
        AppAllocation(v.invoice.doc.id, v.number, v.allocations)
        for v in views
        if v.status == "issued" and v.invoice is not None
    ]
