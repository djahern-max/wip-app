"""Cost categories and cost codes (D-23).

The fourteen categories, in slot order, are seeded per tenant by
``ensure_cost_categories`` (audited, idempotent, one tenant per call: never a
cross-tenant statement). A **cost code** is computed, never stored: the division's
``code_digit`` followed by the category's ``slot`` (SNOW + Labor = 410); a division
without a digit has no codes.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.domain.config.audit import SYSTEM, Actor, audit
from app.domain.config.models import CostCategory, Division

# (slot, name) in slot order — exactly the D-23 list. Adding one needs a decision.
D23_COST_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("10", "Labor"),
    ("20", "Labor Burden"),
    ("30", "Materials"),
    ("35", "Supplies"),
    ("40", "Subcontractors"),
    ("45", "Equipment (owned)"),
    ("47", "Vehicles (owned)"),
    ("50", "Equipment Rental"),
    ("55", "Equipment Maintenance"),
    ("60", "Disposal"),
    ("65", "Fuel"),
    ("70", "Permits & Bonds"),
    ("80", "Warranty"),
    ("90", "Other"),
)


def ensure_cost_categories(db: Session, tenant_id: UUID, actor: Actor = SYSTEM) -> int:
    """Seed the D-23 list for this tenant if it has no categories yet. Returns the
    number of rows created (0 when already seeded). Requires the tenant context."""
    existing = db.execute(select(CostCategory.slot)).scalars().all()
    if existing:
        return 0
    rows = [
        CostCategory(tenant_id=tenant_id, slot=slot, name=name, active=True, sort_order=i)
        for i, (slot, name) in enumerate(D23_COST_CATEGORIES)
    ]
    db.add_all(rows)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.cost_categories_seeded,
        "cost_category",
        None,
        actor,
        after={"slots": [slot for slot, _ in D23_COST_CATEGORIES]},
        decision="D-23",
    )
    return len(rows)


def cost_code(division: Division | None, category: CostCategory | None) -> str | None:
    """``code_digit + slot``, or ``None`` when either side cannot give its part."""
    if division is None or category is None or not division.code_digit:
        return None
    return f"{division.code_digit}{category.slot}"
