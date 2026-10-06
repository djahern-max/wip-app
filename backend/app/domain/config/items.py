"""The tenant's QuickBooks items (products and services), read from the latest raw
versions of the ``Item`` entity (F08). Nothing is normalized and nothing is stored: the
sync already holds every item raw (BLUEPRINT §6.2), and the two policy keys that name
items (D-02, D-39) need only a list a person can pick from and a set of ids to check a
PUT against. An item's name is for display; nothing matches on it.

Requires ``app.tenant_id`` on the session (RLS)."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.ingest.raw import latest_raw_versions

SOURCE = "qbo"
ENTITY = "Item"


@dataclass(frozen=True)
class QboItem:
    external_id: str
    name: str
    item_type: str | None  # QuickBooks ``Type``: Service, Inventory, NonInventory…
    active: bool  # ``Active`` on the raw version; false for a deleted version too
    income_account_external_id: str | None
    deleted: bool = False  # F08.2: the latest raw version is a deletion stub

    @property
    def label(self) -> str:
        """The name a picker shows, the state said in words (D-22); built here, never
        on the page."""
        if self.deleted:
            return (
                self.name if self.name.lower().endswith("(deleted)") else f"{self.name} (deleted)"
            )
        return self.name if self.active else f"{self.name} (inactive)"


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def qbo_items(db: Session, tenant_id: UUID) -> list[QboItem]:
    """Every item held: inactive ones kept and marked, and (F08.2) deleted ones kept
    and marked too, ``active`` false (an older invoice may sit on an item since
    retired or deleted; matching is on the id; the page shows them only behind "Show
    inactive items"). Sorted by name, then id, for the picker."""
    out: list[QboItem] = []
    for raw in latest_raw_versions(db, tenant_id, SOURCE, ENTITY):
        payload = raw.payload
        if not isinstance(payload, dict):
            continue
        ident = _text(payload.get("Id")) or raw.external_id
        name = _text(payload.get("FullyQualifiedName")) or _text(payload.get("Name")) or ident
        income = payload.get("IncomeAccountRef")
        out.append(
            QboItem(
                external_id=ident,
                name=name,
                item_type=_text(payload.get("Type")),
                active=payload.get("Active") is not False and not raw.is_deleted,
                income_account_external_id=(
                    _text(income.get("value")) if isinstance(income, dict) else None
                ),
                deleted=raw.is_deleted,
            )
        )
    return sorted(out, key=lambda i: (i.name.casefold(), i.external_id))


def item_ids(db: Session, tenant_id: UUID) -> frozenset[str]:
    return frozenset(i.external_id for i in qbo_items(db, tenant_id))
