"""Tenant configuration routes (F04). Read: firm roles and client_admin. Write: firm
roles. Accounting-policy keys: firm_admin only. Every write is audited by the
service in the request transaction. Messages are sentences a person can act on
(D-22); machine identifiers travel in their own fields."""

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.schemas import (
    AccountMapOut,
    AccountsOut,
    BurdenRateOut,
    ConfirmAllOut,
    CostCategoryOut,
    CostCodeCellOut,
    CostCodeRowOut,
    CostCodesOut,
    DivisionOut,
    GlAccountOut,
    PolicyOut,
    SuggestRuleOut,
)
from app.core.audit import request_meta
from app.core.auth import Principal, TenantSession
from app.core.authz import can_manage_tenant_config, can_set_policy, can_view_tenant_config
from app.domain.config import burden, policy, service
from app.domain.config.audit import Actor
from app.domain.config.categories import cost_code, ensure_cost_categories
from app.domain.config.chart import suggest
from app.domain.config.models import (
    AccountMap,
    AccountSuggestRule,
    BurdenRate,
    CostCategory,
    Division,
    GlAccount,
    TenantPolicy,
)
from app.domain.config.rules import RuleError
from app.tenancy.models import User

log = logging.getLogger("app.config")

router = APIRouter(prefix="/config", tags=["config"])

Viewer = Annotated[Principal, Depends(can_view_tenant_config)]
Manager = Annotated[Principal, Depends(can_manage_tenant_config)]
PolicySetter = Annotated[Principal, Depends(can_set_policy)]

MAP_STATUS_LABELS = {"suggested": "Suggested", "confirmed": "Confirmed"}


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user.id, role=p.role, meta=request_meta(request))


