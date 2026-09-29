"""The exact token rules F07 suggests with (owner's answers 11 and 13). Each rule is a
constant or one small function; each produces a *suggestion* for a person and is
never called from a path that writes a link (CLAUDE.md: no name matching in write
paths). No fuzzy matching: tokens are equal or they are not.
"""

import re

# Words that say nothing about who a customer is (owner's answer 11). "client" and
# "jobsite" are in the list because anonymized names are "Client NN".
GENERIC_TOKENS = frozenset({"client", "jobsite", "llc", "inc", "and", "&"})
# Also dropped when comparing customers for duplicates (owner's answer 13).
SUFFIX_TOKENS = frozenset({"inc", "llc", "co", "corp", "company", "jr", "sr", "ii", "iii"})

_WORD = re.compile(r"[^\W_]+", re.UNICODE)  # letters and digits; punctuation splits
_ESTIMATE_ID = re.compile(r"^\s*(?:EST)?(\d+)\s*$", re.IGNORECASE)
_MIN_STREET_WORD = 3
# Compass words between a street number and its name ("378 East Dunbarton" and
# "378 E Dunbarton" are one street): skipped, as the one- and two-letter forms are.
DIRECTIONS = frozenset({"north", "south", "east", "west"})


def words(text: str | None) -> list[str]:
    """Case-folded words; punctuation (including "&", ",", "-", ":") separates."""
    return _WORD.findall((text or "").casefold())


def customer_name_key(text: str | None) -> tuple[str, ...] | None:
    """The words of a customer or client name without the generic tokens, in order.
    ``None`` when nothing is left: an empty key never matches anything."""
    key = tuple(w for w in words(text) if w not in GENERIC_TOKENS)
    return key or None


def duplicate_key(text: str | None) -> tuple[str, ...]:
    """The words of a customer name for the duplicates list: generic tokens and
    suffixes dropped, sorted, so word order does not matter."""
    return tuple(sorted(w for w in words(text) if w not in GENERIC_TOKENS | SUFFIX_TOKENS))


def street_tokens(text: str | None) -> set[str]:
    """Each street number with the next word of three or more letters: "378 E
    Dunbarton Rd" and "378 East Dunbarton Road" both give {"378 dunbarton"}. Words
    of one or two letters (E, N, SW) and the compass words are skipped; another
    number ends the search. A text without a street number gives
    nothing (owner's answer 11: the address rule needs a street number)."""
    ws = words(text)
    out: set[str] = set()
    for i, w in enumerate(ws):
        if not w.isdigit():
            continue
        for nxt in ws[i + 1 :]:
            if nxt.isdigit():
                break
            if nxt.isalpha() and len(nxt) >= _MIN_STREET_WORD and nxt not in DIRECTIONS:
                out.add(f"{w} {nxt}")
                break
    return out


def normalized(text: str | None) -> str:
    """Case-folded, trimmed, inner whitespace collapsed: the "by name only" rule."""
    return " ".join((text or "").split()).casefold()


def estimate_id_digits(external_id: str | None) -> str | None:
    """Both EST6115758 and 6115758 give the digits 6115758; anything else gives None."""
    m = _ESTIMATE_ID.match(external_id or "")
    return m.group(1) if m else None


def name_starts_with_estimate_id(display_name: str | None, digits: str) -> bool:
    """§13.2: a QuickBooks name that begins with the estimate id, with or without the
    EST prefix, followed by a non-digit or the end."""
    return bool(re.match(rf"^\s*(?:EST)?{re.escape(digits)}(?!\d)", display_name or "", re.I))
