"""D-29: ``webhook_event`` is the one ingestion table without ``tenant_id``. It may be
touched without tenant context in exactly two places: the webhook route stores a
delivery, and the Connections page counts the last 24 hours of deliveries for the
realm of the tenant it is acting for. This static check pins that: a third reference
anywhere in the application package fails the suite until a decision allows it."""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND / "app"
MODEL = APP_DIR / "ingest" / "models.py"
CATALOG = APP_DIR / "tenancy" / "rls.py"  # the APPEND_ONLY_TABLES entry, a name only
MODULE = APP_DIR / "integrations" / "qbo" / "webhooks.py"

ALLOWED_FUNCTIONS = {"store_delivery", "deliveries_in_window"}


def _functions_using(source: str, pattern: str) -> set[str]:
    """Names of the top-level functions whose bodies mention ``pattern``."""
    hits: set[str] = set()
    current: str | None = None
    for line in source.splitlines():
        m = re.match(r"^(?:async\s+)?def\s+(\w+)\s*\(", line)
        if m:
            current = m.group(1)
            continue
        if line and not line.startswith((" ", "\t", ")", "#")):
            current = None
        if re.search(pattern, line) and current is not None:
            hits.add(current)
    return hits


def test_webhook_event_is_referenced_only_by_its_two_tenant_less_reads() -> None:
    pattern = r"\bWebhookEvent\b|\bwebhook_event\b"
    others = sorted(
        str(p.relative_to(BACKEND))
        for p in APP_DIR.rglob("*.py")
        if p not in (MODEL, CATALOG, MODULE) and re.search(pattern, p.read_text())
    )
    assert others == [], f"webhook_event is referenced outside webhooks.py: {others}"

    catalog = CATALOG.read_text()
    assert re.search(r'"webhook_event",', catalog), "APPEND_ONLY_TABLES must list it (D-29)"
    assert len(re.findall(r"webhook_event", catalog)) == 1

    module = MODULE.read_text()
    code_only = "\n".join(
        line for line in module.splitlines() if not line.lstrip().startswith(("#", '"""', "- "))
    )
    assert _functions_using(code_only, r"\bWebhookEvent\b") == ALLOWED_FUNCTIONS
