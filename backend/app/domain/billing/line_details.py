"""The quantity, rate and service date of a document's lines, read from the raw
QuickBooks payload for display on the job page (F08.1 Part 1). ``billing_line`` keeps
the line id, kind, item, description and amount only; the brief reads the rest from raw
and changes no column (Discovered: a later feature that computes on them adds columns).

The payload comes back from JSONB through the one codec (``parse_float=Decimal``), so a
number here is an ``int`` or a ``Decimal``, never a float. A rate is money and is shown
with cents; a quantity is shown as QuickBooks holds it. Requires ``app.tenant_id`` on the
session (RLS). Reads only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.billing.figures import SALES_ITEM
from app.domain.billing.models import Billing
from app.ingest.models import RawRecord

CENT = Decimal("0.01")


@dataclass(frozen=True)
class LineDetail:
    quantity: str | None
    rate: str | None
    service_date: str | None


EMPTY = LineDetail(None, None, None)


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    d = Decimal(value)
    return d if d.is_finite() else None


def _quantity(value) -> str | None:
    d = _number(value)
    if d is None:
        return None
    text = format(d.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"


def _rate(value) -> str | None:
    d = _number(value)
    return None if d is None else format(d.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _date(value) -> str | None:
    return value if isinstance(value, str) and value else None


def details_from_payload(payload) -> dict[str, LineDetail]:
    """QuickBooks line id → its detail, for the priced lines of one document."""
    out: dict[str, LineDetail] = {}
    lines = payload.get("Line") if isinstance(payload, dict) else None
    for raw in lines or []:
        if not isinstance(raw, dict) or raw.get("DetailType") != SALES_ITEM:
            continue
        detail = raw.get(SALES_ITEM)
        if not isinstance(detail, dict) or raw.get("Id") is None:
            continue
        out[str(raw["Id"])] = LineDetail(
            quantity=_quantity(detail.get("Qty")),
            rate=_rate(detail.get("UnitPrice")),
            service_date=_date(detail.get("ServiceDate")),
        )
    return out


def line_details(db: Session, billing_ids: Sequence[UUID]) -> dict[tuple[str, str], LineDetail]:
    """(document id, QuickBooks line id) → detail, from each document's latest raw
    version (the one its row was built from). One read for the job's documents."""
    if not billing_ids:
        return {}
    out: dict[tuple[str, str], LineDetail] = {}
    for billing_id, payload in db.execute(
        select(Billing.id, RawRecord.payload)
        .join(RawRecord, RawRecord.id == Billing.raw_record_id)
        .where(Billing.id.in_(billing_ids))
    ).all():
        for line_id, detail in details_from_payload(payload).items():
            out[(str(billing_id), line_id)] = detail
    return out
