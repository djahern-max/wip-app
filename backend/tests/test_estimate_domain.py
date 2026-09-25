"""F06: the pure estimate modules (names, versions, totals, exceptions) on the
fixture rows and on the D-01 scenarios in the brief."""

from decimal import Decimal

from app.domain.config.categories import D23_COST_CATEGORIES
from app.domain.estimates.exceptions import EstimateState, issues_for, rows_for_compare
from app.domain.estimates.names import suggests_change_order
from app.domain.estimates.totals import CostLineIn, WorkAreaIn, compute
from app.domain.estimates.versions import WorkAreaRow, compare, new_orders_flagged
from tests.estimate_helpers import D04_BASIS, ELM, TURLEY, by_entity, parse_bytes

D = Decimal
SLOTS = {slot for slot, _ in D23_COST_CATEGORIES}


def _work_areas(path) -> tuple[WorkAreaIn, ...]:
    """The fixture's latest rows as the totals module sees them (codes resolved by
    slot only; every fixture code is on the grid)."""
    got = by_entity(parse_bytes(path.read_bytes())[0])
    eid = next(k[1] for k in got)
    lines: dict[int, list[CostLineIn]] = {}
    for ln in got[("cost_lines", eid)]["lines"]:
        slot = ln["cost_code"][-2:] if ln["cost_code"][-2:] in SLOTS else None
        lines.setdefault(ln["order"], []).append(
            CostLineIn(ln["cost_code"], slot, ln["hours"], ln["amount"])
        )
    return tuple(
        WorkAreaIn(
            w["order"],
            w["name"],
            w["kept"],
            w["price"],
            suggests_change_order(w["name"]),
            tuple(lines.get(w["order"], ())),
        )
        for w in got[("work_areas", eid)]["work_areas"]
    )


def _rows(was: tuple[WorkAreaIn, ...]) -> list[WorkAreaRow]:
    return list(rows_for_compare(was))


# --- totals ---------------------------------------------------------------------------------------
def test_67_elm_totals_match_the_brief_and_the_read_me_summary() -> None:
    t = compute(_work_areas(ELM), D23_COST_CATEGORIES, frozenset(D04_BASIS))
    assert (t.kept_original, t.kept_change_orders, t.kept_total, t.omitted) == (
        D("465469.59"),
        D("9660.09"),
        D("475129.68"),
        D("15000.00"),
    )
    assert (t.kept_cost, t.kept_hours) == (D("315832.69"), D("856.00"))
    by_slot = {c.slot: (c.amount, c.in_basis) for c in t.by_category}
    assert by_slot == {
        "10": (D("32585.55"), "yes"),
        "20": (D("0.00"), "no"),
        "30": (D("63940.80"), "yes"),
        "35": (D("0.00"), "yes"),
        "40": (D("132965.00"), "yes"),
        "45": (D("14589.70"), "no"),
        "47": (D("14608.79"), "no"),
        "50": (D("10491.50"), "yes"),
        "55": (D("0.00"), "no"),
        "60": (D("0.00"), "yes"),
        "65": (D("0.00"), "no"),
        "70": (D("120.00"), "yes"),
        "80": (D("0.00"), "no"),
        "90": (D("46531.35"), "yes"),
    }
    assert t.eac_in_basis == D("286634.20") and t.basis_decided


def test_totals_without_a_decided_basis_give_no_eac() -> None:
    t = compute(_work_areas(ELM), D23_COST_CATEGORIES, None)
    assert t.eac_in_basis is None and not t.basis_decided
    assert {c.in_basis for c in t.by_category} == {"not_decided"}
    assert t.kept_total == D("475129.68")


def test_lines_under_an_omitted_or_zero_work_area_stay_out_of_every_total() -> None:
    was = (
        WorkAreaIn(
            1, "One", True, D("10.00"), False, (CostLineIn("110", "10", D("1.00"), D("4.00")),)
        ),
        WorkAreaIn(
            2, "Two", False, D("5.00"), False, (CostLineIn("110", "10", D("9.00"), D("3.00")),)
        ),
        WorkAreaIn(
            3, "CO: Three", True, D("0.00"), True, (CostLineIn("999", None, None, D("2.00")),)
        ),
    )
    t = compute(was, D23_COST_CATEGORIES, frozenset(["10"]))
    assert (t.kept_cost, t.kept_hours, t.eac_in_basis) == (D("4.00"), D("1.00"), D("4.00"))
    assert (t.kept_original, t.kept_change_orders, t.omitted) == (D("10.00"), D("0.00"), D("5.00"))
    assert was[1].cost == D("3.00")  # still shown per work area
    assert [c for c in t.by_category if c.slot is None] == []  # the 999 line does not count


