"""The decisions a brief makes are appended to ``docs/DECISIONS.md`` word for word
(F08: D-39 and D-40). The brief is the source: the live ``current-feature.md`` while
the feature is in flight, ``docs/briefs/F08.md`` after close-out."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DECISIONS = REPO / "docs" / "DECISIONS.md"


def _brief() -> str:
    closed = REPO / "docs" / "briefs" / "F08.md"
    live = REPO / "current-feature.md"
    text = live.read_text()
    if "## D-39 ·" not in text and closed.exists():
        text = closed.read_text()
    return text


def _entry(text: str, number: str) -> str:
    """One decision's text: from its heading to the next heading (## or ###) or the end."""
    m = re.search(rf"^## {re.escape(number)} ·.*$", text, re.M)
    assert m, f"{number} not found"
    rest = text[m.start() :]
    nxt = re.search(r"^#{2,3} ", rest[1:], re.M)
    body = rest if nxt is None else rest[: nxt.start() + 1]
    return body.rstrip("\n")


def test_d39_and_d40_are_in_decisions_after_d38_word_for_word() -> None:
    decisions = DECISIONS.read_text()
    brief = _brief()
    order = [m.group(1) for m in re.finditer(r"^## (D-\d+[a-z]?) ·", decisions, re.M)]
    assert order[-3:] == ["D-38", "D-39", "D-40"]
    for number in ("D-39", "D-40"):
        assert _entry(decisions, number) == _entry(brief, number), number
