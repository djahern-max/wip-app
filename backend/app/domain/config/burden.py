"""Burden rates (F04): effective-dated fractions (``NUMERIC(7,4)``, Decimal end to
end), optionally per division; no overlapping periods for the same division; one
rule answers "the rate in force on this date" (``pick_rate``, pure, also used by the
estimate burden of F06.1 over rows loaded once)."""

from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.domain.config.audit import Actor, audit
from app.domain.config.models import BurdenRate


class BurdenRateError(ValueError):
    pass


def _overlaps(a_from: date, a_to: date | None, b_from: date, b_to: date | None) -> bool:
    """Half-open periods ``[from, to)``; ``None`` = open-ended."""
    return (b_to is None or a_from < b_to) and (a_to is None or b_from < a_to)


def add_burden_rate(
    db: Session,
    tenant_id: UUID,
    *,
    division_id: UUID | None,
    effective_from: date,
    effective_to: date | None,
    rate: Decimal,
    basis_note: str | None,
    actor: Actor,
) -> BurdenRate:
    if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0 or rate >= 10:
        raise BurdenRateError("a rate as a decimal fraction, such as 0.3250")
    if effective_to is not None and effective_to <= effective_from:
        raise BurdenRateError("the end date must be after the start date")
    existing = db.execute(
        select(BurdenRate)
        .where(BurdenRate.active, BurdenRate.division_id.is_(division_id))
        .with_for_update()
        if division_id is None
        else select(BurdenRate)
        .where(BurdenRate.active, BurdenRate.division_id == division_id)
        .with_for_update()
    ).scalars()
    for other in existing:
        if _overlaps(effective_from, effective_to, other.effective_from, other.effective_to):
            raise BurdenRateError(
                f"this period overlaps the rate in force from {other.effective_from.isoformat()}"
            )
    row = BurdenRate(
        tenant_id=tenant_id,
        division_id=division_id,
        effective_from=effective_from,
        effective_to=effective_to,
        rate=rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        basis_note=(basis_note or "").strip()[:500] or None,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.burden_rate_added,
        "burden_rate",
        row.id,
        actor,
        after=_plain(row),
    )
    return row


def deactivate_burden_rate(db: Session, tenant_id: UUID, rate_id: UUID, actor: Actor) -> BurdenRate:
    row = db.get(BurdenRate, rate_id)
    if row is None or not row.active:
        raise LookupError("burden rate not found")
    before = _plain(row)
    row.active = False
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.burden_rate_deactivated,
        "burden_rate",
        row.id,
        actor,
        before=before,
        after=_plain(row),
    )
    return row


class RateLike(Protocol):
    """What ``pick_rate`` reads from a row: a ``BurdenRate`` or anything shaped like one."""

    division_id: UUID | None
    effective_from: date
    effective_to: date | None
    rate: Decimal
    basis_note: str | None


def pick_rate[R: RateLike](rows: Iterable[R], day: date, division_id: UUID | None) -> R | None:
    """The row in force on ``day`` among active ``rows``: the division's own row if one
    covers the day, else the tenant-wide row (no division), else ``None``. Periods are
    half-open ``[effective_from, effective_to)``. Pure; active rows never overlap for
    one division (``add_burden_rate``), so at most one row of each kind covers a day."""
    covering = [
        r
        for r in rows
        if r.effective_from <= day and (r.effective_to is None or day < r.effective_to)
    ]
    own = [r for r in covering if division_id is not None and r.division_id == division_id]
    if own:
        return max(own, key=lambda r: r.effective_from)
    company = [r for r in covering if r.division_id is None]
    return max(company, key=lambda r: r.effective_from) if company else None


def active_burden_rates(db: Session) -> list[BurdenRate]:
    """The tenant's active rows (requires tenant context)."""
    return list(
        db.execute(
            select(BurdenRate).where(BurdenRate.active).order_by(BurdenRate.effective_from)
        ).scalars()
    )


def burden_rate_on(db: Session, day: date, division_id: UUID | None = None) -> Decimal | None:
    """The rate in force on ``day`` (``pick_rate`` over the active rows), or ``None``.
    Requires tenant context."""
    row = pick_rate(active_burden_rates(db), day, division_id)
    return None if row is None else row.rate


def _plain(row: BurdenRate) -> dict:
    return {
        "division_id": str(row.division_id) if row.division_id else None,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "rate": str(row.rate),
        "basis_note": row.basis_note,
        "active": row.active,
        "at": datetime.now(UTC).isoformat(),
    }
