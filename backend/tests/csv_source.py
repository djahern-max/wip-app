"""Test-only source kind (F03), registered by ``conftest.py`` and never by the
application (the pattern of ``tests/probes.py``). A CSV with a header row:
``external_id`` (required; a row without one is a ``RejectedItem``), an optional
``_deleted`` column (``1`` = source-reported delete), and any other columns, whose
values are stored as strings except ``amount``-like columns, stored as ``Decimal``.

A file whose first line starts with ``#raise`` makes ``parse`` raise, with the
rest of the file in the message, so tests can prove that content never reaches
the batch error.
"""

import csv
import io
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import BinaryIO

from app.integrations.base import RawItem, RejectedItem, SourceKind, register_source_kind

DECIMAL_COLUMNS = frozenset({"amount", "rate", "total"})


def parse_csv(stream: BinaryIO) -> Iterable[RawItem | RejectedItem]:
    text = stream.read().decode("utf-8")
    if text.startswith("#raise"):
        raise ValueError(f"cannot parse: {text}")
    reader = csv.DictReader(io.StringIO(text))
    for n, row in enumerate(reader, start=2):
        external_id = (row.get("external_id") or "").strip()
        if not external_id:
            yield RejectedItem(reason="missing_external_id", row_number=n)
            continue
        deleted = (row.pop("_deleted", "") or "").strip() == "1"
        payload: dict = {}
        bad = False
        for key, value in row.items():
            if key == "external_id" or key is None:
                continue
            if key in DECIMAL_COLUMNS and value not in (None, ""):
                try:
                    payload[key] = Decimal(value)
                except InvalidOperation:
                    bad = True
                    break
            else:
                payload[key] = value
        if bad:
            yield RejectedItem(reason="bad_amount", row_number=n)
            continue
        yield RawItem(entity_type="row", external_id=external_id, payload=payload, deleted=deleted)


test_csv = SourceKind(
    name="test_csv",
    extensions=frozenset({"csv"}),
    parse=parse_csv,
    source="test",
    description="Test-only CSV source.",
)


def register() -> None:
    register_source_kind(test_csv)
