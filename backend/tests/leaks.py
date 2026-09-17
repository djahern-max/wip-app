"""Registry of every secret the suite generates or receives, checked against all
captured log output at the end (``test_zz_log_leaks.py``)."""

from collections import defaultdict

LEAKS: dict[str, set[str]] = defaultdict(set)


def record_secret(kind: str, value: str | None) -> None:
    if value:
        LEAKS[kind].add(value)
