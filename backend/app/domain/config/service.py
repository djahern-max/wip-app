"""Audited configuration services (F04). Every create, change, confirm and
deactivate on a configuration table writes to ``audit_log`` in the caller's
transaction with before and after values. All functions require the tenant
context on ``db``. Errors are ``ConfigError`` with a sentence a person can act on
(D-22)."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.domain.config.audit import Actor, audit
from app.domain.config.categories import cost_code, ensure_cost_categories
from app.domain.config.models import (
    AccountMap,
    AccountSuggestRule,
    CostCategory,
    Division,
    GlAccount,
)
from app.domain.config.rules import RuleError, compile_pattern


class ConfigError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --- divisions ---------------------------------------------------------------------------------


def _division_plain(d: Division) -> dict:
    return {
        "code": d.code,
        "name": d.name,
        "code_digit": d.code_digit,
        "active": d.active,
        "sort_order": d.sort_order,
    }


def _check_digit(digit: str | None) -> str | None:
    digit = (digit or "").strip()
    if not digit:
        return None
    if len(digit) != 1:
        raise ConfigError(422, "The code digit is one character, such as 4.")
    return digit


def create_division(
    db: Session,
    tenant_id: UUID,
    *,
    code: str,
    name: str,
    code_digit: str | None,
    sort_order: int,
    actor: Actor,
) -> Division:
    code = (code or "").strip().upper()
    name = (name or "").strip()
    if not code or len(code) > 20:
        raise ConfigError(422, "Give the division a short code, such as LS or SNOW.")
    if not name:
        raise ConfigError(422, "Give the division a name.")
    digit = _check_digit(code_digit)
    if db.execute(select(Division.id).where(Division.code == code)).first():
        raise ConfigError(409, f"A division with code {code} already exists.")
    if digit and db.execute(select(Division.id).where(Division.code_digit == digit)).first():
        raise ConfigError(409, f"Another division already uses the digit {digit}.")
    row = Division(
        tenant_id=tenant_id, code=code, name=name, code_digit=digit, sort_order=sort_order
    )
    db.add(row)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.division_created,
        "division",
        row.id,
        actor,
        after=_division_plain(row),
    )
    return row


def update_division(
    db: Session,
    tenant_id: UUID,
    division_id: UUID,
    *,
    name: str | None = None,
    code_digit: str | None = None,
    clear_digit: bool = False,
    sort_order: int | None = None,
    actor: Actor,
) -> Division:
    row = db.get(Division, division_id)
    if row is None:
        raise ConfigError(404, "That division does not exist.")
    before = _division_plain(row)
    if name is not None:
        if not name.strip():
            raise ConfigError(422, "Give the division a name.")
        row.name = name.strip()
    if clear_digit:
        row.code_digit = None
    elif code_digit is not None:
        digit = _check_digit(code_digit)
        if (
            digit
            and db.execute(
                select(Division.id).where(Division.code_digit == digit, Division.id != row.id)
            ).first()
        ):
            raise ConfigError(409, f"Another division already uses the digit {digit}.")
        row.code_digit = digit
    if sort_order is not None:
        row.sort_order = sort_order
    db.flush()
    after = _division_plain(row)
    if after != before:
        audit(
            db,
            tenant_id,
            TenantEvent.division_changed,
            "division",
            row.id,
            actor,
            before=before,
            after=after,
        )
    return row


def deactivate_division(db: Session, tenant_id: UUID, division_id: UUID, actor: Actor) -> Division:
    row = db.get(Division, division_id)
    if row is None:
        raise ConfigError(404, "That division does not exist.")
    if row.active:
        before = _division_plain(row)
        row.active = False
        db.flush()
        audit(
            db,
            tenant_id,
            TenantEvent.division_deactivated,
            "division",
            row.id,
            actor,
            before=before,
            after=_division_plain(row),
        )
    return row


# --- cost categories -----------------------------------------------------------------------------


def _category_plain(c: CostCategory) -> dict:
    return {"slot": c.slot, "name": c.name, "active": c.active, "sort_order": c.sort_order}


def rename_cost_category(
    db: Session, tenant_id: UUID, category_id: UUID, *, name: str, actor: Actor
) -> CostCategory:
    row = db.get(CostCategory, category_id)
    if row is None:
        raise ConfigError(404, "That cost category does not exist.")
    if not name.strip():
        raise ConfigError(422, "Give the cost category a name.")
    before = _category_plain(row)
    row.name = name.strip()[:60]
    db.flush()
    if _category_plain(row) != before:
        audit(
            db,
            tenant_id,
            TenantEvent.cost_category_changed,
            "cost_category",
            row.id,
            actor,
            before=before,
            after=_category_plain(row),
        )
    return row


def deactivate_cost_category(
    db: Session, tenant_id: UUID, category_id: UUID, actor: Actor
) -> CostCategory:
    row = db.get(CostCategory, category_id)
    if row is None:
        raise ConfigError(404, "That cost category does not exist.")
    if row.active:
        before = _category_plain(row)
        row.active = False
        db.flush()
        audit(
            db,
            tenant_id,
            TenantEvent.cost_category_deactivated,
            "cost_category",
            row.id,
            actor,
            before=before,
            after=_category_plain(row),
        )
    return row


# --- account map -----------------------------------------------------------------------------


def _map_plain(m: AccountMap) -> dict:
    return {
        "division_id": str(m.division_id) if m.division_id else None,
        "cost_category_id": str(m.cost_category_id) if m.cost_category_id else None,
        "in_job_cost": m.in_job_cost,
        "status": m.status,
        "suggested_by_rule": m.suggested_by_rule,
    }


def set_account_map(
    db: Session,
    tenant_id: UUID,
    gl_account_id: UUID,
    *,
    division_id: UUID | None,
    cost_category_id: UUID | None,
    in_job_cost: bool,
    confirm: bool,
    actor: Actor,
) -> AccountMap:
    """Set (and optionally confirm) one account's mapping by hand."""
    account = db.get(GlAccount, gl_account_id)
    if account is None:
        raise ConfigError(404, "That account does not exist.")
    if division_id is not None and db.get(Division, division_id) is None:
        raise ConfigError(422, "Choose a division from the list.")
    if cost_category_id is not None and db.get(CostCategory, cost_category_id) is None:
        raise ConfigError(422, "Choose a cost category from the list.")
    row = db.execute(
        select(AccountMap).where(AccountMap.gl_account_id == gl_account_id)
    ).scalar_one_or_none()
    before = None if row is None else _map_plain(row)
    if row is None:
        row = AccountMap(tenant_id=tenant_id, gl_account_id=gl_account_id, in_job_cost=in_job_cost)
        db.add(row)
    row.division_id = division_id
    row.cost_category_id = cost_category_id
    row.in_job_cost = in_job_cost
    row.suggested_by_rule = None
    if confirm:
        row.status = "confirmed"
        row.confirmed_by = actor.user_id
        row.confirmed_at = datetime.now(UTC)
    elif row.status != "confirmed":
        row.status = "suggested"
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.account_map_confirmed if confirm else TenantEvent.account_map_set,
        "account_map",
        row.id,
        actor,
        before=before,
        after=_map_plain(row),
        account_no=account.account_no,
    )
    return row


