"""Billed to date per work area (F08.1 Part 1; D-45, D-26, D-36; BLUEPRINT §8.2 column
10). Pure: ``Decimal`` in and out, no database, no clock. Nothing here is stored;
``board.py`` reads the rows and calls this.

A line of an invoice, credit memo or sales receipt is **tied** to a work area, in this
order (D-45): by its pay application (Part 2; the slot is empty in Part 1); by "#n" at
the start of its description, where n is a work-area number on the job's original
estimate and on no other attached estimate (the owner's answer 1, 2026-10-07); by a
person's assignment, which follows the work-area number through later versions as a
"#n" line does (answer 5). A line with no tie counts in the job's billed to date and in
no work area's. The description is a **suggestion** only (a kept work area whose name
equals it under ``same_name``, exactly one); nothing else suggests, and no suggestion is
ever applied by rule (the owner's change a: no suggestion by line position).

Billed to date per work area = Σ of its tied lines' parts (a "#n" line and an
application line for their whole amount, an assigned line for the amount of each of its
rows), a credit memo's negative. "Not assigned to a work area" = the job's billed to date
less Σ of every part, so the
identity holds by construction: sales tax, fuel surcharge lines (D-39), discount lines
and the deposit invoice's lines (D-02) are never tied and sit in that complement.

``BILLING_UNAPPROVED_CO`` (D-45): billed to date on kept change-order work areas without
an applying approval (D-42), above 0.00, with their numbers; the sentence is in
``app/domain/jobs/issues.py`` (``unapproved_co_billing_issue``). A flag and nothing else.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.domain.billing.figures import SALES_ITEM, ZERO, DocFigures, LineIn, words
from app.domain.estimates.exceptions import sentence as estimate_sentence
from app.domain.estimates.versions import same_name

# "#n " at the start of a description (D-26); "#n" alone counts too.
HASH = re.compile(r"^\s*#(\d+)(?:\s|$)")
AreaKey = tuple[str, int]  # (estimate id, order number)
CENT = Decimal("0.01")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class AppTie:
    """Part 2: a document keyed from an issued pay application (matched by number) and
    what the application billed per work area; its lines are tied by the application."""

    billing_id: str
    number: int
    allocations: Mapping[AreaKey, Decimal]


HOW_LABELS = {
    "pay_application": "Pay application",
    "number": "By its number (#n)",
    "assigned": "Assigned",
    "none": "Not assigned",
}


@dataclass(frozen=True)
class AreaRef:
    """A work area of the latest version of an estimate attached to the job."""

    estimate_id: str
    estimate_number: str
    role: str  # original | change_order
    work_area_id: str
    order_no: int
    name: str
    kept: bool
    price: Decimal
    kind: str | None  # the confirmed kind on the original; None on a change-order estimate
    approved: bool = False  # an approval applies (D-42)

    @property
    def key(self) -> AreaKey:
        return (self.estimate_id, self.order_no)

    @property
    def change_order(self) -> bool:
        """A kept change order: confirmed as one on the original, or any kept row of an
        estimate attached as a change order (its role is the confirmation)."""
        if not self.kept:
            return False
        return self.kind == "change_order" if self.role == "original" else True

    @property
    def label(self) -> str:
        """ "#18" on the original; "#1 of EST6120638" on an estimate attached as a change order."""
        return (
            f"#{self.order_no}"
            if self.role == "original"
            else f"#{self.order_no} of {self.estimate_number}"
        )


@dataclass(frozen=True)
class AssignmentIn:
    """One current ``assigned`` row of a line (a ``cleared`` row is no assignment). A
    line has one such row, or several when it is split (F08.4, D-52): the rows of its
    latest event, each with the part's signed ``amount``."""

    id: str
    billing_line_id: str
    estimate_id: str
    order_no: int
    work_area_name: str
    recorded_by: str
    recorded_at: datetime
    amount: Decimal
    note: str | None = None

    @property
    def key(self) -> AreaKey:
        return (self.estimate_id, self.order_no)


@dataclass(frozen=True)
class LinePart:
    """What a line counts for on one work area (F08.4, D-52): the whole line for a "#n"
    or application line, one row's amount for an assigned line."""

    area: AreaRef
    amount: Decimal


@dataclass(frozen=True)
class LineTie:
    doc: DocFigures
    line: LineIn
    amount: Decimal  # signed: a credit memo's line is negative; 0.00 when not counted
    offered: bool  # a person may assign it
    not_offered: str | None  # why not, in words
    area: AreaRef | None  # the tied work area; None for a split (two or more parts)
    how: str  # pay_application | number | assigned | none
    assignment: AssignmentIn | None  # the first current row, tied or not (a dropped work area)
    note: str | None  # the renumbered sentence, a dropped work area, an ambiguous "#n"
    suggested: AreaRef | None  # by name; None when tied, not offered, or no single match
    parts: tuple[LinePart, ...] = ()  # what the line counts for, per work area
    assignments: tuple[AssignmentIn, ...] = ()  # every current row, tied or not

    @property
    def how_label(self) -> str:
        return HOW_LABELS[self.how]

    @property
    def split(self) -> bool:
        return len(self.parts) > 1