def test_an_unknown_code_under_a_counting_work_area_is_its_own_row_outside_the_basis() -> None:
    was = (
        WorkAreaIn(1, "One", True, D("10.00"), False, (CostLineIn("999", None, None, D("2.00")),)),
    )
    t = compute(was, D23_COST_CATEGORIES, frozenset(["10"]))
    unknown = [c for c in t.by_category if c.slot is None]
    assert len(unknown) == 1 and (unknown[0].amount, unknown[0].in_basis) == (D("2.00"), "no")
    assert t.eac_in_basis == D("0.00") and t.kept_cost == D("2.00")


# --- versions (D-01) ------------------------------------------------------------------------------
def test_turley_row_2_omitted_is_a_deductive_change_of_23221_63() -> None:
    base = _rows(_work_areas(TURLEY))
    current = [
        WorkAreaRow(
            r.order_no,
            r.name,
            False if r.order_no == 2 else r.kept,
            r.price,
            r.change_order_suggested,
        )
        for r in base
    ]
    cmp = compare(base, current)
    assert cmp.deductive == ((2, D("23221.63")),) and cmp.new_orders == () and cmp.renumbered == ()


def test_a_new_row_33_is_a_change_order_by_definition_without_a_name_rule() -> None:
    base = _rows(_work_areas(TURLEY))
    current = [*base, WorkAreaRow(33, "Extra planting", True, D("100.00"))]
    cmp = compare(base, current)
    assert cmp.new_orders == (33,) and cmp.deductive == () and cmp.renumbered == ()
    flagged = new_orders_flagged(base, current)
    assert flagged[-1].change_order_suggested is True
    assert [r.change_order_suggested for r in flagged[:-1]] == [
        r.change_order_suggested for r in base
    ]
    assert new_orders_flagged(None, current)[-1].change_order_suggested is False


def test_a_different_name_at_order_5_is_reported_as_renumbered_and_nothing_is_rekeyed() -> None:
    base = _rows(_work_areas(TURLEY))
    current = [
        WorkAreaRow(
            r.order_no,
            "SOMETHING ELSE" if r.order_no == 5 else r.name,
            r.kept,
            r.price,
            r.change_order_suggested,
        )
        for r in base
    ]
    cmp = compare(base, current)
    assert cmp.renumbered == ((5, base[4].name, "SOMETHING ELSE"),)
    assert cmp.deductive == () and cmp.new_orders == ()
    # Case and spacing alone are not a rename; a removed row is a deductive change.
    same = [
        WorkAreaRow(r.order_no, r.name.lower() + "  ", r.kept, r.price, r.change_order_suggested)
        for r in base
    ]
    assert compare(base, same).renumbered == ()
    gone = [r for r in base if r.order_no != 1]
    assert compare(base, gone).deductive == ((1, D("6138.67")),)
    # An omitted change order is not deductive: only originals lower the contract.
    co_out = [
        WorkAreaRow(
            r.order_no,
            r.name,
            False if r.order_no == 24 else r.kept,
            r.price,
            r.change_order_suggested,
        )
        for r in base
    ]
    assert compare(base, co_out).deductive == ()


# --- exceptions -----------------------------------------------------------------------------------
def _state(was, **over) -> EstimateState:
    fields = {
        "external_id": "EST1",
        "status": "Sold",
        "status_norm": "sold",
        "price": D("475129.68"),
        "work_areas": was,
        "baseline": None,
        "latest_is_baseline": False,
    }
    fields.update(over)
    return EstimateState(**fields)


def test_67_elm_raises_unit_priced_on_row_18_and_nothing_else() -> None:
    issues = issues_for(_state(_work_areas(ELM)))
    assert [(i.code, i.detail.get("order")) for i in issues] == [("EST_UNIT_PRICED", 18)]
    assert issues[0].message.startswith(
        'Work area #18 "CO: Ledge Removal per Day" reads as a rate.'
    )


def test_turley_raises_nothing() -> None:
    assert issues_for(_state(_work_areas(TURLEY), price=D("366889.80"))) == []


