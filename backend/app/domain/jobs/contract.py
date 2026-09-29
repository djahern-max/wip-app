"""A job's contract figures (D-01, D-24, D-30; owner's answers 5 and 7). Pure: the
attached estimates in, the figures out; read on demand and never stored.

- **Revised contract**: Σ price of the kept work areas a person has confirmed as
  ``original`` on the job's ``original`` estimate (answer 5: nothing counts before it
  is confirmed). An original estimate with no work areas loaded contributes its
  header price (answer 7). Approved change orders join it with the sign-off feature;
  until then there are none.
- **Unapproved change orders**: kept work areas confirmed as ``change_order`` on the
  original, and every kept work area of an estimate attached as ``change_order`` (its
  header price when it has no work areas loaded). Shown, priced, outside the contract.
- **EAC in the WIP basis**: Σ of each attached estimate's own figure (F06.1, burdened),
  ``ignored`` excluded; not computed when any of them is not computed.
- A time-and-materials job has no revised contract and no change orders (D-24). A
  pool has neither, and no EAC (D-30); nor has a maintenance or snow program
  (``recurring_service``), which is recognised as billed (D-35).

Money is ``Decimal`` throughout; nothing here rounds (every input is already cents).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class AreaIn:
    order_no: int
    kept: bool
    price: Decimal
    kind: str | None  # confirmed kind, or None


@dataclass(frozen=True)
class AttachedIn:
    role: str  # original | change_order | ignored
    price: Decimal  # the estimate's header price
    work_areas: tuple[AreaIn, ...] | None  # None: no work areas loaded
    eac: Decimal | None  # the estimate's EAC in the WIP basis; None: not computed


@dataclass(frozen=True)
class Contract:
    revised_contract: Decimal | None  # None: not shown (no original, T&M, pool)
    from_header_price: bool  # the original has no work areas loaded
    unapproved_change_orders: Decimal | None
    to_confirm: int  # kept work areas on the original with no confirmed kind
    has_original: bool
    eac_in_basis: Decimal | None
    eac_not_computed: bool  # an attached estimate's EAC is not computed


def _kept(areas: Sequence[AreaIn]) -> list[AreaIn]:
    return [a for a in areas if a.kept]


def job_contract(revenue_method: str, attached: Sequence[AttachedIn]) -> Contract:
    counted = [a for a in attached if a.role != "ignored"]
    original = next((a for a in counted if a.role == "original"), None)
    if revenue_method in ("pool", "recurring_service"):  # D-30, D-35
        return Contract(None, False, None, 0, False, None, False)

    revised: Decimal | None = None
    from_header = False
    change_orders = ZERO
    to_confirm = 0
    if original is not None:
        if original.work_areas is None:
            revised, from_header = original.price, True
        else:
            kept = _kept(original.work_areas)
            revised = sum((a.price for a in kept if a.kind == "original"), ZERO)
            change_orders += sum((a.price for a in kept if a.kind == "change_order"), ZERO)
            to_confirm = sum(1 for a in kept if a.kind is None)
    for co in (a for a in counted if a.role == "change_order"):
        if co.work_areas is None:
            change_orders += co.price
        else:
            change_orders += sum((a.price for a in _kept(co.work_areas)), ZERO)

    not_computed = any(a.eac is None for a in counted)
    eac = None if not counted or not_computed else sum((a.eac for a in counted), ZERO)

    if revenue_method == "time_and_materials":
        return Contract(None, False, None, 0, original is not None, eac, not_computed)
    return Contract(
        revised, from_header, change_orders, to_confirm, original is not None, eac, not_computed
    )
