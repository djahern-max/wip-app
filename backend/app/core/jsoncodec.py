"""The one JSON codec for every JSON/JSONB column (F03; CLAUDE.md Money; D-20).

Money arrives from QBO as JSON numbers and must be stored as received, so the
serializer writes a ``Decimal`` as a JSON *number* with exactly the digits
``str(Decimal)`` gives (``141366.68000000002``, ``0.10``, ``1E+2``), never as a
string and never through ``float``. The standard library cannot emit a Decimal
natively (``default=`` can only return another object to encode), so the
structure is encoded here; ``json.dumps`` is used only to escape string values.

Rules: dict keys must be ``str`` and are sorted; no whitespace; ``bool`` before
``int``; ``float`` and non-finite Decimals are refused with the key path in the
message; anything else is refused too. The deserializer is ``json.loads`` with
``parse_float=Decimal``. ``create_app_engine`` installs both at the engine, and
SQLAlchemy's psycopg dialect installs them on every connection's adapters map,
so ORM and raw-SQL paths cannot disagree.

``payload_sha256`` hashes the canonical form *before* storage: jsonb normalises
number formatting (``1E+2`` becomes ``100``), so hashing what Postgres returns
would not be stable against what was received.
"""

import hashlib
import json
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from typing import Any

json_loads: Callable[[str | bytes], Any] = partial(json.loads, parse_float=Decimal)


class JSONEncodeError(TypeError):
    """A value that must not be stored as JSON (a float, a non-finite Decimal, an
    unknown type, a non-string key). The message names the path, never the value."""


def canonical_json(obj: Any) -> str:
    parts: list[str] = []
    _encode(obj, parts, "$")
    return "".join(parts)


def payload_sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _encode(obj: Any, out: list[str], path: str) -> None:
    if obj is None:
        out.append("null")
    elif obj is True:
        out.append("true")
    elif obj is False:
        out.append("false")
    elif isinstance(obj, str):
        out.append(json.dumps(obj, ensure_ascii=False))
    elif isinstance(obj, int):
        out.append(int.__repr__(obj))
    elif isinstance(obj, Decimal):
        if not obj.is_finite():
            raise JSONEncodeError(f"non-finite Decimal at {path}")
        out.append(str(obj))
    elif isinstance(obj, float):
        raise JSONEncodeError(f"float at {path}: money and measures must be Decimal")
    elif isinstance(obj, dict):
        out.append("{")
        first = True
        for key in sorted(obj):
            if not isinstance(key, str):
                raise JSONEncodeError(f"non-string key at {path}")
            if not first:
                out.append(",")
            first = False
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _encode(obj[key], out, f"{path}.{key}")
        out.append("}")
    elif isinstance(obj, list | tuple):
        out.append("[")
        for i, item in enumerate(obj):
            if i:
                out.append(",")
            _encode(item, out, f"{path}[{i}]")
        out.append("]")
    else:
        raise JSONEncodeError(f"{type(obj).__name__} at {path} cannot be stored as JSON")
