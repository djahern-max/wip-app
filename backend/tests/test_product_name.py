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


def _occurrences(path: Path) -> int:
    return len(re.findall(rf"\b{re.escape(PRODUCT_NAME)}\b", path.read_text(), re.IGNORECASE))


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
