"""Accounting-policy keys (F04). One row per key per tenant; typed; **no key has a
default anywhere**: an unset key reads as "not decided" and ``require_policy``
raises ``PolicyNotDecided`` naming the key, which later features surface rather than
guess (BLUEPRINT §8.3, D-04). Setting a key is a ``firm_admin`` action recorded with
who, when, and a decision reference, audited with before and after.

Money values (``small_job_threshold``) are ``Decimal`` end to end: accepted as a
string, stored as a JSON number by the F03 codec, returned as a string.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.domain.config.audit import Actor, audit
from app.domain.config.models import CostCategory, TenantPolicy


class PolicyNotDecided(LookupError):
    def __init__(self, key: str) -> None:
        super().__init__(f"policy {key!r} has not been decided for this tenant")
        self.key = key


class PolicyValueError(ValueError):
    """The value does not fit the key's type; the message says what fits."""


@dataclass(frozen=True)
class PolicyKey:
    key: str
    label: str
    kind: str  # timezone | month | category_slots | money | text
    description: str


# The registry. Deliberately no ``default`` field: see the module docstring and the
# static test in tests/test_policy.py.
POLICY_KEYS: dict[str, PolicyKey] = {
    k.key: k
    for k in (
        PolicyKey(
            "timezone", "Time zone", "timezone", "Period boundaries are tenant-local months."
        ),
        PolicyKey(
            "fiscal_year_start_month",
            "Fiscal year starts in",
            "month",
            "1 = January … 12 = December.",
        ),
        PolicyKey(
            "wip_basis",
            "WIP basis",
            "category_slots",
            "Cost categories counted in both cost to date and EAC (BLUEPRINT §8.3).",
        ),
        PolicyKey(
            "small_job_threshold",
            "Small job threshold",
            "money",
            "Revised contract below which a job is treated as a small job.",
        ),
        PolicyKey(
            "deposit_identification",
            "Deposit identification",
            "text",
            "How a customer deposit is recognised in QuickBooks (D-02).",
        ),
        PolicyKey(
            "fuel_surcharge_treatment",
            "Fuel surcharge treatment",
            "text",
            "How a fuel surcharge on an invoice is treated (D-04).",
        ),
    )
}


def validate_value(db: Session, key: str, value):
    """Return the value to store, or raise ``PolicyValueError``."""
    spec = POLICY_KEYS.get(key)
    if spec is None:
        raise PolicyValueError(f"unknown policy key {key!r}")
    if spec.kind == "timezone":
        if not isinstance(value, str) or not value:
            raise PolicyValueError("a time zone name such as America/New_York")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise PolicyValueError("a time zone name such as America/New_York") from None
        return value
    if spec.kind == "month":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
            raise PolicyValueError("a month number from 1 to 12")
        return value
    if spec.kind == "category_slots":
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise PolicyValueError('a list of cost category slots such as ["10", "20"]')
        known = set(db.execute(select(CostCategory.slot).where(CostCategory.active)).scalars())
        unknown = sorted(set(value) - known)
        if unknown:
            raise PolicyValueError(f"unknown cost category slot(s): {', '.join(unknown)}")
        return sorted(set(value))
    if spec.kind == "money":
        if not isinstance(value, str):
            raise PolicyValueError('an amount as a string, such as "25000.00"')
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise PolicyValueError('an amount as a string, such as "25000.00"') from None
        if not amount.is_finite() or amount < 0:
            raise PolicyValueError("an amount of zero or more")
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if spec.kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise PolicyValueError("a short text")
        return value.strip()[:500]
    raise PolicyValueError(f"unknown policy kind {spec.kind!r}")


def get_policy(db: Session, key: str) -> TenantPolicy | None:
    if key not in POLICY_KEYS:
        raise PolicyValueError(f"unknown policy key {key!r}")
    return db.execute(select(TenantPolicy).where(TenantPolicy.key == key)).scalar_one_or_none()


def require_policy(db: Session, key: str):
    row = get_policy(db, key)
    if row is None:
        raise PolicyNotDecided(key)
    return row.value


def set_policy(
    db: Session, tenant_id: UUID, key: str, value, *, decision_ref: str, actor: Actor
) -> TenantPolicy:
    if POLICY_KEYS.get(key) and POLICY_KEYS[key].kind == "category_slots":
        from app.domain.config.categories import ensure_cost_categories

        ensure_cost_categories(db, tenant_id)
    stored = validate_value(db, key, value)
    ref = (decision_ref or "").strip()
    if not ref:
        raise PolicyValueError("a decision reference (who decided, where it is written down)")
    row = get_policy(db, key)
    before = None if row is None else {"value": _plain(row.value), "decision_ref": row.decision_ref}
    if row is None:
        row = TenantPolicy(tenant_id=tenant_id, key=key)
        db.add(row)
    row.value = stored
    row.decided_by = actor.user_id
    row.decided_at = datetime.now(UTC)
    row.decision_ref = ref[:200]
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.policy_set,
        "tenant_policy",
        key,
        actor,
        before=before,
        after={"value": _plain(stored), "decision_ref": row.decision_ref},
    )
    return row


def _plain(value):
    """JSON-safe for an audit row and an API response: money as a string."""
    if isinstance(value, Decimal):
        return str(value)
    return value
