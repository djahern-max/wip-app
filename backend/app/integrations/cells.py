"""Cell readers shared by the file parsers (F06; the F04 rule, generalised).

A value handed back by openpyxl or ``csv`` becomes text, a ``Decimal``, an integer
or an ISO date here, and nothing on this path ever calls ``float()``. A numeric cell
with a decimal point arrives from openpyxl as a ``float``; ``repr`` is the exact
shortest form of what the workbook stores (``1599103.5499999998``), and ``Decimal``
keeps it as digits from there. Money is quantized ``ROUND_HALF_UP`` to the cent in
``cell_money`` and hours to two places in ``cell_hours``: the one rounding point of
the estimate import (CLAUDE.md, Money and math).
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")


class CellError(ValueError):
    """The cell cannot be read as the type asked for. The message names the type,
    never the value."""


def cell_text(value) -> str:
    """The cell as text; an empty cell is ``""``. Integers keep their digits, a whole
    float (``1010.0``) its integer digits, a date its ISO form."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        d = Decimal(repr(value))
        return str(d.quantize(Decimal(1))) if d == d.to_integral_value() else format(d, "f")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise CellError("not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        d = Decimal(repr(value))
    elif isinstance(value, Decimal):
        d = value
    else:
        text = str(value).strip().replace(",", "").replace("$", "").replace(" ", "")
        if text == "":
            return None
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise CellError("not a number") from None
    if not d.is_finite():
        raise CellError("not a number")
    return d


def cell_money(value) -> Decimal | None:
    """``None`` for an empty cell; otherwise the amount to the cent, half up."""
    d = _decimal(value)
    return None if d is None else d.quantize(CENT, rounding=ROUND_HALF_UP)


def cell_hours(value) -> Decimal | None:
    d = _decimal(value)
    return None if d is None else d.quantize(CENT, rounding=ROUND_HALF_UP)


def cell_int(value) -> int | None:
    """A whole number; ``None`` for an empty cell; ``CellError`` for anything else."""
    d = _decimal(value)
    if d is None:
        return None
    if d != d.to_integral_value():
        raise CellError("not a whole number")
    return int(d)


def cell_date(value) -> str | None:
    """An ISO date string; ``None`` for an empty cell."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if text == "":
        return None
    try:
        return date.fromisoformat(
            text[:10] if len(text) > 10 and text[10] in "T " else text
        ).isoformat()
    except ValueError:
        raise CellError("not a date (YYYY-MM-DD)") from None
