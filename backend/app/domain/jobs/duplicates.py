"""Possible duplicate customers (§10 ``CUSTOMER_FUZZY``; owner's answer 13). Pure and
read-only: the owner merges in QuickBooks and the platform follows; nothing is written.

Two active top-level customers are a pair when their names, case-folded with
punctuation, the generic tokens and the suffixes dropped (``names.duplicate_key``),
have the same words, or when the shorter is the longer less exactly one word (a first
name removed) and a shared word has three or more letters.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.jobs.names import duplicate_key

_MIN_LETTERS = 3


@dataclass(frozen=True)
class NamedRow:
    id: UUID
    display_name: str


def _anchored(words: tuple[str, ...]) -> bool:
    return any(w.isalpha() and len(w) >= _MIN_LETTERS for w in words)


def duplicate_pairs(rows: Sequence[NamedRow]) -> list[tuple[NamedRow, NamedRow]]:
    keyed = [(duplicate_key(r.display_name), r) for r in rows]
    by_key: dict[tuple[str, ...], list[NamedRow]] = {}
    for key, r in keyed:
        if key:
            by_key.setdefault(key, []).append(r)
    pairs: set[tuple[UUID, UUID]] = set()
    rows_by_id = {r.id: r for r in rows}

    def add(a: NamedRow, b: NamedRow) -> None:
        if a.id != b.id:
            pairs.add(tuple(sorted((a.id, b.id), key=str)))  # type: ignore[arg-type]

    for group in by_key.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                add(a, b)
    for key, r in keyed:
        if len(key) < 2:
            continue
        for i in range(len(key)):
            shorter = key[:i] + key[i + 1 :]
            if not _anchored(shorter):
                continue
            for other in by_key.get(shorter, []):
                add(r, other)
    out = [(rows_by_id[a], rows_by_id[b]) for a, b in pairs]
    return sorted(out, key=lambda p: (p[0].display_name.casefold(), p[1].display_name.casefold()))