def confirm_account_map(
    db: Session, tenant_id: UUID, gl_account_id: UUID, actor: Actor
) -> AccountMap:
    row = db.execute(
        select(AccountMap).where(AccountMap.gl_account_id == gl_account_id)
    ).scalar_one_or_none()
    if row is None:
        raise ConfigError(404, "That account has no suggested mapping to confirm.")
    return _confirm(db, tenant_id, row, actor)


def _confirm(db: Session, tenant_id: UUID, row: AccountMap, actor: Actor) -> AccountMap:
    if row.status == "confirmed":
        return row
    before = _map_plain(row)
    row.status = "confirmed"
    row.confirmed_by = actor.user_id
    row.confirmed_at = datetime.now(UTC)
    db.flush()
    account = db.get(GlAccount, row.gl_account_id)
    audit(
        db,
        tenant_id,
        TenantEvent.account_map_confirmed,
        "account_map",
        row.id,
        actor,
        before=before,
        after=_map_plain(row),
        account_no=account.account_no if account else None,
    )
    return row


def confirm_all_suggestions(db: Session, tenant_id: UUID, actor: Actor) -> int:
    """One audit row per account, never a summary row."""
    rows = (
        db.execute(select(AccountMap).where(AccountMap.status == "suggested").with_for_update())
        .scalars()
        .all()
    )
    for row in rows:
        _confirm(db, tenant_id, row, actor)
    return len(rows)


def unmapped_count(db: Session) -> int:
    """Active accounts without a confirmed mapping."""
    confirmed = select(AccountMap.gl_account_id).where(AccountMap.status == "confirmed")
    return (
        db.execute(select(GlAccount.id).where(GlAccount.active, GlAccount.id.not_in(confirmed)))
        .scalars()
        .all()
        .__len__()
    )


