"""Capture every log record emitted during the suite, including SQLAlchemy's
statement and parameter logging at INFO, so the leak test can prove that no
password, secret, code, or session id reaches a log line.

Alembic's ``fileConfig`` (run by the migration fixtures and tests) resets the root
handlers and the ``sqlalchemy.engine`` level, so ``ensure_capture`` is re-applied
before every test; it is idempotent.
"""

import logging

RECORDS: list[tuple[str, str]] = []  # (logger name, formatted message)


class _Collector(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            RECORDS.append((record.name, self.format(record)))
        except Exception:  # noqa: BLE001 - never let log capture break a test
            RECORDS.append((record.name, str(record.msg)))


_handler = _Collector(level=logging.DEBUG)
_handler.setFormatter(logging.Formatter("%(message)s"))


def ensure_capture() -> None:
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    engine_logger = logging.getLogger("sqlalchemy.engine")
    engine_logger.setLevel(logging.INFO)
    engine_logger.disabled = False
    for name in ("app", "uvicorn", "fastapi"):
        logging.getLogger(name).disabled = False
