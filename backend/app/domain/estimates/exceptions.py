"""Exception generators for one estimate (F06; BLUEPRINT §10; plan question 5).

Pure: the estimate's state in, a list of ``Issue`` (code, one plain sentence,
detail) out, computed when the estimate is read. F09 calls the same function and
persists the result; nothing migrates. File-level facts (``EST_NO_ID``, rows not
loaded) live on the import batch, not here.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.estimates.names import reads_as_unit_price
from app.domain.estimates.totals import ZERO, WorkAreaIn, kept_total
from app.domain.estimates.versions import WorkAreaRow, compare

# The one place each code has its sentence (D-22: the sentence, never the code alone).
SENTENCES: dict[str, str] = {
    "EST_UNKNOWN_STATUS": (
        'Status "{status}" is not Pending, Sold or Lost, so the estimate is loaded '
        "without a status. Correct it in the file and upload the file again."
    ),
    "EST_ZERO_SOLD": (
        "This estimate is Sold at 0.00. Give it its price in the file and upload again, "
        "or mark it Lost."
    ),
    "EST_UNIT_PRICED": (
        'Work area #{order} "{name}" reads as a rate. Move it to a time-and-materials '
        "job or give it a fixed price (D-24)."
    ),
    "EST_NO_CATEGORY_SPLIT": (
        "Estimated cost by cost category is missing for work area{s} {orders}, so the "
        "estimate stays off the WIP schedule until the cost lines are supplied (D-04)."
    ),
    "EST_PRICE_MISMATCH": (
        "Kept work areas total {kept} but the estimate price is {price}. Check the kept "
        "flags and prices in the file and upload it again."
    ),
    "EST_DEDUCTIVE_CHANGE": (
        "Work area #{order} ({price}) was in the baseline and is now omitted: a deductive "
        "change. Confirm it, or restore the work area and upload again (D-01)."
    ),
    "EST_WORK_AREA_RENUMBERED": (
        'Work area #{order} was "{old}" in the baseline and is now "{new}". Work '
        "areas keep their numbers; check the order column and upload again."
    ),
    "EST_UNKNOWN_COST_CODE": (
        "Cost code {code} on work area #{order} is not on this company's cost-code grid. "
        "The line is loaded without a category; correct the code and upload again."
    ),
    "EST_COST_LINE_ON_OMITTED": (
        "Work area #{order} is {why} but has cost lines totalling {amount}. They are "
        "left out of every total; remove them or keep the work area."
    ),
    "EST_NO_ID": (
        'Row {row} on "{sheet}" has no estimate id and was not loaded. Give it the '
        "estimating system's id and upload the file again."
    ),
}


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    detail: dict = field(default_factory=dict)


def sentence(code: str, /, **values) -> str:
    return SENTENCES[code].format(**values)


def _money(d: Decimal) -> str:
    return format(d, "f")


@dataclass(frozen=True)
class EstimateState:
    external_id: str
    status: str
    status_norm: str | None
    price: Decimal
    work_areas: tuple[WorkAreaIn, ...] | None  # None: no version with work areas yet
    baseline: tuple[WorkAreaRow, ...] | None  # None: no baseline yet
    latest_is_baseline: bool = False


def issues_for(state: EstimateState) -> list[Issue]:
    out: list[Issue] = []
    if state.status_norm is None:
        out.append(
            Issue(
                "EST_UNKNOWN_STATUS",
                sentence("EST_UNKNOWN_STATUS", status=state.status),
                {"status": state.status},
            )
        )
    sold = state.status_norm == "sold"
    if sold and state.price == ZERO:
        out.append(Issue("EST_ZERO_SOLD", sentence("EST_ZERO_SOLD"), {"price": "0.00"}))
    was = state.work_areas
    if was is not None:
        for w in was:
            if w.kept and reads_as_unit_price(w.name):
                out.append(
                    Issue(
                        "EST_UNIT_PRICED",
                        sentence("EST_UNIT_PRICED", order=w.order_no, name=w.name),
                        {"order": w.order_no, "name": w.name},
                    )
                )
        kept = kept_total(was)
        if kept != state.price:
            out.append(
                Issue(
                    "EST_PRICE_MISMATCH",
                    sentence("EST_PRICE_MISMATCH", kept=_money(kept), price=_money(state.price)),
                    {"kept_total": _money(kept), "price": _money(state.price)},
                )
            )
    if sold:
        missing = [w.order_no for w in was or () if w.counts and not w.lines]
        if was is None or missing:
            orders = ", ".join(f"#{o}" for o in missing) if missing else "(none loaded yet)"
            out.append(
                Issue(
                    "EST_NO_CATEGORY_SPLIT",
                    sentence(
                        "EST_NO_CATEGORY_SPLIT", s="s" if len(missing) != 1 else "", orders=orders
                    ),
                    {"orders": missing},
                )
            )
    for w in was or ():
        for ln in w.lines:
            if ln.slot is None:
                out.append(
                    Issue(
                        "EST_UNKNOWN_COST_CODE",
                        sentence("EST_UNKNOWN_COST_CODE", code=ln.cost_code, order=w.order_no),
                        {"order": w.order_no, "cost_code": ln.cost_code},
                    )
                )
        if w.lines and not w.counts:
            why = "omitted" if not w.kept else "priced 0.00"
            out.append(
                Issue(
                    "EST_COST_LINE_ON_OMITTED",
                    sentence(
                        "EST_COST_LINE_ON_OMITTED", order=w.order_no, why=why, amount=_money(w.cost)
                    ),
                    {"order": w.order_no, "amount": _money(w.cost)},
                )
            )
    if state.baseline is not None and was is not None and not state.latest_is_baseline:
        current = [
            WorkAreaRow(w.order_no, w.name, w.kept, w.price, w.change_order_suggested) for w in was
        ]
        cmp = compare(state.baseline, current)
        for order, price in cmp.deductive:
            out.append(
                Issue(
                    "EST_DEDUCTIVE_CHANGE",
                    sentence("EST_DEDUCTIVE_CHANGE", order=order, price=_money(price)),
                    {"order": order, "price": _money(price)},
                )
            )
        for order, old, new in cmp.renumbered:
            out.append(
                Issue(
                    "EST_WORK_AREA_RENUMBERED",
                    sentence("EST_WORK_AREA_RENUMBERED", order=order, old=old, new=new),
                    {"order": order, "baseline_name": old, "name": new},
                )
            )
    return out


def rows_for_compare(work_areas: Sequence[WorkAreaIn]) -> tuple[WorkAreaRow, ...]:
    return tuple(
        WorkAreaRow(w.order_no, w.name, w.kept, w.price, w.change_order_suggested)
        for w in work_areas
    )
