"""Burden rates (F04): effective-dated fractions (``NUMERIC(7,4)``, Decimal end to
end), optionally per division; no overlapping periods for the same division; one
function answers "the rate in force on this date"."""

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
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


def burden_rate_on(db: Session, day: date, division_id: UUID | None = None) -> Decimal | None:
    """The rate in force on ``day``: the division's own rate if one covers the day,
    else the tenant-wide rate (no division), else ``None``. Requires tenant context."""
    rows = db.execute(
        select(BurdenRate)
        .where(BurdenRate.active, BurdenRate.effective_from <= day)
        .order_by(BurdenRate.effective_from.desc())
    ).scalars()
    fallback: Decimal | None = None
    for row in rows:
        if row.effective_to is not None and day >= row.effective_to:
            continue
        if row.division_id == division_id and division_id is not None:
            return row.rate
        if row.division_id is None and fallback is None:
            fallback = row.rate
    return fallback


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
