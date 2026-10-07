"""F08.1 Part 1 (D-45): the pure tie of a line to a work area, the suggestion by name,
billed to date per work area and the flag's inputs. No database."""

from datetime import UTC, date, datetime
from decimal import Decimal

from app.domain.billing.figures import (
    SALES_ITEM,
    BillingPolicy,
    DocIn,
    LineIn,
    document_figures,
)
from app.domain.billing.work_areas import (
    AreaRef,
    AssignmentIn,
    hash_number,
    job_work_areas,
    resolve_hash,
    suggest_work_area,
    tie_line,
)
from app.domain.jobs.issues import unapproved_co_billing_issue

D = Decimal
FUEL = frozenset({"902"})
POLICY = BillingPolicy(deposit_items=frozenset({"901"}), surcharge_items=FUEL)
ELM = "e1"
TURLEY = "e2"


def area(
    n: int,
    name: str,
    price: str,
    *,
    kind: str | None = "original",
    kept: bool = True,
    approved: bool = False,
    estimate: str = ELM,
    role: str = "original",
) -> AreaRef:
    number = "EST6115758" if estimate == ELM else "EST6120638"
    return AreaRef(
        estimate, number, role, f"wa-{estimate}-{n}", n, name, kept, D(price), kind, approved
    )


AREAS = (
    area(1, "Mobilization", "5500.00"),
    area(2, "Erosion Control & Site Prep", "10830.68"),
    area(3, "Demo Existing Wall", "17142.92"),
    area(4, "New Retaining Wall", "132820.88"),
    area(17, "Budget for repairs", "15000.00", kind=None, kept=False),
    area(18, "CO: Ledge Removal per Day (07/07/26)", "5475.00", kind="change_order"),
    area(22, "CO: Ledge Removal per Day (07/17/26", "5475.00", kind="change_order"),
)


def line(amount: str, description: str, *, item: str = "903", kind: str = SALES_ITEM, n: int = 1):
    return LineIn(kind, item, D(amount), description, id=f"l{n}", line_no=n)


def doc(lines, *, kind: str = "invoice", number: str = "3107", voided: bool = False):
    total = sum((ln.amount for ln in lines), D("0.00"))
    d = DocIn(
        id=f"d-{number}",
        kind=kind,
        external_id=number,
        doc_number=number,
        txn_date=date(2026, 8, 21),
        total=total,
        tax_total=D("0.00"),
        balance=total,
        voided=voided,
        deleted=False,
        lines=tuple(lines),
    )
    return document_figures(d, POLICY, frozenset({"EST6115758"}))


def assignment(line_id: str, n: int, name: str, *, estimate: str = ELM) -> AssignmentIn:
    return AssignmentIn(
        f"a-{line_id}", line_id, estimate, n, name, "u1", datetime(2026, 10, 7, tzinfo=UTC)
    )


def _tie(d, ln, assigned=None):
    return tie_line(
        d,
        ln,
        AREAS,
        assigned,
        surcharge_items=FUEL,
        estimate_numbers={ELM: "EST6115758", TURLEY: "EST6120638"},
    )


def test_hash_number_reads_only_the_start() -> None:
    assert hash_number("#3 Demo Existing Wall") == 3
    assert hash_number("  #12") == 12
    assert hash_number("Demo #3") is None
    assert hash_number("#3Demo") is None
    assert hash_number(None) is None


def test_a_hash_line_ties_by_number_and_position_decides_nothing() -> None:
    d = doc([line("17142.92", "#3 Mobilization", n=1), line("5500.00", "Something else", n=2)])
    first, second = (_tie(d, ln) for ln in d.doc.lines)
    assert first.area.order_no == 3 and first.how == "number" and first.suggested is None
    assert second.area is None and second.how == "none" and second.suggested is None


def test_hash_is_tied_only_when_the_original_alone_carries_the_number() -> None:
    both = (
        *AREAS,
        area(1, "MOBILIZATION", "6138.67", kind=None, estimate=TURLEY, role="change_order"),
    )
    tied, note = resolve_hash(1, both)
    assert tied is None and "more than one estimate" in note and "EST6120638" in note
    tied, note = resolve_hash(99, AREAS)
    assert tied is None and "not a work area of the original" in note
    only, _ = resolve_hash(2, both)
    assert only.order_no == 2 and only.role == "original"


def test_the_suggestion_is_one_name_match_under_same_name() -> None:
    assert suggest_work_area("  demo existing   wall ", AREAS).order_no == 3
    assert suggest_work_area("Ledge Removal", AREAS) is None
    assert suggest_work_area("", AREAS) is None
    twins = (*AREAS, area(30, "mobilization", "1.00"))
    assert suggest_work_area("Mobilization", twins) is None  # two matches: nothing
    assert suggest_work_area("Budget for repairs", AREAS) is None  # omitted: never suggested


def test_not_offered_lines_are_listed_and_never_tied_or_suggested() -> None:
    fuel = doc([line("100.00", "Mobilization", item="902", n=1)])
    t = _tie(fuel, fuel.doc.lines[0], assignment("l1", 1, "Mobilization"))
    assert (t.offered, t.area, t.suggested) == (False, None, None)
    assert "Fuel surcharge" in t.not_offered
    discount = doc([line("5.00", "Mobilization", kind="DiscountLineDetail", n=1)])
    assert _tie(discount, discount.doc.lines[0]).not_offered.startswith("Not a priced line")
    dep = doc([line("149800.00", "Mobilization", item="901", n=1)], number="EST6115758_DEP")
    assert dep.is_deposit and "deposit" in _tie(dep, dep.doc.lines[0]).not_offered
    voided = doc([line("5500.00", "Mobilization", n=1)], voided=True)
    t = _tie(voided, voided.doc.lines[0])
    assert t.not_offered == "Voided document: in no figure." and t.amount == D("0.00")
    undecided = tie_line(
        voided, voided.doc.lines[0], AREAS, None, surcharge_items=None, estimate_numbers={}
    )
    assert "Fuel surcharge treatment" in undecided.not_offered


