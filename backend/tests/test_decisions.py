"""The decisions a brief makes are appended to ``docs/DECISIONS.md`` word for word
(F08: D-39 and D-40; F08.2: D-41; F07.4: D-44, with D-42 and D-43 pasted by the owner).
The brief is the source: the live
``current-feature.md`` while the feature is in flight, ``docs/briefs/Fxx.md`` after
close-out."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DECISIONS = REPO / "docs" / "DECISIONS.md"


def _brief(number: str = "D-39", closed_name: str = "F08.md") -> str:
    closed = REPO / "docs" / "briefs" / closed_name
    live = REPO / "current-feature.md"
    text = live.read_text()
    if f"## {number} ·" not in text and closed.exists():
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
    run = ["D-38", "D-39", "D-40", "D-41", "D-42", "D-43", "D-44"]
    assert order[order.index("D-38") : order.index("D-38") + len(run)] == run
    for number in ("D-39", "D-40"):
        assert _entry(decisions, number) == _entry(brief, number), number


def test_d41_is_in_decisions_after_d40_word_for_word() -> None:
    """F08.2: the owner's decision on the diagnostic's finding, as the brief carries it."""
    decisions = DECISIONS.read_text()
    brief = _brief("D-41", "F08.2.md")
    assert _entry(decisions, "D-41") == _entry(brief, "D-41")


def test_d42_d43_once_and_d44_word_for_word_after_d43() -> None:
    """F07.4: D-42 and D-43 are the owner's paste (2026-10-06), present once each and left
    alone; D-44 is made by the brief and appended word for word."""
    decisions = DECISIONS.read_text()
    order = [m.group(1) for m in re.finditer(r"^## (D-\d+[a-z]?) ·", decisions, re.M)]
    for number in ("D-42", "D-43", "D-44"):
        assert order.count(number) == 1, number
    assert order.index("D-42") == order.index("D-41") + 1
    assert order.index("D-44") == order.index("D-43") + 1
    brief = _brief("D-44", "F07.4.md")
    assert _entry(decisions, "D-44") == _entry(brief, "D-44")
    assert "project manager" in _entry(decisions, "D-42")
    assert "Retainage" in _entry(decisions, "D-43")


def test_d45_is_in_decisions_once_directly_after_d44() -> None:
    """F08.1: D-45 was appended by the owner (2026-10-06) and is left alone; it is the one
    decision after D-44 and names the flag."""
    decisions = DECISIONS.read_text()
    order = [m.group(1) for m in re.finditer(r"^## (D-\d+[a-z]?) ·", decisions, re.M)]
    assert order.count("D-45") == 1
    assert order.index("D-45") == order.index("D-44") + 1
    entry = _entry(decisions, "D-45")
    assert "BILLING_UNAPPROVED_CO" in entry and "assigns earlier invoice lines" in entry
