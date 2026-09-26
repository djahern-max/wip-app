"""The product name appears in exactly one backend constant and one frontend constant.

Since D-33 the name is the hostname, one string (D-27), so every occurrence of that string
is an occurrence of the name: there is no hostname exclusion. Two readings are not namings.
An e-mail address at the domain (``admin@…``) is an address. The entity line on each of the
three static pages (F05.0) names the product once, and those pages are listed here as the
one allowed mention outside the constants.
"""

import re
from pathlib import Path

from app.core.product import PRODUCT_NAME

REPO = Path(__file__).resolve().parents[2]
BACKEND_CONSTANT = REPO / "backend/app/core/product.py"
FRONTEND_CONSTANT = REPO / "frontend/src/product.js"

# The hostname (D-27) is the same string as the name (D-33); test_deploy.py reads it as this.
DOMAIN = PRODUCT_NAME

# The one allowed mention outside the constants: the entity line, once per page.
ENTITY_LINE_PAGES = ("privacy.html", "terms.html", "qbo-disconnected.html")

# A naming: the string as a word, not the local part of an address (``@`` before it) and
# not a prefix of a longer word.
_NAME = re.compile(rf"(?<![@\w]){re.escape(PRODUCT_NAME)}(?!\w)", re.IGNORECASE)


def _source_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    skip = {"node_modules", ".venv", "dist", "__pycache__", ".pytest_cache", ".ruff_cache"}
    return [
        p
        for p in root.rglob("*")
        if p.suffix in suffixes and not (skip & set(p.parts)) and p.is_file()
    ]


def _occurrences(path: Path) -> int:
    return len(_NAME.findall(path.read_text()))


def test_backend_constant_is_the_only_backend_occurrence() -> None:
    assert _occurrences(BACKEND_CONSTANT) == 1
    others = [
        p
        for p in _source_files(REPO / "backend", (".py", ".ini", ".toml", ".mako"))
        if p != BACKEND_CONSTANT and p != Path(__file__) and _occurrences(p)
    ]
    assert others == []


def test_frontend_constant_is_the_only_frontend_occurrence() -> None:
    source = FRONTEND_CONSTANT.read_text()
    frontend_name = re.search(r'PRODUCT_NAME\s*=\s*"([^"]+)"', source).group(1)
    assert frontend_name == PRODUCT_NAME
    # SITE_HOST is defined as PRODUCT_NAME, not as a second literal; SUPPORT_EMAIL is an address.
    assert re.search(r"export const SITE_HOST = PRODUCT_NAME;", source)
    assert _occurrences(FRONTEND_CONSTANT) == 1
    others = {
        p.relative_to(REPO / "frontend").as_posix(): n
        for p in _source_files(REPO / "frontend", (".js", ".jsx", ".html", ".json", ".css"))
        if p != FRONTEND_CONSTANT and (n := _occurrences(p))
    }
    assert others == {f"public/{page}": 1 for page in ENTITY_LINE_PAGES}


def test_index_html_names_the_product_through_placeholders_only() -> None:
    html = (REPO / "frontend/index.html").read_text()
    assert "%PRODUCT_NAME%" in html and "%SITE_HOST%" in html
    assert _NAME.search(html) is None


def test_what_counts_as_a_naming() -> None:
    name = PRODUCT_NAME
    assert len(_NAME.findall(f"the {name} app; {name.upper()}; https://{name}/x")) == 3
    assert len(_NAME.findall(f"mail admin@{name}; {name}elopment")) == 0
