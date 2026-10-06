"""A job's contract figures (D-01, D-24, D-30, D-42, D-44; owner's answers 5 and 7).
Pure: the attached estimates in, the figures out; read on demand and never stored.

- **Original contract**: Σ price of the kept work areas a person has confirmed as
  ``original`` on the job's ``original`` estimate (answer 5: nothing counts before it
  is confirmed). An original estimate with no work areas loaded contributes its
  header price (answer 7).
- **Approved change orders** (F07.4, D-42): Σ price of the kept change-order work
  areas with an approval that applies: confirmed ``change_order`` on the original, or
  any kept work area of an estimate attached as ``change_order`` (its role is the
  confirmation; the owner's answer B).
- **Revised contract** = original contract + approved change orders.
- **Unapproved change orders**: the kept change-order work areas without an applying
  approval (an attached change-order estimate with no work areas loaded counts its
  header price). Shown, priced, outside the contract; ``unapproved_count`` counts them.
- **EAC in the WIP basis**: Σ of each attached estimate's own figure (F06.1, burdened),
  ``ignored`` excluded; not computed when any of them is not computed. Approval moves
  nothing here (D-44): the cost of an unapproved change order is expected whether or
  not its price is ever agreed.
- A time-and-materials job has no revised contract and no change orders (D-24). A
  pool has neither, and no EAC (D-30); nor has a maintenance or snow program
  (``recurring_service``), which is recognised as billed (D-35).

**Whether an approval applies** (``approval_state``; rule C, the owner, 2026-10-06): an
approval is for the work area at its price on that day, keyed by its order number on its
estimate. It applies while *every* version with work areas received after the one it
was made on carries a row at that order number that is kept, has the same name (the kind
carry's comparison) and the same price, and the latest row is confirmed as a change
order (or is a kept row of an estimate attached as a change order). Any break ends the
approval for good: a later version that restores the name and price does not revive it.
The first breaking version names why, in the words the sentence prints.

Money is ``Decimal`` throughout; nothing here rounds (every input is already cents).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.billing.figures import words
from app.domain.estimates.versions import same_name

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class AreaIn:
    order_no: int
    kept: bool
    price: Decimal
    kind: str | None  # confirmed kind, or None
    approved: bool = False  # F07.4: an approval applies (decided by approval_state)


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
    original_contract: Decimal | None = None  # F07.4: confirmed originals (or the header price)
    approved_change_orders: Decimal | None = None  # F07.4 (D-42)
    unapproved_count: int = 0  # F07.4: kept change-order work areas without an approval


def _kept(areas: Sequence[AreaIn]) -> list[AreaIn]:
    return [a for a in areas if a.kept]


def job_contract(revenue_method: str, attached: Sequence[AttachedIn]) -> Contract:
    counted = [a for a in attached if a.role != "ignored"]
    original = next((a for a in counted if a.role == "original"), None)
    if revenue_method in ("pool", "recurring_service"):  # D-30, D-35
        return Contract(None, False, None, 0, False, None, False)

    base: Decimal | None = None
    from_header = False
    approved = ZERO
    unapproved = ZERO
    unapproved_count = 0
    to_confirm = 0
    if original is not None:
        if original.work_areas is None:
            base, from_header = original.price, True
        else:
            kept = _kept(original.work_areas)
            base = sum((a.price for a in kept if a.kind == "original"), ZERO)
            for a in kept:
                if a.kind != "change_order":
                    continue
                if a.approved:
                    approved += a.price
                else:
                    unapproved += a.price
                    unapproved_count += 1
            to_confirm = sum(1 for a in kept if a.kind is None)
    for co in (a for a in counted if a.role == "change_order"):
        if co.work_areas is None:
            unapproved += co.price
            unapproved_count += 1
        else:
            for a in _kept(co.work_areas):
                if a.approved:
                    approved += a.price
                else:
                    unapproved += a.price
                    unapproved_count += 1

    not_computed = any(a.eac is None for a in counted)
    eac = None if not counted or not_computed else sum((a.eac for a in counted), ZERO)

    if revenue_method == "time_and_materials":
        return Contract(None, False, None, 0, original is not None, eac, not_computed)
    revised = None if base is None else base + approved
    return Contract(
        revised,
        from_header,
        unapproved,
        to_confirm,
        original is not None,
        eac,
        not_computed,
        original_contract=base,
        approved_change_orders=None if base is None else approved,
        unapproved_count=unapproved_count,
    )


# --- F07.4: does an approval still apply? (D-42, rule C) ---------------------------------------


@dataclass(frozen=True)
class RowIn:
    """One work area of one version, as the chain reads it."""

    order_no: int
    name: str
    kept: bool
    price: Decimal
    kind: str | None


@dataclass(frozen=True)
class VersionIn:
    version_no: int
    rows: tuple[RowIn, ...]


@dataclass(frozen=True)
class ApprovalIn:
    order_no: int
    name: str  # the name on the version approved
    price: Decimal  # the price that day
    version_no: int  # the version the approval was made on


@dataclass(frozen=True)
class ApprovalState:
    applies: bool
    ended: str | None  # why not, in the sentence's words; None while it applies


def approval_state(approval: ApprovalIn, later: Sequence[VersionIn], role: str) -> ApprovalState:
    """``later``: the versions with work areas received after ``approval.version_no``, in
    order, the latest last. ``role``: the estimate's role on the job (``original`` needs
    the latest row confirmed as a change order; ``change_order`` does not)."""
    for v in later:
        row = next((r for r in v.rows if r.order_no == approval.order_no), None)
        if row is None:
            return ApprovalState(False, f"version {v.version_no} does not carry it")
        if not row.kept:
            return ApprovalState(False, f"version {v.version_no} omits it")
        if not same_name(row.name, approval.name):
            return ApprovalState(False, f'version {v.version_no} renamed it "{row.name}"')
        if row.price != approval.price:
            return ApprovalState(False, f"version {v.version_no} priced it {words(row.price)}")
    if role == "original" and later and later[-1].rows:
        latest = next((r for r in later[-1].rows if r.order_no == approval.order_no), None)
        if latest is not None and latest.kind != "change_order":
            return ApprovalState(False, "it is no longer confirmed as a change order")
    return ApprovalState(True, None)
