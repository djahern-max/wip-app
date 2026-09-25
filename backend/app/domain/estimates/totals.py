"""Totals for one estimate version (F06, D-32, D-04). Pure Decimal arithmetic on
rows already quantized to the cent; nothing here rounds.

A work area's cost is the sum of its cost lines. A line counts toward the estimate's
totals only under a kept work area priced above 0.00 (D-32: an omitted or 0.00 work
area needs no lines; a line under one is reported and left out). Cost by category is
the sum over counting work areas; EAC in the WIP basis is that sum over the slots
the tenant's ``wip_basis`` policy names (D-04), and is ``None`` while the policy is
not decided (no default anywhere: F04).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0.00")
UNKNOWN_CODE_NAME = "Unknown cost code"


@dataclass(frozen=True)
class CostLineIn:
    cost_code: str
    slot: str | None  # None: the code is not on the tenant's grid
    hours: Decimal | None
    amount: Decimal


@dataclass(frozen=True)
class WorkAreaIn:
    order_no: int
    name: str
    kept: bool
    price: Decimal
    change_order_suggested: bool
    lines: tuple[CostLineIn, ...] = ()

    @property
    def cost(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), ZERO)

    @property
    def hours(self) -> Decimal:
        return sum((ln.hours for ln in self.lines if ln.hours is not None), ZERO)

    @property
    def counts(self) -> bool:
        """Its lines count toward the estimate's totals."""
        return self.kept and self.price > ZERO


@dataclass(frozen=True)
class CategoryTotal:
    slot: str | None
    name: str
    amount: Decimal
    hours: Decimal
    in_basis: str  # yes | no | not_decided


@dataclass(frozen=True)
class Totals:
    kept_original: Decimal
    kept_change_orders: Decimal
    kept_total: Decimal
    omitted: Decimal
    kept_hours: Decimal
    kept_cost: Decimal
    by_category: tuple[CategoryTotal, ...]
    eac_in_basis: Decimal | None
    basis_decided: bool


def kept_total(work_areas: Sequence[WorkAreaIn]) -> Decimal:
    return sum((w.price for w in work_areas if w.kept), ZERO)


def compute(
    work_areas: Sequence[WorkAreaIn],
    categories: Sequence[tuple[str, str]],
    basis: frozenset[str] | None,
) -> Totals:
    """``categories``: (slot, name) in slot order for the tenant; ``basis``: the
    slots in the WIP basis, or ``None`` while the policy is not decided."""
    kept_original = sum(
        (w.price for w in work_areas if w.kept and not w.change_order_suggested), ZERO
    )
    kept_co = sum((w.price for w in work_areas if w.kept and w.change_order_suggested), ZERO)
    omitted = sum((w.price for w in work_areas if not w.kept), ZERO)
    counting = [w for w in work_areas if w.counts]
    amounts: dict[str | None, Decimal] = {}
    hours: dict[str | None, Decimal] = {}
    for w in counting:
        for ln in w.lines:
            amounts[ln.slot] = amounts.get(ln.slot, ZERO) + ln.amount
            if ln.hours is not None:
                hours[ln.slot] = hours.get(ln.slot, ZERO) + ln.hours
    decided = basis is not None
    rows: list[CategoryTotal] = []
    for slot, name in categories:
        in_basis = "not_decided" if not decided else ("yes" if slot in basis else "no")
        rows.append(
            CategoryTotal(slot, name, amounts.get(slot, ZERO), hours.get(slot, ZERO), in_basis)
        )
    if None in amounts:
        rows.append(
            CategoryTotal(
                None,
                UNKNOWN_CODE_NAME,
                amounts[None],
                hours.get(None, ZERO),
                "not_decided" if not decided else "no",
            )
        )
    eac = None
    if decided:
        eac = sum((a for slot, a in amounts.items() if slot is not None and slot in basis), ZERO)
    return Totals(
        kept_original=kept_original,
        kept_change_orders=kept_co,
        kept_total=kept_original + kept_co,
        omitted=omitted,
        kept_hours=sum((w.hours for w in counting), ZERO),
        kept_cost=sum((w.cost for w in counting), ZERO),
        by_category=tuple(rows),
        eac_in_basis=eac,
        basis_decided=decided,
    )