def test_an_assignment_follows_the_number_and_a_rename_shows_the_renumbered_sentence() -> None:
    d = doc([line("5475.00", "Ledge Removal", n=1)])
    ln = d.doc.lines[0]
    t = _tie(d, ln, assignment("l1", 18, "CO: Ledge Removal per Day (07/07/26)"))
    assert t.area.order_no == 18 and t.how == "assigned" and t.note is None
    renamed = _tie(d, ln, assignment("l1", 18, "CO: Ledge Removal day one"))
    assert renamed.area.order_no == 18 and renamed.how == "assigned"
    assert renamed.note == (
        'Work area #18 was "CO: Ledge Removal day one" in the baseline and is now '
        '"CO: Ledge Removal per Day (07/07/26)". Work areas keep their numbers; check the '
        "order column and upload again."
    )
    dropped = _tie(d, ln, assignment("l1", 29, "CO: Ledge Removal per Day (09/23/26)"))
    assert dropped.area is None and dropped.how == "none"
    assert dropped.note == (
        'Assigned to work area #29 "CO: Ledge Removal per Day (09/23/26)", which is not on '
        "the latest version of estimate EST6115758; assign the line again or clear it."
    )
    detached = _tie(d, ln, assignment("l1", 1, "MOBILIZATION", estimate="e9"))
    assert "any estimate on this job" in detached.note
    # A "#n" line is tied by its number even when it also carries an assignment.
    hashed = doc([line("5475.00", "#22 Ledge Removal", n=1)])
    t = _tie(
        hashed, hashed.doc.lines[0], assignment("l1", 18, "CO: Ledge Removal per Day (07/07/26)")
    )
    assert t.area.order_no == 22 and t.how == "number"


def test_billed_per_work_area_the_complement_and_the_flag() -> None:
    pmt2 = doc(
        [
            line("5500.00", "Mobilization", n=1),
            line("10830.68", "#2 Erosion", n=2),
            line("17142.92", "Demo Existing Wall", n=3),
            line("132820.88", "New Retaining Wall", n=4),
        ],
        number="EST6115758_PMT2",
    )
    ledge = doc([line("5475.00", "Ledge Removal", n=5), line("5475.00", "Ledge Removal", n=6)])
    memo = doc([line("100.00", "#1 Mobilization", n=7)], kind="credit_memo", number="CM1")
    billed_to_date = D("166294.48") + D("10950.00") - D("100.00")
    before = job_work_areas([pmt2, ledge, memo], billed_to_date, AREAS, {}, surcharge_items=FUEL)
    assert before.billed[(ELM, 2)] == D("10830.68")  # the "#2" line, by number
    assert before.billed[(ELM, 1)] == D("-100.00")  # the credit memo's "#1" line, negative
    assert before.not_assigned == billed_to_date - D("10830.68") + D("100.00")
    assert before.unapproved_billed == D("0.00") and before.unapproved_labels == ()
    assert before.suggested == 3  # Mobilization, Demo, New Retaining Wall; not the ledge lines
    assigned = {
        "l1": assignment("l1", 1, "Mobilization"),
        "l3": assignment("l3", 3, "Demo Existing Wall"),
        "l4": assignment("l4", 4, "New Retaining Wall"),
        "l5": assignment("l5", 18, "CO: Ledge Removal per Day (07/07/26)"),
        "l6": assignment("l6", 22, "CO: Ledge Removal per Day (07/17/26"),
    }
    after = job_work_areas(
        [pmt2, ledge, memo], billed_to_date, AREAS, assigned, surcharge_items=FUEL
    )
    assert [after.billed[(ELM, n)] for n in (1, 2, 3, 4)] == [
        D("5400.00"),
        D("10830.68"),
        D("17142.92"),
        D("132820.88"),
    ]
    assert after.not_assigned == D("0.00")
    assert after.unapproved_billed == D("10950.00") and after.unapproved_labels == ("#18", "#22")
    assert after.left_to_bill(AREAS[0]) == D("100.00")
    assert after.left_to_bill(AREAS[5]) is None  # an unapproved change order
    assert after.left_to_bill(AREAS[4]) is None  # omitted
    approved = tuple(
        AreaRef(**{**a.__dict__, "approved": True}) if a.order_no == 18 else a for a in AREAS
    )
    later = job_work_areas(
        [pmt2, ledge, memo], billed_to_date, approved, assigned, surcharge_items=FUEL
    )
    assert later.unapproved_billed == D("5475.00") and later.unapproved_labels == ("#22",)
    assert later.left_to_bill(approved[5]) == D("0.00")
    undecided = job_work_areas([pmt2], None, AREAS, {}, surcharge_items=None)
    assert undecided.not_assigned is None and undecided.suggested == 0
    text = unapproved_co_billing_issue("67 Elm Street | Parking Lot", after).message
    assert text == (
        '10,950.00 has been billed on job "67 Elm Street | Parking Lot" on 2 change orders '
        "that are not approved: #18, #22 (D-45)."
    )
    assert unapproved_co_billing_issue("J", later).message.endswith(
        "on 1 change order that is not approved: #22 (D-45)."
    )
    assert unapproved_co_billing_issue("J", before) is None
    for word in ("paid", "payment", "collected"):
        assert word not in text.lower()
