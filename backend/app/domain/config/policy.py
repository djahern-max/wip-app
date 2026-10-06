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

F08 (D-02, D-39): ``deposit_identification`` is the tenant's deposit item ids, at least
one; ``fuel_surcharge_treatment`` is the fuel surcharge item ids and the rate (a fraction
kept as a string, ``"0.0500"``), where no items and no rate is "this company charges no
fuel surcharge", a decision like any other. Item ids are checked against the items the
sync holds (``app.domain.config.items``); a name is never stored.
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
from app.domain.config.items import item_ids as held_item_ids
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
    kind: str  # timezone | month | category_slots | money | text | item_ids | surcharge
    description: str
    waiting: str | None = None  # F04.1: why the key cannot be set on the screen yet


# Key names other modules may import (tests/test_policy.py: a key name is a string
# literal only in this file).
WIP_BASIS = "wip_basis"
TIMEZONE = "timezone"
DEPOSIT_IDENTIFICATION = "deposit_identification"  # F08 (D-02)
FUEL_SURCHARGE_TREATMENT = "fuel_surcharge_treatment"  # F08 (D-39)
ITEM_KINDS: frozenset[str] = frozenset({"item_ids", "surcharge"})  # the two item pickers
RATE_PLACES = Decimal("0.0001")

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
            DEPOSIT_IDENTIFICATION,
            "Deposit identification",
            "item_ids",
            "The QuickBooks items a deposit invoice uses (D-02).",
        ),
        PolicyKey(
            FUEL_SURCHARGE_TREATMENT,
            "Fuel surcharge treatment",
            "surcharge",
            "The QuickBooks items a fuel surcharge line uses, and the rate (D-39).",
        ),
    )
}


def _item_list(db: Session, tenant_id: UUID | None, value, *, field: str) -> list[str]:
    """A list of the tenant's item ids: strings, de-duplicated, sorted, each one held."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PolicyValueError(f'a list of QuickBooks item ids under "{field}", such as ["12"]')
    ids = sorted({v.strip() for v in value})
    if ids:
        if tenant_id is None:
            raise PolicyValueError("the company's items to check against")
        unknown = [i for i in ids if i not in held_item_ids(db, tenant_id)]
        if unknown:
            raise PolicyValueError(
                f"item id(s) this company does not have in QuickBooks: {', '.join(unknown)}"
            )
    return ids


def _rate(value) -> str | None:
    """A fraction of the amount due (D-39), ``"0.0500"`` for 5.00%; stored as a string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyValueError('a rate as a fraction in a string, such as "0.0500" for 5.00%')
    try:
        rate = Decimal(value.strip())
    except InvalidOperation:
        raise PolicyValueError(
            'a rate as a fraction in a string, such as "0.0500" for 5.00%'
        ) from None
    if not rate.is_finite() or not (Decimal("0") < rate < Decimal("1")):
        raise PolicyValueError("a rate above 0 and below 1 (5.00% is 0.0500)")
    return format(rate.quantize(RATE_PLACES, rounding=ROUND_HALF_UP), "f")


def validate_value(db: Session, key: str, value, *, tenant_id: UUID | None = None):
    """Return the value to store, or raise ``PolicyValueError``. The item kinds (F08)
    check ids against the items the sync holds for ``tenant_id``."""
    spec = POLICY_KEYS.get(key)
    if spec is None:
        raise PolicyValueError(f"unknown policy key {key!r}")
    if spec.kind == "item_ids":
        if not isinstance(value, dict):
            raise PolicyValueError('an object {"item_ids": [...]} naming at least one item')
        ids = _item_list(db, tenant_id, value.get("item_ids"), field="item_ids")
        if not ids:
            raise PolicyValueError("at least one QuickBooks item: a deposit invoice is on one")
        return {"item_ids": ids}
    if spec.kind == "surcharge":
        if not isinstance(value, dict):
            raise PolicyValueError(
                'an object {"item_ids": [...], "rate": "0.0500"}; no items and no rate '
                "records that this company charges no fuel surcharge"
            )
        ids = _item_list(db, tenant_id, value.get("item_ids", []), field="item_ids")
        rate = _rate(value.get("rate"))
        if rate is not None and not ids:
            raise PolicyValueError(
                "the fuel surcharge items with the rate, or no items and no rate to record "
                "that this company charges no fuel surcharge"
            )
        return {"item_ids": ids, "rate": rate}
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
    stored = validate_value(db, key, value, tenant_id=tenant_id)
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


# --- F08: what the billing figures read ---------------------------------------------------------


@dataclass(frozen=True)
class SurchargeTreatment:
    item_ids: frozenset[str]  # empty with rate None: this company charges no fuel surcharge
    rate: Decimal | None  # None: not decided yet (F08 does not read it; F08.1 requires it)


def deposit_items(db: Session) -> frozenset[str] | None:
    """The deposit item ids (D-02), or None when the key is not decided. Never a default."""
    row = get_policy(db, DEPOSIT_IDENTIFICATION)
    if row is None or not isinstance(row.value, dict):
        return None
    return frozenset(str(i) for i in row.value.get("item_ids") or ())


def surcharge_treatment(db: Session) -> SurchargeTreatment | None:
    """The fuel surcharge items and rate (D-39), or None when the key is not decided."""
    row = get_policy(db, FUEL_SURCHARGE_TREATMENT)
    if row is None or not isinstance(row.value, dict):
        return None
    rate = row.value.get("rate")
    return SurchargeTreatment(
        item_ids=frozenset(str(i) for i in row.value.get("item_ids") or ()),
        rate=Decimal(str(rate)) if rate is not None else None,
    )