def hash_number(description: str | None) -> int | None:
    m = HASH.match(description or "")
    return int(m.group(1)) if m else None


def _by_order(areas: Sequence[AreaRef], n: int) -> list[AreaRef]:
    return [a for a in areas if a.order_no == n]


def resolve_hash(n: int, areas: Sequence[AreaRef]) -> tuple[AreaRef | None, str | None]:
    """The work area a "#n" line is tied to, or why it is not (owner's answer 1): tied
    only when the original estimate carries n and no other attached estimate does."""
    carrying = _by_order(areas, n)
    on_original = [a for a in carrying if a.role == "original"]
    if not on_original:
        return None, f"#{n} is not a work area of the original estimate; assign the line by hand."
    if len(carrying) > 1:
        numbers = ", ".join(sorted({a.estimate_number for a in carrying}))
        return None, (
            f"#{n} is a work area on more than one estimate of this job ({numbers}), so the "
            "number does not say which; assign the line by hand."
        )
    return on_original[0], None


def suggest_work_area(description: str | None, areas: Sequence[AreaRef]) -> AreaRef | None:
    """A kept work area whose name equals the description under ``same_name``; exactly
    one, or nothing. A suggestion for a person, never a decision."""
    if not (description or "").strip():
        return None
    matches = [a for a in areas if a.kept and same_name(a.name, description or "")]
    return matches[0] if len(matches) == 1 else None


def offered(
    doc: DocFigures, line: LineIn, surcharge_items: frozenset[str] | None
) -> tuple[bool, str | None]:
    """Whether a person may assign the line, or why not, in words."""
    if surcharge_items is None:
        return False, "Waits for the policy key Fuel surcharge treatment (Configuration, Policy)."
    if not doc.counted:
        return False, f"{doc.state_label} document: in no figure."
    if line.line_kind != SALES_ITEM:
        return False, "Not a priced line (a discount or a description line)."
    if line.item_external_id in surcharge_items:
        return False, "Fuel surcharge line (D-39): outside billed to date."
    if doc.is_deposit:
        return False, "The deposit invoice (D-02): netted at the job level, not by work area."
    return True, None


def tie_line(
    doc: DocFigures,
    line: LineIn,
    areas: Sequence[AreaRef],
    assignments: Sequence[AssignmentIn],
    *,
    surcharge_items: frozenset[str] | None,
    estimate_numbers: Mapping[str, str],
    application: AppTie | None = None,
) -> LineTie:
    """One line's tie in D-45's order. ``assignments``: the line's current ``assigned``
    rows (none, one, or several for a split, D-52). ``application``: the issued pay
    application the line's document was keyed from (Part 2), which ties every line of
    the document. ``estimate_numbers``: id → number of the estimates attached to the job."""
    amount = doc.sign * line.amount if doc.counted else ZERO
    can, why = offered(doc, line, surcharge_items)
    by_key = {a.key: a for a in areas}
    rows = tuple(assignments)

    def tie(
        area: AreaRef | None,
        how: str,
        note: str | None,
        suggested: bool,
        parts: tuple[LinePart, ...] | None = None,
    ) -> LineTie:
        if parts is None:
            parts = (LinePart(area, amount),) if area is not None else ()
        return LineTie(
            doc=doc,
            line=line,
            amount=amount,
            offered=can,
            not_offered=why,
            area=area,
            how=how,
            assignment=rows[0] if rows else None,
            note=note,
            suggested=suggest_work_area(line.description, areas) if suggested else None,
            parts=parts,
            assignments=rows,
        )

    if not can:
        return tie(None, "none", None, False)
    if application is not None:
        return tie(None, "pay_application", None, False)
    n = hash_number(line.description)
    if n is not None:
        area, note = resolve_hash(n, areas)
        if area is not None:
            return tie(area, "number", None, False)
        return (
            tie(None, "none", note, True)
            if not rows
            else _assigned(tie, rows, by_key, estimate_numbers, amount)
        )
    if rows:
        return _assigned(tie, rows, by_key, estimate_numbers, amount)
    return tie(None, "none", None, True)