# --- suggest rules ---------------------------------------------------------------------------


def load_suggest_rules(db: Session, tenant_id: UUID, spec: dict, actor: Actor) -> int:
    """Replace the tenant's rules with ``spec`` (the fixture JSON shape: ``divisions``
    and ``rules``). Divisions named in the spec are created when missing. Patterns are
    compiled first; one bad rule refuses the whole load, naming it."""
    ensure_cost_categories(db, tenant_id)
    divisions = {d.code: d for d in db.execute(select(Division)).scalars()}
    for i, d in enumerate(spec.get("divisions", [])):
        code = str(d.get("code", "")).strip().upper()
        if code and code not in divisions:
            divisions[code] = create_division(
                db,
                tenant_id,
                code=code,
                name=str(d.get("name") or code),
                code_digit=d.get("code_digit"),
                sort_order=i,
                actor=actor,
            )
    categories = {c.name: c for c in db.execute(select(CostCategory)).scalars()}
    new_rows: list[AccountSuggestRule] = []
    for r in spec.get("rules", []):
        name = str(r.get("name") or f"rule {r.get('order')}")
        compile_pattern(name, str(r.get("pattern") or ""))
        division = r.get("division")
        if division is not None and division not in divisions:
            raise RuleError(f"rule {name!r}: unknown division {division!r}")
        category = r.get("cost_category")
        if category is not None and category not in categories:
            raise RuleError(f"rule {name!r}: unknown cost category {category!r}")
        if division is not None and r.get("division_from_digit") is not None:
            raise RuleError(f"rule {name!r}: give a division or a digit position, not both")
        if category is not None and r.get("cost_category_from_slot"):
            raise RuleError(f"rule {name!r}: give a cost category or from-slot, not both")
        new_rows.append(
            AccountSuggestRule(
                tenant_id=tenant_id,
                sort_order=int(r["order"]),
                name=name[:100],
                pattern=str(r["pattern"]),
                division_id=divisions[division].id if division else None,
                division_from_digit=r.get("division_from_digit"),
                cost_category_id=categories[category].id if category else None,
                cost_category_from_slot=bool(r.get("cost_category_from_slot", False)),
                in_job_cost=bool(r["in_job_cost"]),
            )
        )
    old = db.execute(select(AccountSuggestRule)).scalars().all()
    before = [{"order": o.sort_order, "name": o.name, "pattern": o.pattern} for o in old]
    for o in old:
        db.delete(o)
    db.flush()
    db.add_all(new_rows)
    db.flush()
    audit(
        db,
        tenant_id,
        TenantEvent.suggest_rules_loaded,
        "account_suggest_rule",
        None,
        actor,
        before=before,
        after=[{"order": n.sort_order, "name": n.name, "pattern": n.pattern} for n in new_rows],
    )
    return len(new_rows)


# --- cost codes grid ---------------------------------------------------------------------------


def cost_code_grid(db: Session) -> list[dict]:
    """Division × cost category: the code and the confirmed-or-suggested ledger account
    mapped to it, or none. Read-only."""
    divisions = list(
        db.execute(select(Division).where(Division.active).order_by(Division.sort_order)).scalars()
    )
    categories = list(
        db.execute(
            select(CostCategory).where(CostCategory.active).order_by(CostCategory.sort_order)
        ).scalars()
    )
    accounts = {a.id: a for a in db.execute(select(GlAccount).where(GlAccount.active)).scalars()}
    by_cell: dict[tuple[UUID, UUID], list[tuple[str, str, str]]] = {}
    for m in db.execute(select(AccountMap)).scalars():
        if m.division_id and m.cost_category_id and m.gl_account_id in accounts:
            a = accounts[m.gl_account_id]
            by_cell.setdefault((m.division_id, m.cost_category_id), []).append(
                (a.account_no, a.name, m.status)
            )
    grid = []
    for d in divisions:
        cells = []
        for c in categories:
            hits = sorted(by_cell.get((d.id, c.id), []))
            cells.append(
                {
                    "cost_category_id": str(c.id),
                    "slot": c.slot,
                    "code": cost_code(d, c),
                    "accounts": [{"account_no": n, "name": nm, "status": st} for n, nm, st in hits],
                }
            )
        grid.append(
            {
                "division_id": str(d.id),
                "code": d.code,
                "name": d.name,
                "code_digit": d.code_digit,
                "cells": cells,
            }
        )
    return grid
