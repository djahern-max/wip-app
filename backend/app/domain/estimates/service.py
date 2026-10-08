"""Reads for the Estimates API (F06). Loads an estimate's latest version with its
work areas and cost lines, the baseline rows, the tenant's categories and WIP basis,
and hands the pure modules (``totals``, ``exceptions``) what they need. No writes.
Every function requires ``app.tenant_id`` on the session (RLS)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.config.burden import active_burden_rates
from app.domain.config.categories import ensure_cost_categories
from app.domain.config.models import BurdenRate, CostCategory, Division
from app.domain.config.policy import TIMEZONE, WIP_BASIS, get_policy
from app.domain.estimates.burden import BurdenResult, compute_burden
from app.domain.estimates.exceptions import EstimateState, Issue, burden_issues, issues_for
from app.domain.estimates.models import (
    STATUS_LABELS,
    Estimate,
    EstimateCost,
    EstimateVersion,
    EstimateWorkArea,
)
from app.domain.estimates.totals import CostLineIn, Totals, WorkAreaIn, compute
from app.domain.estimates.versions import WorkAreaRow
from app.ingest.models import ImportBatch

CENT = Decimal("0.01")
IN_BASIS_LABELS = {"yes": "Yes", "no": "No", "not_decided": "Not decided"}


def money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(CENT), "f")


def status_label(est: Estimate) -> str:
    return STATUS_LABELS.get(est.status_norm or "", est.status)


@dataclass(frozen=True)
class Grid:
    categories: tuple[tuple[str, str], ...]  # (slot, name) in slot order
    category_by_id: dict[UUID, tuple[str, str]]
    division_code_by_id: dict[UUID, str]
    basis: frozenset[str] | None  # None: wip_basis not decided


def load_grid(db: Session, tenant_id: UUID) -> Grid:
    ensure_cost_categories(db, tenant_id)
    cats = list(
        db.execute(
            select(CostCategory).where(CostCategory.active).order_by(CostCategory.sort_order)
        ).scalars()
    )
    divisions = {d.id: d.code for d in db.execute(select(Division)).scalars()}
    row = get_policy(db, WIP_BASIS)
    basis = frozenset(str(s) for s in row.value) if row is not None else None
    return Grid(
        categories=tuple((c.slot, c.name) for c in cats),
        category_by_id={c.id: (c.slot, c.name) for c in cats},
        division_code_by_id=divisions,
        basis=basis,
    )


@dataclass(frozen=True)
class LineView:
    line: EstimateCost
    slot: str | None
    category_name: str | None
    division_code: str | None


@dataclass(frozen=True)
class WorkAreaView:
    row: EstimateWorkArea
    lines: tuple[LineView, ...]

    def as_in(self) -> WorkAreaIn:
        return WorkAreaIn(
            self.row.order_no,
            self.row.name,
            self.row.kept,
            self.row.price,
            self.row.change_order_suggested,
            tuple(
                CostLineIn(
                    lv.line.cost_code,
                    lv.slot,
                    lv.line.hours,
                    lv.line.amount,
                    lv.line.division_id if lv.division_code else None,
                    lv.division_code,
                )
                for lv in self.lines
            ),
        )


@dataclass(frozen=True)
class EstimateView:
    estimate: Estimate
    latest: EstimateVersion | None
    work_areas: tuple[WorkAreaView, ...] | None  # None: no version with work areas
    baseline: tuple[WorkAreaRow, ...] | None
    latest_is_baseline: bool
    version_count: int
    areas_version: EstimateVersion | None = None  # the version whose work areas are shown

    def state(self) -> EstimateState:
        return EstimateState(
            external_id=self.estimate.external_id,
            status=self.estimate.status,
            status_norm=self.estimate.status_norm,
            price=self.estimate.price,
            work_areas=None
            if self.work_areas is None
            else tuple(w.as_in() for w in self.work_areas),
            baseline=self.baseline,
            latest_is_baseline=self.latest_is_baseline,
        )

    def issues(self) -> list[Issue]:
        return issues_for(self.state())

    def totals(self, grid: Grid) -> Totals:
        return compute(tuple(w.as_in() for w in self.work_areas or ()), grid.categories, grid.basis)


def _work_areas_for(
    db: Session, version_ids: Sequence[UUID], grid: Grid
) -> dict[UUID, list[WorkAreaView]]:
    if not version_ids:
        return {}
    areas = list(
        db.execute(
            select(EstimateWorkArea)
            .where(EstimateWorkArea.estimate_version_id.in_(version_ids))
            .order_by(EstimateWorkArea.order_no)
        ).scalars()
    )
    lines_by_area: dict[UUID, list[LineView]] = {}
    if areas:
        for ln in db.execute(
            select(EstimateCost)
            .where(EstimateCost.estimate_work_area_id.in_([a.id for a in areas]))
            .order_by(EstimateCost.cost_code)
        ).scalars():
            slot, name = grid.category_by_id.get(ln.cost_category_id, (None, None))
            lines_by_area.setdefault(ln.estimate_work_area_id, []).append(
                LineView(
                    ln,
                    slot,
                    name,
                    grid.division_code_by_id.get(ln.division_id) if ln.division_id else None,
                )
            )
    out: dict[UUID, list[WorkAreaView]] = {}
    for a in areas:
        out.setdefault(a.estimate_version_id, []).append(
            WorkAreaView(a, tuple(lines_by_area.get(a.id, ())))
        )
    return out


def load_views(db: Session, estimates: Sequence[Estimate], grid: Grid) -> list[EstimateView]:
    if not estimates:
        return []
    ids = [e.id for e in estimates]
    versions = list(
        db.execute(
            select(EstimateVersion)
            .where(EstimateVersion.estimate_id.in_(ids))
            .order_by(EstimateVersion.estimate_id, EstimateVersion.version_no)
        ).scalars()
    )
    by_est: dict[UUID, list[EstimateVersion]] = {}
    for v in versions:
        by_est.setdefault(v.estimate_id, []).append(v)
    latest_with_areas: dict[UUID, EstimateVersion] = {}
    baselines: dict[UUID, EstimateVersion] = {}
    for eid, vs in by_est.items():
        for v in vs:
            if v.work_areas_raw_record_id is not None:
                latest_with_areas[eid] = v
            if v.is_baseline:
                baselines[eid] = v
    wanted = {v.id for v in latest_with_areas.values()} | {v.id for v in baselines.values()}
    areas = _work_areas_for(db, sorted(wanted, key=str), grid)
    views: list[EstimateView] = []
    for e in estimates:
        vs = by_est.get(e.id, [])
        latest = vs[-1] if vs else None
        with_areas = latest_with_areas.get(e.id)
        baseline = baselines.get(e.id)
        baseline_rows = None
        if baseline is not None:
            baseline_rows = tuple(
                WorkAreaRow(
                    w.row.order_no,
                    w.row.name,
                    w.row.kept,
                    w.row.price,
                    w.row.change_order_suggested,
                )
                for w in areas.get(baseline.id, [])
            )
        views.append(
            EstimateView(
                estimate=e,
                latest=latest,
                work_areas=None if with_areas is None else tuple(areas.get(with_areas.id, [])),
                baseline=baseline_rows,
                latest_is_baseline=bool(with_areas and baseline and with_areas.id == baseline.id),
                version_count=len(vs),
                areas_version=with_areas,
            )
        )
    return views


def list_estimates(
    db: Session, tenant_id: UUID, *, status: str | None = None, estimator: str | None = None
) -> tuple[list[EstimateView], list[str], Grid]:
    grid = load_grid(db, tenant_id)
    stmt = select(Estimate).order_by(Estimate.external_id)
    if status == "unknown":
        stmt = stmt.where(Estimate.status_norm.is_(None))
    elif status:
        stmt = stmt.where(Estimate.status_norm == status)
    if estimator:
        stmt = stmt.where(Estimate.estimator == estimator)
    estimates = list(db.execute(stmt).scalars())
    estimators = list(
        db.execute(
            select(Estimate.estimator)
            .where(Estimate.estimator.is_not(None))
            .group_by(Estimate.estimator)
            .order_by(func.lower(Estimate.estimator))
        ).scalars()
    )
    return load_views(db, estimates, grid), estimators, grid


@dataclass(frozen=True)
class VersionView:
    version: EstimateVersion
    original_filename: str | None


def estimate_detail(
    db: Session, tenant_id: UUID, estimate_id: UUID
) -> tuple[EstimateView, list[VersionView], Grid] | None:
    est = db.get(Estimate, estimate_id)
    if est is None:
        return None
    grid = load_grid(db, tenant_id)
    view = load_views(db, [est], grid)[0]
    versions = list(
        db.execute(
            select(EstimateVersion, ImportBatch.original_filename)
            .join(ImportBatch, ImportBatch.id == EstimateVersion.import_batch_id)
            .where(EstimateVersion.estimate_id == est.id)
            .order_by(EstimateVersion.version_no)
        ).all()
    )
    return view, [VersionView(v, name) for v, name in versions], grid


# --- labor burden (F06.1; D-05, D-34): computed on read, never written ----------------


@dataclass(frozen=True)
class BurdenView:
    result: BurdenResult
    day_source: str  # estimate_date | received | none
    issues: list[Issue]


@dataclass(frozen=True)
class BurdenInputs:
    """What ``estimate_burden`` reads of the tenant: the active rates and the time zone.
    F09: read once per request (``load_burden_inputs``) when many estimates are priced,
    so the Estimates list carries the burden sentences at two reads, not two per row."""

    rates: tuple[BurdenRate, ...]
    timezone: str | None


def load_burden_inputs(db: Session) -> BurdenInputs:
    tz = get_policy(db, TIMEZONE)
    return BurdenInputs(tuple(active_burden_rates(db)), None if tz is None else str(tz.value))


def pricing_day(
    db: Session, view: EstimateView, inputs: BurdenInputs | None = None
) -> tuple[date | None, str]:
    """The date the burden rate is read at: the estimate date; when it is blank, the
    received date of the version whose work areas are shown, in the tenant's time zone
    (owner's answer 1). No time zone set, or no such version: no date."""
    if view.estimate.estimate_date is not None:
        return view.estimate.estimate_date, "estimate_date"
    if view.areas_version is None:
        return None, "none"
    tz = inputs.timezone if inputs is not None else _timezone(db)
    if tz is None:
        return None, "none"
    return view.areas_version.received_at.astimezone(ZoneInfo(tz)).date(), "received"


def _timezone(db: Session) -> str | None:
    tz = get_policy(db, TIMEZONE)
    return None if tz is None else str(tz.value)


def estimate_burden(
    db: Session, view: EstimateView, grid: Grid, inputs: BurdenInputs | None = None
) -> BurdenView:
    """Reads the active burden rates and the time zone (or takes them in ``inputs``);
    writes nothing."""
    if inputs is None:
        inputs = load_burden_inputs(db)
    day, source = pricing_day(db, view, inputs)
    result = compute_burden(tuple(w.as_in() for w in view.work_areas or ()), inputs.rates, day)
    return BurdenView(result, source, burden_issues(result, grid.basis))