def _assigned(
    tie,
    rows: tuple[AssignmentIn, ...],
    by_key: dict[AreaKey, AreaRef],
    numbers,
    line_amount: Decimal,
) -> LineTie:
    """The line's current rows against the latest versions: every row must find its work
    area, else the whole line counts as not assigned until a person acts (F08.1 Plan
    answer 2; a split is one decision and is not half kept)."""
    parts: list[LinePart] = []
    notes: list[str] = []
    for row in rows:
        area = by_key.get(row.key)
        if area is None:
            estimate = numbers.get(row.estimate_id)
            where = (
                f"the latest version of estimate {estimate}"
                if estimate
                else "any estimate on this job"
            )
            return tie(
                None,
                "none",
                f'Assigned to work area #{row.order_no} "{row.work_area_name}", which '
                f"is not on {where}; assign the line again or clear it.",
                True,
            )
        if not same_name(area.name, row.work_area_name):
            notes.append(
                estimate_sentence(
                    "EST_WORK_AREA_RENUMBERED",
                    order=area.order_no,
                    old=row.work_area_name,
                    new=area.name,
                )
            )
        parts.append(LinePart(area, row.amount if len(rows) > 1 else line_amount))
    if len(parts) > 1:
        # The pick-list's order: the original's work areas by number, then the estimates
        # attached as change orders by number (the rows of an event carry no order).
        parts.sort(
            key=lambda p: (p.area.role != "original", p.area.estimate_number, p.area.order_no)
        )
        total = sum((p.amount for p in parts), ZERO)
        if total != line_amount:
            notes.append(
                f"The parts sum to {words(total)} and the line is {words(line_amount)}; "
                "split the line again."
            )
        return tie(None, "assigned", " ".join(notes) or None, False, tuple(parts))
    return tie(parts[0].area, "assigned", " ".join(notes) or None, False)


@dataclass(frozen=True)
class JobWorkAreas:
    lines: tuple[LineTie, ...]
    billed: dict[AreaKey, Decimal]  # per work area: its tied lines and the pay applications
    not_assigned: Decimal | None  # the job's billed to date less Σ tied; None while undecided
    unapproved_billed: Decimal  # Σ billed on kept change orders without an approval
    unapproved_labels: tuple[str, ...]  # their labels, in estimate and number order
    suggested: int  # offered, untied lines with a suggestion
    billed_lines: dict[AreaKey, Decimal] = field(default_factory=dict)  # the lines alone
    application_of: dict[str, int] = field(default_factory=dict)  # document id → application

    def billed_on(self, area: AreaRef) -> Decimal:
        return self.billed.get(area.key, ZERO)

    def left_to_bill(self, area: AreaRef) -> Decimal | None:
        """Price less billed to date; None for an omitted work area and for a change
        order that is not approved (outside the contract)."""
        if not area.kept or (area.change_order and not area.approved):
            return None
        return area.price - self.billed_on(area)

    def percent_billed(self, area: AreaRef) -> Decimal | None:
        """F08.4 (D-52): billed to date ÷ scheduled value × 100, ``ROUND_HALF_UP`` to
        0.01, a display figure; None where left to bill is None or the price is 0.00."""
        if self.left_to_bill(area) is None or area.price == ZERO:
            return None
        return (self.billed_on(area) / area.price * HUNDRED).quantize(CENT, ROUND_HALF_UP)


def job_work_areas(
    documents: Sequence[DocFigures],
    billed_to_date: Decimal | None,
    areas: Sequence[AreaRef],
    assignments: Mapping[str, Sequence[AssignmentIn]],
    *,
    surcharge_items: frozenset[str] | None,
    applications: Sequence[AppTie] = (),
) -> JobWorkAreas:
    """``documents``: the job's documents, each with its lines; ``assignments``: the
    current ``assigned`` rows per line id (one, or the parts of a split); ``applications``:
    the documents keyed from issued pay applications (Part 2), first in D-45's order.
    Every figure is computed here and nowhere stored."""
    numbers = {a.estimate_id: a.estimate_number for a in areas}
    by_doc = {app.billing_id: app for app in applications}
    lines: list[LineTie] = []
    for d in sorted(documents, key=lambda f: (f.doc.txn_date, f.doc.external_id)):
        for ln in d.doc.lines:
            lines.append(
                tie_line(
                    d,
                    ln,
                    areas,
                    assignments.get(ln.id or "", ()),
                    surcharge_items=surcharge_items,
                    estimate_numbers=numbers,
                    application=by_doc.get(d.doc.id),
                )
            )
    billed_lines: dict[AreaKey, Decimal] = {a.key: ZERO for a in areas}
    tied = ZERO
    for t in lines:
        for part in t.parts:  # the row's amount, not the line's (D-52)
            billed_lines[part.area.key] = billed_lines.get(part.area.key, ZERO) + part.amount
            tied += part.amount
    billed = dict(billed_lines)
    for app in applications:
        for key, amount in app.allocations.items():
            billed[key] = billed.get(key, ZERO) + amount
            tied += amount
    unapproved = [a for a in areas if a.change_order and not a.approved and billed[a.key] > ZERO]
    unapproved.sort(key=lambda a: (a.role != "original", a.estimate_number, a.order_no))
    return JobWorkAreas(
        lines=tuple(lines),
        billed=billed,
        not_assigned=None if billed_to_date is None else billed_to_date - tied,
        unapproved_billed=sum((billed[a.key] for a in unapproved), ZERO),
        unapproved_labels=tuple(a.label for a in unapproved),
        suggested=sum(1 for t in lines if t.offered and not t.parts and t.suggested),
        billed_lines=billed_lines,
        application_of={app.billing_id: app.number for app in applications},
    )