def _raise(exc: service.ConfigError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- accounts and the map ---------------------------------------------------------------------


def _division_out(d: Division) -> DivisionOut:
    return DivisionOut(
        id=str(d.id),
        code=d.code,
        name=d.name,
        code_digit=d.code_digit,
        active=d.active,
        sort_order=d.sort_order,
    )


def _category_out(c: CostCategory) -> CostCategoryOut:
    return CostCategoryOut(
        id=str(c.id), slot=c.slot, name=c.name, active=c.active, sort_order=c.sort_order
    )


def _emails(db: TenantSession, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    return dict(db.execute(select(User.id, User.email).where(User.id.in_(ids))).all())


@router.get("/accounts", response_model=AccountsOut)
def accounts(
    _v: Viewer,
    db: TenantSession,
    filter: Annotated[str, Query(pattern="^(all|unmapped|suggested|confirmed)$")] = "all",
):
    ensure_cost_categories(db, _v.active_tenant_id)
    divisions = {d.id: d for d in db.execute(select(Division)).scalars()}
    categories = {c.id: c for c in db.execute(select(CostCategory)).scalars()}
    maps = {m.gl_account_id: m for m in db.execute(select(AccountMap)).scalars()}
    emails = _emails(db, {m.confirmed_by for m in maps.values() if m.confirmed_by})
    rows = list(
        db.execute(
            select(GlAccount).where(GlAccount.active).order_by(GlAccount.account_no)
        ).scalars()
    )
    out: list[GlAccountOut] = []
    counts = {"unmapped": 0, "suggested": 0, "confirmed": 0}
    for a in rows:
        m = maps.get(a.id)
        state = "unmapped" if m is None else m.status
        if m is None or m.status != "confirmed":
            counts["unmapped"] += 1
        if m is not None:
            counts[m.status] += 1
        if (
            filter != "all"
            and state != filter
            and not (filter == "unmapped" and state == "suggested")
        ):
            continue
        d = divisions.get(m.division_id) if m and m.division_id else None
        c = categories.get(m.cost_category_id) if m and m.cost_category_id else None
        out.append(
            GlAccountOut(
                id=str(a.id),
                account_no=a.account_no,
                name=a.name,
                ledger_type=a.ledger_type,
                active=a.active,
                cost_code=cost_code(d, c) if m and m.in_job_cost else None,
                map=None
                if m is None
                else AccountMapOut(
                    division_id=str(m.division_id) if m.division_id else None,
                    division_code=d.code if d else None,
                    cost_category_id=str(m.cost_category_id) if m.cost_category_id else None,
                    cost_category_name=c.name if c else None,
                    in_job_cost=m.in_job_cost,
                    status=m.status,
                    status_label=MAP_STATUS_LABELS[m.status],
                    suggested_by_rule=m.suggested_by_rule,
                    confirmed_by_email=emails.get(m.confirmed_by) if m.confirmed_by else None,
                    confirmed_at=m.confirmed_at.isoformat() if m.confirmed_at else None,
                ),
                map_status_label="Unmapped" if m is None else MAP_STATUS_LABELS[m.status],
            )
        )
    return AccountsOut(
        total_active=len(rows),
        unmapped_count=counts["unmapped"],
        suggested_count=counts["suggested"],
        confirmed_count=counts["confirmed"],
        accounts=out,
    )


class MapIn(_In):
    division_id: str | None = None
    cost_category_id: str | None = None
    in_job_cost: bool
    confirm: bool = False


def _uuid(value: str | None, what: str) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Choose a {what} from the list.") from None


@router.put("/accounts/{gl_account_id}/map", response_model=AccountsOut)
def set_map(request: Request, m: Manager, db: TenantSession, gl_account_id: UUID, body: MapIn):
    try:
        service.set_account_map(
            db,
            m.active_tenant_id,
            gl_account_id,
            division_id=_uuid(body.division_id, "division"),
            cost_category_id=_uuid(body.cost_category_id, "cost category"),
            in_job_cost=body.in_job_cost,
            confirm=body.confirm,
            actor=_actor(m, request),
        )
    except service.ConfigError as exc:
        _raise(exc)
    return accounts(m, db, "all")


@router.post("/accounts/{gl_account_id}/confirm", response_model=AccountsOut)
def confirm_one(request: Request, m: Manager, db: TenantSession, gl_account_id: UUID):
    try:
        service.confirm_account_map(db, m.active_tenant_id, gl_account_id, _actor(m, request))
    except service.ConfigError as exc:
        _raise(exc)
    return accounts(m, db, "all")


@router.post("/accounts/confirm-all", response_model=ConfirmAllOut)
def confirm_all(request: Request, m: Manager, db: TenantSession):
    n = service.confirm_all_suggestions(db, m.active_tenant_id, _actor(m, request))
    return ConfirmAllOut(confirmed=n, unmapped_count=service.unmapped_count(db))


@router.post("/accounts/suggest", response_model=AccountsOut)
def run_suggestions(request: Request, m: Manager, db: TenantSession):
    """Re-run the suggestion step by hand (idempotent; never touches confirmed rows)."""
    try:
        suggest(db, m.active_tenant_id, _actor(m, request))
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return accounts(m, db, "all")


# --- divisions --------------------------------------------------------------------------------


@router.get("/divisions", response_model=list[DivisionOut])
def divisions(_v: Viewer, db: TenantSession):
    rows = db.execute(select(Division).order_by(Division.sort_order, Division.code)).scalars()
    return [_division_out(d) for d in rows]


class DivisionIn(_In):
    code: str
    name: str
    code_digit: str | None = None
    sort_order: int = 0


@router.post("/divisions", response_model=DivisionOut, status_code=201)
def create_division(request: Request, m: Manager, db: TenantSession, body: DivisionIn):
    try:
        row = service.create_division(
            db,
            m.active_tenant_id,
            code=body.code,
            name=body.name,
            code_digit=body.code_digit,
            sort_order=body.sort_order,
            actor=_actor(m, request),
        )
    except service.ConfigError as exc:
        _raise(exc)
    return _division_out(row)


class DivisionUpdateIn(_In):
    name: str | None = None
    code_digit: str | None = None
    clear_digit: bool = False
    sort_order: int | None = None


@router.put("/divisions/{division_id}", response_model=DivisionOut)
def update_division(
    request: Request, m: Manager, db: TenantSession, division_id: UUID, body: DivisionUpdateIn
):
    try:
        row = service.update_division(
            db,
            m.active_tenant_id,
            division_id,
            name=body.name,
            code_digit=body.code_digit,
            clear_digit=body.clear_digit,
            sort_order=body.sort_order,
            actor=_actor(m, request),
        )
    except service.ConfigError as exc:
        _raise(exc)
    return _division_out(row)


@router.post("/divisions/{division_id}/deactivate", response_model=DivisionOut)
def deactivate_division(request: Request, m: Manager, db: TenantSession, division_id: UUID):
    try:
        row = service.deactivate_division(db, m.active_tenant_id, division_id, _actor(m, request))
    except service.ConfigError as exc:
        _raise(exc)
    return _division_out(row)


# --- cost categories and cost codes --------------------------------------------------------------


@router.get("/cost-categories", response_model=list[CostCategoryOut])
def cost_categories(v: Viewer, db: TenantSession):
    ensure_cost_categories(db, v.active_tenant_id)
    rows = db.execute(select(CostCategory).order_by(CostCategory.sort_order)).scalars()
    return [_category_out(c) for c in rows]


class CategoryIn(_In):
    name: str


@router.put("/cost-categories/{category_id}", response_model=CostCategoryOut)
def rename_category(
    request: Request, m: Manager, db: TenantSession, category_id: UUID, body: CategoryIn
):
    try:
        row = service.rename_cost_category(
            db, m.active_tenant_id, category_id, name=body.name, actor=_actor(m, request)
        )
    except service.ConfigError as exc:
        _raise(exc)
    return _category_out(row)


@router.post("/cost-categories/{category_id}/deactivate", response_model=CostCategoryOut)
def deactivate_category(request: Request, m: Manager, db: TenantSession, category_id: UUID):
    try:
        row = service.deactivate_cost_category(
            db, m.active_tenant_id, category_id, _actor(m, request)
        )
    except service.ConfigError as exc:
        _raise(exc)
    return _category_out(row)


@router.get("/cost-codes", response_model=CostCodesOut)
def cost_codes(v: Viewer, db: TenantSession):
    ensure_cost_categories(db, v.active_tenant_id)
    cats = list(
        db.execute(
            select(CostCategory).where(CostCategory.active).order_by(CostCategory.sort_order)
        ).scalars()
    )
    grid = service.cost_code_grid(db)
    return CostCodesOut(
        categories=[_category_out(c) for c in cats],
        rows=[
            CostCodeRowOut(
                division_id=r["division_id"],
                code=r["code"],
                name=r["name"],
                code_digit=r["code_digit"],
                cells=[CostCodeCellOut(**c) for c in r["cells"]],
            )
            for r in grid
        ],
    )


# --- burden rates ----------------------------------------------------------------------------


def _rate_out(r: BurdenRate, divisions: dict[UUID, Division]) -> BurdenRateOut:
    d = divisions.get(r.division_id) if r.division_id else None
    return BurdenRateOut(
        id=str(r.id),
        division_id=str(r.division_id) if r.division_id else None,
        division_code=d.code if d else None,
        effective_from=r.effective_from.isoformat(),
        effective_to=r.effective_to.isoformat() if r.effective_to else None,
        rate=str(r.rate),
        basis_note=r.basis_note,
        active=r.active,
    )


@router.get("/burden-rates", response_model=list[BurdenRateOut])
def burden_rates(_v: Viewer, db: TenantSession):
    divisions = {d.id: d for d in db.execute(select(Division)).scalars()}
    rows = db.execute(
        select(BurdenRate).order_by(BurdenRate.effective_from.desc(), BurdenRate.id)
    ).scalars()
    return [_rate_out(r, divisions) for r in rows]


class BurdenRateIn(_In):
    division_id: str | None = None
    effective_from: date
    effective_to: date | None = None
    rate: str  # a fraction as a string, e.g. "0.3250"
    basis_note: str | None = None


@router.post("/burden-rates", response_model=BurdenRateOut, status_code=201)
def add_burden_rate(request: Request, m: Manager, db: TenantSession, body: BurdenRateIn):
    try:
        rate = Decimal(body.rate)
    except InvalidOperation:
        raise HTTPException(
            status_code=422, detail="Enter the rate as a decimal fraction, such as 0.3250."
        ) from None
    try:
        row = burden.add_burden_rate(
            db,
            m.active_tenant_id,
            division_id=_uuid(body.division_id, "division"),
            effective_from=body.effective_from,
            effective_to=body.effective_to,
            rate=rate,
            basis_note=body.basis_note,
            actor=_actor(m, request),
        )
    except burden.BurdenRateError as exc:
        raise HTTPException(status_code=422, detail=f"{exc}.".replace("..", ".")) from None
    divisions = {d.id: d for d in db.execute(select(Division)).scalars()}
    return _rate_out(row, divisions)


@router.post("/burden-rates/{rate_id}/deactivate", response_model=BurdenRateOut)
def deactivate_burden_rate(request: Request, m: Manager, db: TenantSession, rate_id: UUID):
    try:
        row = burden.deactivate_burden_rate(db, m.active_tenant_id, rate_id, _actor(m, request))
    except LookupError:
        raise HTTPException(status_code=404, detail="That burden rate does not exist.") from None
    divisions = {d.id: d for d in db.execute(select(Division)).scalars()}
    return _rate_out(row, divisions)


# --- policy -----------------------------------------------------------------------------------


def _policy_out(spec: policy.PolicyKey, row: TenantPolicy | None, email: str | None) -> PolicyOut:
    return PolicyOut(
        key=spec.key,
        label=spec.label,
        kind=spec.kind,
        description=spec.description,
        decided=row is not None,
        value=policy._plain(row.value) if row is not None else None,
        decided_by_email=email,
        decided_at=row.decided_at.isoformat() if row is not None else None,
        decision_ref=row.decision_ref if row is not None else None,
    )


@router.get("/policy", response_model=list[PolicyOut])
def policies(_v: Viewer, db: TenantSession):
    rows = {r.key: r for r in db.execute(select(TenantPolicy)).scalars()}
    emails = _emails(db, {r.decided_by for r in rows.values() if r.decided_by})
    return [
        _policy_out(spec, rows.get(key), emails.get(rows[key].decided_by) if key in rows else None)
        for key, spec in policy.POLICY_KEYS.items()
    ]


class PolicyIn(_In):
    value: object
    decision_ref: str


@router.put("/policy/{key}", response_model=PolicyOut)
def set_policy(request: Request, p: PolicySetter, db: TenantSession, key: str, body: PolicyIn):
    if key not in policy.POLICY_KEYS:
        raise HTTPException(status_code=404, detail="That policy key does not exist.")
    try:
        row = policy.set_policy(
            db,
            p.active_tenant_id,
            key,
            body.value,
            decision_ref=body.decision_ref,
            actor=_actor(p, request),
        )
    except policy.PolicyValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{policy.POLICY_KEYS[key].label} needs {exc}."
        ) from None
    return _policy_out(policy.POLICY_KEYS[key], row, p.user.email)


# --- suggest rules ------------------------------------------------------------------------------


@router.get("/suggest-rules", response_model=list[SuggestRuleOut])
def suggest_rules(_v: Viewer, db: TenantSession):
    divisions = {d.id: d for d in db.execute(select(Division)).scalars()}
    categories = {c.id: c for c in db.execute(select(CostCategory)).scalars()}
    rows = db.execute(select(AccountSuggestRule).order_by(AccountSuggestRule.sort_order)).scalars()
    return [
        SuggestRuleOut(
            id=str(r.id),
            sort_order=r.sort_order,
            name=r.name,
            pattern=r.pattern,
            division_code=divisions[r.division_id].code if r.division_id else None,
            division_from_digit=r.division_from_digit,
            cost_category_name=categories[r.cost_category_id].name if r.cost_category_id else None,
            cost_category_from_slot=r.cost_category_from_slot,
            in_job_cost=r.in_job_cost,
            active=r.active,
        )
        for r in rows
    ]


class RulesIn(_In):
    divisions: list[dict] = []
    rules: list[dict]


@router.put("/suggest-rules", response_model=list[SuggestRuleOut])
def load_rules(request: Request, m: Manager, db: TenantSession, body: RulesIn):
    try:
        service.load_suggest_rules(db, m.active_tenant_id, body.model_dump(), _actor(m, request))
    except (RuleError, service.ConfigError, KeyError, ValueError) as exc:
        detail = (
            exc.detail
            if isinstance(exc, service.ConfigError)
            else f"The rules could not be loaded: {exc}."
        )
        raise HTTPException(status_code=422, detail=detail) from None
    return suggest_rules(m, db)
