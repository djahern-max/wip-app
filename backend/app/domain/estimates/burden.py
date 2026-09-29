"""Labor burden on an estimate (F06.1; D-05, D-34). Pure: no database, no I/O, no
clock, Decimal only. Computed when an estimate is read and never written anywhere:
the cost lines stay exactly as loaded, so their total always ties to the estimating
system (D-34).

Burden applies to Labor (slot 10) lines under counting work areas (kept and priced
above 0.00, the rule every F06 total uses). For each work area and division:
``(sum of the slot-10 amounts) x the rate in force for that division on the pricing
day``, quantized ROUND_HALF_UP to the cent once (D-05; owner's answer 3). Every
figure above that is a sum of those amounts; nothing else rounds.

A division with labor and no rate in force on the day, or no pricing day at all,
leaves burden not computed (``None``) for its work areas and for the estimate. Slot-20
lines are never burden: estimators do not key it (D-05); the work areas carrying one
are reported and the lines are left out of the with-burden figures (``totals``).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from app.domain.config.burden import RateLike, pick_rate
from app.domain.estimates.totals import ZERO, WorkAreaIn

LABOR_SLOT = "10"
BURDEN_SLOT = "20"
CENT = Decimal("0.01")


@dataclass(frozen=True)
class DivisionBurden:
    division_id: UUID
    division_code: str
    labor: Decimal
    rate: RateLike | None  # None: no rate in force on the day (or no day)
    burden: Decimal | None  # None: not computed


@dataclass(frozen=True)
class WorkAreaBurden:
    order_no: int
    divisions: tuple[DivisionBurden, ...]

    @property
    def burden(self) -> Decimal | None:
        if any(d.burden is None for d in self.divisions):
            return None
        return sum((d.burden for d in self.divisions if d.burden is not None), ZERO)


@dataclass(frozen=True)
class BurdenResult:
    day: date | None
    work_areas: tuple[WorkAreaBurden, ...]  # counting work areas that carry labor
    by_division: tuple[DivisionBurden, ...]  # the estimate's labor and burden per division
    burden_line_orders: tuple[int, ...]  # counting work areas carrying slot-20 lines

    @property
    def missing(self) -> tuple[str, ...]:
        """Division codes with labor and no burden computed, in division-code order."""
        return tuple(d.division_code for d in self.by_division if d.burden is None)

    @property
    def total(self) -> Decimal | None:
        if self.missing:
            return None
        return sum((d.burden for d in self.by_division if d.burden is not None), ZERO)

    def burden_for(self, order_no: int) -> Decimal | None:
        """A work area's burden: 0.00 when it carries none (no labor, or it does not
        count); ``None`` when a rate it needs is missing."""
        for w in self.work_areas:
            if w.order_no == order_no:
                return w.burden
        return ZERO


def _quantize(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def compute_burden(
    work_areas: Sequence[WorkAreaIn], rates: Iterable[RateLike], day: date | None
) -> BurdenResult:
    """``rates``: the tenant's active burden-rate rows; ``day``: the date to price at
    (the estimate date, else the version's received date), or ``None``."""
    rate_rows = list(rates)
    per_area: list[WorkAreaBurden] = []
    burden_lines: list[int] = []
    codes: dict[UUID, str] = {}
    for w in work_areas:
        if not w.counts:
            continue
        labor: dict[UUID, Decimal] = {}
        for ln in w.lines:
            if ln.slot == BURDEN_SLOT:
                if w.order_no not in burden_lines:
                    burden_lines.append(w.order_no)
            elif ln.slot == LABOR_SLOT and ln.division_id is not None:
                labor[ln.division_id] = labor.get(ln.division_id, ZERO) + ln.amount
                codes[ln.division_id] = ln.division_code or "?"
        if not labor:
            continue
        rows = []
        for division_id in sorted(labor, key=lambda d: codes[d]):
            rate = None if day is None else pick_rate(rate_rows, day, division_id)
            amount = labor[division_id]
            rows.append(
                DivisionBurden(
                    division_id,
                    codes[division_id],
                    amount,
                    rate,
                    None if rate is None else _quantize(amount * rate.rate),
                )
            )
        per_area.append(WorkAreaBurden(w.order_no, tuple(rows)))
    by_division: list[DivisionBurden] = []
    for division_id in sorted(codes, key=lambda d: codes[d]):
        parts = [d for w in per_area for d in w.divisions if d.division_id == division_id]
        computed = all(d.burden is not None for d in parts)
        by_division.append(
            DivisionBurden(
                division_id,
                codes[division_id],
                sum((d.labor for d in parts), ZERO),
                parts[0].rate,  # one day, so one row per division
                sum((d.burden for d in parts if d.burden is not None), ZERO) if computed else None,
            )
        )
    return BurdenResult(day, tuple(per_area), tuple(by_division), tuple(burden_lines))
