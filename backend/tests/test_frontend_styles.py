"""Interface conventions, statically (D-22; F03.1). One hand-written stylesheet,
imported once; no inline ``style={{`` objects in ``frontend/src`` (allow-list empty
until an entry is justified); the one accent colour is a CSS variable used only for
the primary action and links; one primary action per screen. Same approach as
``test_frontend_effects.py``: no JS parser, no new dependency."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO / "frontend/src"

# file (relative to frontend/src) → why an inline style is justified there. Empty.
INLINE_STYLE_ALLOWED: dict[str, str] = {}

_INLINE_STYLE = re.compile(r"\bstyle=\{\{")


def _css() -> str:
    """The stylesheet without its comments (prose may name what the rules forbid)."""
    return re.sub(r"/\*.*?\*/", "", (FRONTEND_SRC / "styles.css").read_text(), flags=re.S)


def _sources() -> list[tuple[str, str]]:
    return [
        (p.relative_to(FRONTEND_SRC).as_posix(), p.read_text())
        for p in sorted(FRONTEND_SRC.rglob("*"))
        if p.suffix in (".js", ".jsx")
    ]


def test_no_inline_style_objects_outside_the_allow_list() -> None:
    offenders = {
        rel: [src.count("\n", 0, m.start()) + 1 for m in _INLINE_STYLE.finditer(src)]
        for rel, src in _sources()
        if _INLINE_STYLE.search(src) and rel not in INLINE_STYLE_ALLOWED
    }
    assert offenders == {}, (
        f"inline style objects (D-22: one stylesheet): {offenders}. Move them to "
        "frontend/src/styles.css, or add the file to INLINE_STYLE_ALLOWED with the reason."
    )
    stale = sorted(
        set(INLINE_STYLE_ALLOWED) - {rel for rel, s in _sources() if _INLINE_STYLE.search(s)}
    )
    assert stale == []


def test_exactly_one_stylesheet_imported_once() -> None:
    sheets = sorted(p.relative_to(FRONTEND_SRC).as_posix() for p in FRONTEND_SRC.rglob("*.css"))
    assert sheets == ["styles.css"]
    importers = [rel for rel, src in _sources() if re.search(r"import\s+\"\./styles\.css\"", src)]
    assert importers == ["main.jsx"]
    assert not any(re.search(r"\.css['\"]", src) for rel, src in _sources() if rel != "main.jsx")


def test_system_font_stack_and_no_decoration() -> None:
    css = _css()
    assert re.search(r"font-family:\s*system-ui", css)
    for forbidden in ("gradient(", "box-shadow", "animation", "transition", "@keyframes", "url("):
        assert forbidden not in css, forbidden


def test_one_accent_colour_used_only_for_primary_action_and_links() -> None:
    css = _css()
    assert css.count("--accent:") == 1
    # Every rule that uses the accent, by selector.
    users = []
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if "var(--accent)" in block.group(2):
            users.append(" ".join(block.group(1).split()))
    assert sorted(users) == [".button-primary", ".link-button", "a"], users
    # No other colour literal is used for text or backgrounds outside the variables.
    literals = re.findall(r"#[0-9a-fA-F]{3,6}\b", css.split("}", 1)[1])  # after :root
    assert literals == [], f"colour literals outside :root: {literals}"


def test_one_primary_action_per_screen() -> None:
    """Each screen file renders at most one ``button-primary`` per ``<main`` it draws
    (TotpEnrol draws two screens). Imports is drawn inside Shell's main."""
    for rel, src in _sources():
        if not rel.startswith("pages/"):
            continue
        primaries = src.count('className="button-primary"')
        screens = max(1, src.count("<main"))
        assert primaries <= screens, f"{rel}: {primaries} primary actions for {screens} screen(s)"
    assert sum(s.count('className="button-primary"') for _, s in _sources()) >= 5


def test_machine_tokens_are_not_rendered_by_the_imports_page() -> None:
    """The page shows the display fields; the machine fields are never interpolated
    into text (they may still be compared, e.g. ``b.status === "failed"``)."""
    src = (FRONTEND_SRC / "pages/Imports.jsx").read_text()
    for field in ("source_kind", "error_detail"):
        assert not re.search(rf"\{{b\.{field}\}}", src), field
    assert not re.search(r"\{b\.status\}", src)
    for shown in ("b.source_label", "b.status_label", "b.message", "k.label"):
        assert shown in src, shown
