"""Version comparison (D-01; F06 plan question 4). Pure: rows in, findings out.

Work-area identity across versions is ``order_no`` (the "#n" of D-26). Against the
baseline: a kept work area whose order is not in the baseline is a change order by
definition; a baseline original (kept, not a suggested change order) that is now
omitted or gone is a deductive change; a kept work area whose name at an order
number differs from the baseline is reported as renumbered and never re-keyed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class WorkAreaRow:
    order_no: int
    name: str
    kept: bool
    price: Decimal
    change_order_suggested: bool = False


@dataclass(frozen=True)
class Comparison:
    new_orders: tuple[int, ...]  # kept now, absent from the baseline
    deductive: tuple[tuple[int, Decimal], ...]  # (order, baseline price)
    renumbered: tuple[tuple[int, str, str], ...]  # (order, baseline name, current name)


def _norm(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def compare(baseline: Sequence[WorkAreaRow], current: Sequence[WorkAreaRow]) -> Comparison:
    base = {r.order_no: r for r in baseline}
    now = {r.order_no: r for r in current}
    new_orders = tuple(sorted(o for o, r in now.items() if r.kept and o not in base))
    deductive = tuple(
        (o, r.price)
        for o, r in sorted(base.items())
        if r.kept and not r.change_order_suggested and (o not in now or not now[o].kept)
    )
    renumbered = tuple(
        (o, base[o].name, r.name)
        for o, r in sorted(now.items())
        if r.kept and o in base and _norm(base[o].name) != _norm(r.name)
    )
    return Comparison(new_orders=new_orders, deductive=deductive, renumbered=renumbered)


def new_orders_flagged(
    baseline: Sequence[WorkAreaRow] | None, rows: Sequence[WorkAreaRow]
) -> list[WorkAreaRow]:
    """The rows with ``change_order_suggested`` also set for kept work areas not in
    the baseline (D-01: by definition, no name needed). Without a baseline the rows
    come back as they are."""
    if baseline is None:
        return list(rows)
    new = set(compare(baseline, rows).new_orders)
    return [
        WorkAreaRow(
            r.order_no, r.name, r.kept, r.price, r.change_order_suggested or r.order_no in new
        )
        for r in rows
    ]
