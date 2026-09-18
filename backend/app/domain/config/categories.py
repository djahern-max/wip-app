"""Cost categories and cost codes (D-23).

The fourteen categories, in slot order, are seeded per tenant when the tenant is
created (``seed_cost_categories_at_creation``: a second transaction under the new
tenant's context, API and CLI) and, as a safety net for tenants that predate F04,
by ``ensure_cost_categories`` on first read (attributed to ``system``, exactly one
set under concurrency). Both are audited; neither is a cross-tenant statement.
A **cost code** is computed, never stored: the division's
``code_digit`` followed by the category's ``slot`` (SNOW + Labor = 410); a division
without a digit has no codes.
"""

from uuid import UUID

from sqlalchemy import select, text
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


def _seed(db: Session, tenant_id: UUID, actor: Actor, via: str) -> int:
    """Insert the D-23 list if this tenant has none. Callers hold the tenant's
    advisory lock, so two concurrent seeds cannot both insert."""
    if db.execute(select(CostCategory.slot).limit(1)).first() is not None:
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
        via=via,
    )
    return len(rows)


def _lock_tenant_seed(db: Session, tenant_id: UUID) -> None:
    """A transaction-scoped advisory lock keyed by the tenant: a second seeder waits
    for the first to commit, re-checks, and finds the rows already there."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:tenant))"), {"tenant": str(tenant_id)})


def seed_cost_categories_at_creation(db: Session, tenant_id: UUID, actor: Actor) -> int:
    """The creation-time seed (API and CLI): a second transaction under the new
    tenant's context, attributed to the person (or CLI) that created the tenant."""
    _lock_tenant_seed(db, tenant_id)
    return _seed(db, tenant_id, actor, via="tenant_created")


def ensure_cost_categories(db: Session, tenant_id: UUID) -> int:
    """The safety net for tenants that predate F04 (or whose creation-time seed did
    not run): seeds the D-23 list when a tenant has no categories. Attributed to
    ``system`` — never to the user whose read triggered it. Idempotent under
    concurrency: a cheap check first, then the tenant's advisory lock and a re-check,
    so two concurrent first reads produce exactly one set. Returns rows created."""
    if db.execute(select(CostCategory.slot).limit(1)).first() is not None:
        return 0
    _lock_tenant_seed(db, tenant_id)
    return _seed(db, tenant_id, SYSTEM, via="lazy")


def cost_code(division: Division | None, category: CostCategory | None) -> str | None:
    """``code_digit + slot``, or ``None`` when either side cannot give its part."""
    if division is None or category is None or not division.code_digit:
        return None
    return f"{division.code_digit}{category.slot}"
