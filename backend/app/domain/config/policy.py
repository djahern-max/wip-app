"""Accounting-policy keys (F04). One row per key per tenant; typed; **no key has a
default anywhere**: an unset key reads as "not decided" and ``require_policy``
raises ``PolicyNotDecided`` naming the key, which later features surface rather than
guess (BLUEPRINT §8.3, D-04). Setting a key is a ``firm_admin`` action recorded with
who, when, and an optional reference to where the decision is written down (F04.1),
audited with before and after.

A key whose feature has not arrived carries a ``waiting`` sentence (F04.1): the screen
shows the sentence in place of "Decide" and the route refuses a PUT with it. The
feature that first reads the key removes the sentence and gives the key its control.
``set_policy`` itself accepts any key: a value a later feature or a test stores on a
waiting key is still read back with who and when.

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


class PolicyWaiting(LookupError):
    """The key cannot be set yet; the message is the registry's waiting sentence."""

    def __init__(self, key: str) -> None:
        super().__init__(POLICY_KEYS[key].waiting or "")
        self.key = key


@dataclass(frozen=True)
class PolicyKey:
    key: str
    label: str
    kind: str  # timezone | month | category_slots | money | text
    description: str
    waiting: str | None = None  # F04.1: why the key cannot be set on the screen yet


# Key names other modules may import (tests/test_policy.py: a key name is a string
# literal only in this file).
WIP_BASIS = "wip_basis"
TIMEZONE = "timezone"

# F04.1: the United States time zones offered on the screen, zone name to plain words.
# One mapping for the label and the stored value (D-22): the API returns the options
# and the decided value's label, and the page never builds a label itself. Any valid
# zone name is still accepted by ``validate_value``; one off this list is shown and
# offered as its own name so saving never loses it.
TIME_ZONE_LABELS: dict[str, str] = {
    "America/New_York": "Eastern",
    "America/Chicago": "Central",
    "America/Denver": "Mountain",
    "America/Phoenix": "Arizona",
    "America/Los_Angeles": "Pacific",
    "America/Anchorage": "Alaska",
    "Pacific/Honolulu": "Hawaii",
}


def time_zone_label(zone: str) -> str:
    """The plain words with the zone name, "Eastern (America/New_York)", for a listed
    zone; the zone name alone for any other."""
    words = TIME_ZONE_LABELS.get(zone)
    return f"{words} ({zone})" if words else zone


def time_zone_options(stored: str | None) -> list[tuple[str, str]]:
    """The drop-down: (zone name, label) for each listed zone, plus the stored zone
    when it is not on the list."""
    options = [(zone, time_zone_label(zone)) for zone in TIME_ZONE_LABELS]
    if stored and stored not in TIME_ZONE_LABELS:
        options.append((stored, stored))
    return options


# The registry. Deliberately no ``default`` field: see the module docstring and the
# static test in tests/test_policy.py.
POLICY_KEYS: dict[str, PolicyKey] = {
    k.key: k
    for k in (
        PolicyKey(TIMEZONE, "Time zone", "timezone", "Period boundaries are tenant-local months."),
        PolicyKey(
            "fiscal_year_start_month",
            "Fiscal year starts in",
            "month",
            "1 = January … 12 = December.",
        ),
        PolicyKey(
            WIP_BASIS,
            "WIP basis",
            "category_slots",
            "Cost categories counted in both cost to date and EAC (BLUEPRINT §8.3).",
        ),
        PolicyKey(
            "small_job_threshold",
            "Small job threshold",
            "money",
            "Revised contract below which a job is treated as a small job.",
            waiting="Not decided (D-08). Set when the WIP schedule arrives.",
        ),
        PolicyKey(
            "deposit_identification",
            "Deposit identification",
            "text",
            "The QuickBooks item a deposit invoice uses (D-02).",
            waiting="Set with the billing reports (F08).",
        ),
        PolicyKey(
            "fuel_surcharge_treatment",
            "Fuel surcharge treatment",
            "text",
            "How a fuel surcharge line on an invoice is recognised.",
            waiting="Set with the billing reports (F08).",
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


def check_settable(key: str) -> None:
    """Raise ``PolicyWaiting`` when the key's feature has not arrived (F04.1). Called
    by the route, not by ``set_policy``."""
    spec = POLICY_KEYS.get(key)
    if spec is None:
        raise PolicyValueError(f"unknown policy key {key!r}")
    if spec.waiting:
        raise PolicyWaiting(key)


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
    db: Session,
    tenant_id: UUID,
    key: str,
    value,
    *,
    decision_ref: str | None = None,
    actor: Actor,
) -> TenantPolicy:
    if POLICY_KEYS.get(key) and POLICY_KEYS[key].kind == "category_slots":
        from app.domain.config.categories import ensure_cost_categories

        ensure_cost_categories(db, tenant_id)
    stored = validate_value(db, key, value)
    ref = (decision_ref or "").strip()  # F04.1: optional; blank is stored as ""
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
