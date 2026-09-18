"""Source registry (F03). A *source kind* declares its name, the file extensions
it accepts, and ``parse(stream) -> Iterable[RawItem]``. The import pipeline stores
and checksums the file, then calls ``parse`` and hands each item to ``store_raw``.
It knows nothing about what the items mean: LMN, isolved and the closing-report
PDF (F06, F11) register kinds here; they add no branch to domain code.

F03 registers one production kind, ``unparsed_file``, which stores the file and
yields no records, so the pipeline can be exercised end to end before any parser
exists. A test-only CSV kind lives under ``tests/`` and is registered by the test
harness (the same pattern as ``tests/probes.py``).

``RawItem.external_id`` is the string the source supplies. A source must supply a
deterministic key for rows that arrive without one; how LMN does that is F06's
question. ``deleted=True`` records a source-reported delete (a new version with
``is_deleted``; D-20).
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, BinaryIO


@dataclass(frozen=True)
class RawItem:
    entity_type: str
    external_id: str
    payload: Any  # JSON-compatible; money as Decimal (app.core.jsoncodec)
    deleted: bool = False


@dataclass(frozen=True)
class RejectedItem:
    """A row the source could not read. Counted in ``rows_rejected``; the rest of
    the file still loads (CLAUDE.md: importers never fail a whole file for bad
    rows). ``reason`` is a short code, never the row's content. The ``exception``
    rows that let a person resolve it belong to F06/F09."""

    reason: str
    row_number: int | None = None


@dataclass(frozen=True)
class SourceKind:
    name: str
    extensions: frozenset[str]  # lower-case, without the dot; empty = any
    # A ``parse`` that raises fails the whole batch; a bad row is a RejectedItem.
    parse: Callable[[BinaryIO], Iterable[RawItem | RejectedItem]]
    # ``raw_record.source`` for items of this kind (and the connection system in F05).
    source: str
    # What a person sees (D-22: human labels, never machine tokens on screen).
    label: str = ""
    description: str = ""

    def accepts(self, filename: str) -> bool:
        if not self.extensions:
            return True
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ext in self.extensions


SOURCE_KINDS: dict[str, SourceKind] = {}


def register_source_kind(kind: SourceKind) -> SourceKind:
    if not kind.label:
        raise ValueError(f"source kind {kind.name!r} needs a display label")
    if kind.name in SOURCE_KINDS and SOURCE_KINDS[kind.name] is not kind:
        raise ValueError(f"source kind {kind.name!r} is already registered")
    SOURCE_KINDS[kind.name] = kind
    return kind


def get_source_kind(name: str) -> SourceKind:
    try:
        return SOURCE_KINDS[name]
    except KeyError:
        raise LookupError(f"unknown source kind {name!r}") from None


def _parse_nothing(_stream: BinaryIO) -> Iterable[RawItem | RejectedItem]:
    return ()


unparsed_file = register_source_kind(
    SourceKind(
        name="unparsed_file",
        extensions=frozenset(),
        parse=_parse_nothing,
        source="file",
        label="Unparsed file",
        description="Stores and checksums a file; yields no records (F03 pipeline check).",
    )
)
