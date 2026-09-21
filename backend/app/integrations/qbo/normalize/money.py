"""Cents, exactly (owner answer 6, F05). QuickBooks sends amounts as JSON numbers;
the codec gives ``Decimal`` (or ``int``). An amount with more than two decimals is
refused, never rounded: rounding happens once, in ``app/wip/calc.py``, and nowhere
else."""

from datetime import date
from decimal import Decimal

from app.integrations.qbo.normalize import Unreadable

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def cents(value, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise Unreadable(f"{field}_not_a_number")
    amount = Decimal(value)
    if not amount.is_finite():
        raise Unreadable(f"{field}_not_finite")
    if amount != amount.quantize(CENT):
        raise Unreadable(f"{field}_more_than_two_decimals")
    return amount.quantize(CENT)


def cents_or_zero(value, *, field: str) -> Decimal:
    return ZERO if value is None else cents(value, field=field)


def iso_date(value, *, field: str) -> date:
    if not isinstance(value, str):
        raise Unreadable(f"{field}_missing")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise Unreadable(f"{field}_not_a_date") from None


def ref_value(ref, *, field: str, required: bool) -> str | None:
    """``{"value": "12", "name": "…"}`` → ``"12"``."""
    if ref is None:
        if required:
            raise Unreadable(f"{field}_missing")
        return None
    if not isinstance(ref, dict) or not isinstance(ref.get("value"), str) or not ref["value"]:
        raise Unreadable(f"{field}_unreadable")
    return ref["value"]


def external_id(payload: dict) -> str:
    value = payload.get("Id")
    if not isinstance(value, str) or not value:
        raise Unreadable("id_missing")
    return value


def home_currency_only(payload: dict) -> None:
    """Multi-currency is out of scope (F05): a document in another currency carries
    ``ExchangeRate`` other than 1."""
    rate = payload.get("ExchangeRate")
    if rate is not None and rate != 1:
        raise Unreadable("foreign_currency")
