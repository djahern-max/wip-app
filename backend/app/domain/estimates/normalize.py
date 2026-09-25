"""The estimate normalizer (F06, D-32): raw template records → the estimate tables.

``normalize_batch`` is idempotent and re-runnable. For every estimate id the batch
touched it reads the **latest raw version** of each of the three entities (any
batch: the raw store is the source of truth, so the three CSV sheets may arrive in
any order), creates or updates the ``estimate`` from the header record by id, and
makes one ``estimate_version`` when anything of that estimate is new to the spine:
a full snapshot of the work areas and cost lines the platform now holds, built
from the raw versions it names. A re-run finds every raw version applied and
reports ``unchanged``. Nothing is ever removed because it is absent from a file.

Baseline (D-01; owner's answer of 2026-09-25 to plan question 11): the first
version with work areas received while the estimate is sold; when a later upload
marks it sold, the latest version with work areas at that moment. Versions received
while pending, lost or unknown are never the baseline.

A cost code resolves to ``(division, cost_category)`` through the F04 grid: the
first character is the division's ``code_digit``, the last two the category's
``slot``; an unknown code is stored as text with both ids NULL and reported when the
estimate is read (``EST_UNKNOWN_COST_CODE``).

``estimates.normalize`` is the task chained after ``import.process_batch`` for the
``estimate_template`` source kind (F04's ``after_load`` pattern).
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.core.db import tenant_session
from app.domain.config.audit import SYSTEM, Actor, audit
from app.domain.config.categories import ensure_cost_categories
from app.domain.config.models import CostCategory, Division
from app.domain.estimates.models import Estimate, EstimateCost, EstimateVersion, EstimateWorkArea
from app.domain.estimates.names import suggests_change_order
from app.domain.estimates.versions import WorkAreaRow, new_orders_flagged
from app.ingest.models import ImportBatch, RawRecord
from app.ingest.raw import latest_raw
from app.worker.registry import task

log = logging.getLogger("app.domain.estimates")

NORMALIZE = "estimates.normalize"
SOURCE = "template"
ENTITIES = ("estimate", "work_areas", "cost_lines")
HEADER_FIELDS = (
    "estimator",
    "client_name",
    "jobsite",
    "name",
    "status",
    "status_norm",
    "price",
    "estimate_date",
)


@dataclass
class Counts:
    created: int = 0
    updated: int = 0
    versions: int = 0
    unchanged: int = 0
    held: int = 0  # ids whose rows wait for an Estimates sheet
    issues: int = 0


@dataclass(frozen=True)
class Grid:
    divisions: dict[str, UUID]  # code_digit → division id (active)
    categories: dict[str, UUID]  # slot → cost_category id (active)

    def resolve(self, code: str) -> tuple[UUID | None, UUID | None]:
        code = code.strip()
        if len(code) < 3:
            return None, None
        return self.divisions.get(code[0]), self.categories.get(code[-2:])


def load_grid(db: Session) -> Grid:
    divisions = {
        d.code_digit: d.id
        for d in db.execute(select(Division).where(Division.active)).scalars()
        if d.code_digit
    }
    categories = {
        c.slot: c.id for c in db.execute(select(CostCategory).where(CostCategory.active)).scalars()
    }
    return Grid(divisions, categories)


def _ids_in_batch(db: Session, import_batch_id: UUID) -> list[str]:
    return sorted(
        set(
            db.execute(
                select(RawRecord.external_id).where(
                    RawRecord.import_batch_id == import_batch_id, RawRecord.source == SOURCE
                )
            ).scalars()
        )
    )


def _plain(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _header_values(payload: dict) -> dict:
    estimate_date = payload.get("estimate_date")
    return {
        "estimator": payload.get("estimator") or None,
        "client_name": payload.get("client") or None,
        "jobsite": payload.get("jobsite") or None,
        "name": str(payload.get("name") or "")[:500],
        "status": str(payload.get("status") or "")[:40],
        "status_norm": payload.get("status_norm"),
        "price": Decimal(str(payload["price"])).quantize(Decimal("0.01")),
        "estimate_date": date.fromisoformat(estimate_date) if estimate_date else None,
    }


def _latest_version(db: Session, estimate_id: UUID) -> EstimateVersion | None:
    return db.execute(
        select(EstimateVersion)
        .where(EstimateVersion.estimate_id == estimate_id)
        .order_by(EstimateVersion.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()


def _baseline(db: Session, estimate_id: UUID) -> EstimateVersion | None:
    return db.execute(
        select(EstimateVersion).where(
            EstimateVersion.estimate_id == estimate_id, EstimateVersion.is_baseline
        )
    ).scalar_one_or_none()


def _rows_of(db: Session, version_id: UUID) -> list[WorkAreaRow]:
    return [
        WorkAreaRow(w.order_no, w.name, w.kept, w.price, w.change_order_suggested)
        for w in db.execute(
            select(EstimateWorkArea)
            .where(EstimateWorkArea.estimate_version_id == version_id)
            .order_by(EstimateWorkArea.order_no)
        ).scalars()
    ]


def _issue(message: str, **detail) -> dict:
    return {"code": None, "message": message, "row_number": None, "detail": detail}


def normalize_batch(
    db: Session, tenant_id: UUID, import_batch_id: UUID, actor: Actor = SYSTEM
) -> Counts:
    batch = db.get(ImportBatch, import_batch_id)
    if batch is None:
        raise LookupError("import batch not found in this tenant")
    ensure_cost_categories(db, tenant_id)
    grid = load_grid(db)
    counts = Counts()
    issues: list[dict] = list(batch.issues or [])
    for eid in _ids_in_batch(db, import_batch_id):
        header = latest_raw(db, tenant_id, SOURCE, "estimate", eid)
        wa_raw = latest_raw(db, tenant_id, SOURCE, "work_areas", eid)
        cl_raw = latest_raw(db, tenant_id, SOURCE, "cost_lines", eid)
        est = db.execute(
            select(Estimate).where(Estimate.source == SOURCE, Estimate.external_id == eid)
        ).scalar_one_or_none()
        if est is None and header is None:
            counts.held += 1
            issues.append(
                _issue(
                    f"{eid} is not on the Estimates sheet and is not a loaded estimate yet; "
                    "its work areas and cost lines are held until an Estimates sheet with "
                    "that id is uploaded.",
                    estimate_id=eid,
                )
            )
            continue
        created = False
        changed: dict[str, list] = {}
        if est is None:
            values = _header_values(header.payload)
            est = Estimate(
                tenant_id=tenant_id,
                source=SOURCE,
                external_id=eid,
                raw_record_id=header.id,
                **values,
            )
            db.add(est)
            db.flush()
            created = True
            counts.created += 1
            audit(
                db,
                tenant_id,
                TenantEvent.estimate_created,
                "estimate",
                est.id,
                actor,
                after={k: _plain(v) for k, v in values.items()},
                external_id=eid,
            )
        elif header is not None and header.id != est.raw_record_id:
            values = _header_values(header.payload)
            for field, new in values.items():
                old = getattr(est, field)
                if old != new:
                    changed[field] = [_plain(old), _plain(new)]
                    setattr(est, field, new)
            est.raw_record_id = header.id
            db.flush()
        latest = _latest_version(db, est.id)
        wa_id = wa_raw.id if wa_raw else None
        cl_id = cl_raw.id if cl_raw else None
        new_detail = latest is None or (
            wa_id != latest.work_areas_raw_record_id or cl_id != latest.cost_lines_raw_record_id
        )
        if not (created or changed or new_detail):
            counts.unchanged += 1
            continue
        version = _make_version(
            db, tenant_id, est, batch, latest, header, wa_raw, cl_raw, grid, issues
        )
        counts.versions += 1
        if changed:
            counts.updated += 1
            audit(
                db,
                tenant_id,
                TenantEvent.estimate_updated,
                "estimate",
                est.id,
                actor,
                before={k: v[0] for k, v in changed.items()},
                after={k: v[1] for k, v in changed.items()},
                changed_fields=sorted(changed),
                version_no=version.version_no,
                external_id=eid,
            )
        audit(
            db,
            tenant_id,
            TenantEvent.estimate_version_created,
            "estimate",
            est.id,
            actor,
            after={
                "version_no": version.version_no,
                "is_baseline": version.is_baseline,
                "kept_total": _plain(version.kept_total),
            },
            external_id=eid,
        )
    counts.issues = len(issues)
    batch.issues = issues
    touched = counts.created or counts.updated or counts.versions or counts.held
    batch.followup_outcome = "applied" if touched else "unchanged"
    db.flush()
    return counts


def _make_version(
    db: Session,
    tenant_id: UUID,
    est: Estimate,
    batch: ImportBatch,
    latest: EstimateVersion | None,
    header: RawRecord | None,
    wa_raw: RawRecord | None,
    cl_raw: RawRecord | None,
    grid: Grid,
    issues: list[dict],
) -> EstimateVersion:
    baseline = _baseline(db, est.id)
    baseline_rows = _rows_of(db, baseline.id) if baseline else None
    version = EstimateVersion(
        tenant_id=tenant_id,
        estimate_id=est.id,
        version_no=1 if latest is None else latest.version_no + 1,
        import_batch_id=batch.id,
        header_raw_record_id=header.id if header else est.raw_record_id,
        work_areas_raw_record_id=wa_raw.id if wa_raw else None,
        cost_lines_raw_record_id=cl_raw.id if cl_raw else None,
        status_norm=est.status_norm,
        is_baseline=False,
    )
    db.add(version)
    db.flush()
    rows_in = (wa_raw.payload or {}).get("work_areas", []) if wa_raw else []
    rows = [
        WorkAreaRow(
            int(r["order"]),
            str(r["name"]),
            bool(r["kept"]),
            Decimal(str(r["price"])),
            suggests_change_order(str(r["name"])),
        )
        for r in rows_in
    ]
    rows = new_orders_flagged(baseline_rows, rows)
    notes = {int(r["order"]): r.get("notes") for r in rows_in}
    areas: dict[int, EstimateWorkArea] = {}
    for r in rows:
        w = EstimateWorkArea(
            tenant_id=tenant_id,
            estimate_version_id=version.id,
            order_no=r.order_no,
            name=r.name[:500],
            kept=r.kept,
            change_order_suggested=r.change_order_suggested,
            price=r.price,
            notes=(notes.get(r.order_no) or None),
        )
        db.add(w)
        areas[r.order_no] = w
    db.flush()
    orphans: list[int] = []
    for ln in (cl_raw.payload or {}).get("lines", []) if cl_raw else []:
        order = int(ln["order"])
        w = areas.get(order)
        if w is None:
            orphans.append(order)
            continue
        division_id, category_id = grid.resolve(str(ln["cost_code"]))
        hours = ln.get("hours")
        db.add(
            EstimateCost(
                tenant_id=tenant_id,
                estimate_work_area_id=w.id,
                cost_category_id=category_id,
                division_id=division_id,
                cost_code=str(ln["cost_code"])[:20],
                hours=None if hours is None else Decimal(str(hours)),
                amount=Decimal(str(ln["amount"])),
                notes=(ln.get("notes") or None),
            )
        )
    db.flush()
    if orphans:
        distinct = sorted(set(orphans))
        listed = ", ".join(f"#{o}" for o in distinct)
        plural = "s" if len(distinct) != 1 else ""
        issues.append(
            _issue(
                f"{est.external_id}: cost lines for work area{plural} {listed} were held "
                "because the platform has no such work area; upload the Work areas sheet "
                "and the lines will attach.",
                estimate_id=est.external_id,
                orders=sorted(set(orphans)),
            )
        )
    if rows:
        version.kept_total = sum((r.price for r in rows if r.kept), Decimal("0.00"))
    # D-01 baseline (owner's answer to plan question 11).
    if est.status_norm == "sold" and baseline is None and rows:
        if wa_raw is not None and (latest is None or wa_raw.id != latest.work_areas_raw_record_id):
            version.is_baseline = True
        else:
            previous = _latest_with_work_areas(db, est.id, before=version.version_no)
            (previous or version).is_baseline = True
    db.flush()
    return version


def _latest_with_work_areas(
    db: Session, estimate_id: UUID, *, before: int
) -> EstimateVersion | None:
    return db.execute(
        select(EstimateVersion)
        .where(
            EstimateVersion.estimate_id == estimate_id,
            EstimateVersion.version_no < before,
            EstimateVersion.work_areas_raw_record_id.is_not(None),
        )
        .order_by(EstimateVersion.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()


@task(NORMALIZE)
def normalize_task(tenant_id: UUID, *, engine: Engine, import_batch_id: str) -> None:
    batch_id = UUID(import_batch_id)
    with tenant_session(engine, tenant_id) as s:
        counts = normalize_batch(s, tenant_id, batch_id)
    log.info(
        "tenant=%s batch=%s estimates normalized: created=%d updated=%d versions=%d "
        "unchanged=%d held=%d issues=%d",
        tenant_id,
        batch_id,
        counts.created,
        counts.updated,
        counts.versions,
        counts.unchanged,
        counts.held,
        counts.issues,
    )
