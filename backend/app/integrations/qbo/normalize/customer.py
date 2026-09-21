"""Customer → ``customer`` (S-01 (a)): a project is ``IsProject = true``; ``Job``
alone means a sub-customer. Names are data; nothing matches on them."""

from dataclasses import dataclass

from app.integrations.qbo.normalize import Unreadable
from app.integrations.qbo.normalize.money import external_id, ref_value


@dataclass(frozen=True)
class CustomerRow:
    external_id: str
    display_name: str
    parent_external_id: str | None
    is_project: bool
    active: bool


def normalize_customer(payload: dict) -> CustomerRow:
    if not isinstance(payload, dict):
        raise Unreadable("payload_not_an_object")
    name = payload.get("DisplayName")
    if not isinstance(name, str) or not name:
        raise Unreadable("display_name_missing")
    return CustomerRow(
        external_id=external_id(payload),
        display_name=name[:500],
        parent_external_id=ref_value(payload.get("ParentRef"), field="parent", required=False),
        is_project=payload.get("IsProject") is True,
        active=payload.get("Active") is not False,
    )
