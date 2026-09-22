"""The product name appears in exactly one backend constant and one frontend constant."""

import re
from pathlib import Path

from app.core.product import PRODUCT_NAME

REPO = Path(__file__).resolve().parents[2]
BACKEND_CONSTANT = REPO / "backend/app/core/product.py"
FRONTEND_CONSTANT = REPO / "frontend/src/product.js"


def _source_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    skip = {"node_modules", ".venv", "dist", "__pycache__", ".pytest_cache", ".ruff_cache"}
    return [
        p
        for p in root.rglob("*")
        if p.suffix in suffixes and not (skip & set(p.parts)) and p.is_file()
    ]


# The hostname (D-27) is a fact of where the platform runs, not the product name, so
# ``jobcost.dev`` does not count (F05.0: the three static pages name it once each).
# When D-09 renames the product, the constant changes and the domain stays.
DOMAIN = "jobcost.dev"
_DOMAIN_TAIL = re.escape(DOMAIN[len(PRODUCT_NAME) :])  # ".dev"
_NAME = re.compile(rf"\b{re.escape(PRODUCT_NAME)}\b(?!{_DOMAIN_TAIL}\b)", re.IGNORECASE)


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
    frontend_name = re.search(r'PRODUCT_NAME\s*=\s*"([^"]+)"', FRONTEND_CONSTANT.read_text()).group(
        1
    )
    assert frontend_name == PRODUCT_NAME
    assert _occurrences(FRONTEND_CONSTANT) == 1
    others = [
        p
        for p in _source_files(REPO / "frontend", (".js", ".jsx", ".html", ".json", ".css"))
        if p != FRONTEND_CONSTANT and _occurrences(p)
    ]
    assert others == []


def test_the_hostname_form_is_not_counted() -> None:
    assert DOMAIN.startswith(PRODUCT_NAME)
    assert len(_NAME.findall("operated at jobcost.dev, mail admin@jobcost.dev")) == 0
    assert len(_NAME.findall("the jobcost app; JOBCOST; jobcost.development")) == 3
