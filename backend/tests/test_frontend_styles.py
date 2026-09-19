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


def _rules(css: str) -> dict[str, str]:
    """selector → declarations, top-level rules only (media blocks are skipped)."""
    flat = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    out: dict[str, str] = {}
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", flat):
        for selector in block.group(1).split(","):
            key = " ".join(selector.split())
            out[key] = out.get(key, "") + block.group(2)
    return out


def test_the_table_pattern_holds_the_header_and_the_first_column_inside_a_bounded_box() -> None:
    """Owner browser pass 2026-09-19: fixed once for every report from F08 on. The box
    scrolls both ways inside itself and is bounded by the viewport, so its sideways
    scrollbar is on screen; the header row and the first column are held, opaque, the
    top-left cell above both; the first column has a plain right border and a minimum
    width; nothing in the pattern breaks a word unless a cell opts in."""
    rules = _rules(_css())
    wrap = rules[".table-wrap"]
    assert re.search(r"overflow:\s*auto", wrap)
    assert re.search(r"max-height:\s*\d+d?vh", wrap)
    # A collapsed border does not travel with a held cell.
    assert re.search(r"border-collapse:\s*separate", rules[".table"])
    head, first, corner = (
        rules[".table thead th"],
        rules[".table td:first-child"],
        rules[".table thead th:first-child"],
    )
    assert "position: sticky" in head and re.search(r"top:\s*0", head)
    assert "position: sticky" in first and re.search(r"left:\s*0", first)
    for held in (head, first):
        assert "background: var(--surface)" in held  # opaque
    assert re.search(r"border-right:\s*1px solid", first)
    assert re.search(r"min-width:\s*\d", first)

    def z(decl: str) -> int:
        return int(re.search(r"z-index:\s*(\d+)", decl).group(1))

    assert z(corner) > z(head) > z(first)
    for selector in (".table th", ".table td:first-child"):
        assert re.search(r"overflow-wrap:\s*normal", rules[selector]), selector
    breakers = [
        s
        for s, d in rules.items()
        if re.search(r"overflow-wrap:\s*anywhere|word-break:\s*break", d)
    ]
    assert sorted(breakers) == [
        ".item-title",
        ".table td.wrap-anywhere:first-child",
        ".wrap-anywhere",
    ]


def test_the_account_column_never_opts_in_to_breaking_inside_a_word() -> None:
    src = (FRONTEND_SRC / "pages/config/Accounts.jsx").read_text()
    assert "wrap-anywhere" not in src


def test_imports_keeps_the_chosen_source() -> None:
    """The Source is read from and written to ``sourceChoice.js`` (its behaviour is
    tested with ``node --test``); the page never goes back to a hard-coded source."""
    src = (FRONTEND_SRC / "pages/Imports.jsx").read_text()
    assert 'useState("unparsed_file")' not in src
    assert "rememberedSource(" in src and "rememberSource(" in src
    assert "onChange={(e) => chooseKind(e.target.value)}" in src