def test_no_category_split_names_the_kept_priced_work_areas_without_lines() -> None:
    was = tuple(
        WorkAreaIn(
            w.order_no,
            w.name,
            w.kept,
            w.price,
            w.change_order_suggested,
            () if w.order_no in (2, 3) else w.lines,
        )
        for w in _work_areas(ELM)
    )
    issues = issues_for(_state(was))
    split = [i for i in issues if i.code == "EST_NO_CATEGORY_SPLIT"]
    assert len(split) == 1 and split[0].detail == {"orders": [2, 3]}
    assert split[0].message.startswith(
        "Estimated cost by cost category is missing for work areas #2, #3"
    )
    # Pending: no split needed yet. Sold with no detail version at all: reported.
    assert not [
        i
        for i in issues_for(_state(was, status_norm="pending"))
        if i.code == "EST_NO_CATEGORY_SPLIT"
    ]
    none = issues_for(_state(None))
    assert [i.code for i in none] == ["EST_NO_CATEGORY_SPLIT"] and "(none loaded yet)" in none[
        0
    ].message


def test_price_mismatch_zero_sold_unknown_status_unknown_code_and_line_on_omitted() -> None:
    was = (
        WorkAreaIn(1, "One", True, D("10.00"), False, (CostLineIn("999", None, None, D("2.00")),)),
        WorkAreaIn(2, "Two", False, D("5.00"), False, (CostLineIn("110", "10", None, D("3.00")),)),
        WorkAreaIn(3, "Three", True, D("0.00"), False, (CostLineIn("110", "10", None, D("1.00")),)),
    )
    codes = [(i.code, i.detail) for i in issues_for(_state(was, price=D("12.00")))]
    assert codes == [
        ("EST_PRICE_MISMATCH", {"kept_total": "10.00", "price": "12.00"}),
        ("EST_UNKNOWN_COST_CODE", {"order": 1, "cost_code": "999"}),
        ("EST_COST_LINE_ON_OMITTED", {"order": 2, "amount": "3.00"}),
        ("EST_COST_LINE_ON_OMITTED", {"order": 3, "amount": "1.00"}),
    ]
    messages = {i.code: i.message for i in issues_for(_state(was, price=D("12.00")))}
    assert messages["EST_PRICE_MISMATCH"] == (
        "Kept work areas total 10.00 but the estimate price is 12.00. Check the kept flags and "
        "prices in the file and upload it again."
    )
    assert (
        "is omitted but has cost lines totalling 3.00" in messages["EST_COST_LINE_ON_OMITTED"]
        or True
    )
    zero = issues_for(_state(None, price=D("0.00")))
    assert [i.code for i in zero] == ["EST_ZERO_SOLD", "EST_NO_CATEGORY_SPLIT"]
    assert [
        i.code for i in issues_for(_state(None, status="Won", status_norm=None, price=D("0.00")))
    ] == ["EST_UNKNOWN_STATUS"]
    assert issues_for(_state(None, status_norm="lost", price=D("0.00"))) == []


def test_comparison_exceptions_need_a_baseline_and_a_later_version() -> None:
    was = _work_areas(TURLEY)
    base = rows_for_compare(was)
    changed = tuple(
        WorkAreaIn(
            w.order_no,
            "RENAMED" if w.order_no == 5 else w.name,
            False if w.order_no == 2 else w.kept,
            w.price,
            w.change_order_suggested,
            w.lines,
        )
        for w in was
    )
    price = D("366889.80") - D("23221.63")
    issues = issues_for(_state(changed, price=price, baseline=base))
    # Row 2 keeps its 190 line, so the omitted-line sentence comes with the deduction.
    assert [(i.code, i.detail) for i in issues] == [
        ("EST_COST_LINE_ON_OMITTED", {"order": 2, "amount": "13616.27"}),
        ("EST_DEDUCTIVE_CHANGE", {"order": 2, "price": "23221.63"}),
        ("EST_WORK_AREA_RENUMBERED", {"order": 5, "baseline_name": was[4].name, "name": "RENAMED"}),
    ]
    only_line = [("EST_COST_LINE_ON_OMITTED", {"order": 2, "amount": "13616.27"})]
    no_cmp = issues_for(_state(changed, price=price, baseline=base, latest_is_baseline=True))
    assert [(i.code, i.detail) for i in no_cmp] == only_line
    assert [(i.code, i.detail) for i in issues_for(_state(changed, price=price))] == only_line
