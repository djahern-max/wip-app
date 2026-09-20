"""The chart-of-accounts normalizer and the suggestion step (F04).

``normalize_chart`` is idempotent and re-runnable: it turns the latest raw record
per account in a batch into ``gl_account`` rows. New accounts are inserted, a
renamed account is updated with the old name kept in the audit detail, and an
active account missing from a **newer** chart is marked inactive, never deleted.
Only the newest chart batch of the tenant (by ``uploaded_at``) is applied: an older
batch processed late (a retry that waited out its backoff, a requeue) changes
nothing, and its follow-up says so (``followup_outcome = superseded``).
Which accounts the newer chart holds comes from the file itself (``present``: the
task re-reads the stored object), not from ``raw_record``: an unchanged account
writes no raw row in a later batch (D-20), so raw rows alone would read every
unchanged account as removed.
``suggest`` writes ``account_map`` rows with ``status = suggested`` for active
accounts that match a rule and have no confirmed mapping; it never touches a
confirmed row, and it removes a stale suggestion for an account no rule matches
any more (owner amendment A: no match = unmapped).

``config.normalize_chart`` is the task chained after ``import.process_batch``
(plan call 3): normalize, then suggest, each in its own ``tenant_session``.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.audit import TenantEvent
from app.core.db import tenant_session
from app.core.storage import ObjectStore
from app.domain.config.audit import SYSTEM, Actor, audit
from app.domain.config.categories import ensure_cost_categories
from app.domain.config.models import (
    AccountMap,
    AccountSuggestRule,
    CostCategory,
    Division,
    GlAccount,
)
from app.domain.config.rules import compile_rules, suggest_for
from app.ingest.models import ImportBatch, RawRecord
from app.worker.registry import task

log = logging.getLogger("app.domain.config")

NORMALIZE_CHART = "config.normalize_chart"
CHART_SOURCE = "chart"


@dataclass
class ChartCounts:
    added: int = 0
    renamed: int = 0
    deactivated: int = 0
    unchanged: int = 0
    superseded: bool = False  # a newer chart batch exists: nothing was applied


def _latest_in_batch(db: Session, import_batch_id: UUID) -> list[RawRecord]:
    """The highest version per external_id among this batch's records."""
    rows = db.execute(
        select(RawRecord)
        .where(RawRecord.import_batch_id == import_batch_id, RawRecord.source == CHART_SOURCE)
        .order_by(RawRecord.external_id, RawRecord.version.desc())
    ).scalars()
    latest: dict[str, RawRecord] = {}
    for r in rows:
        latest.setdefault(r.external_id, r)
    return list(latest.values())


def _is_newest_chart_batch(db: Session, batch: ImportBatch) -> bool:
    """Newest by ``uploaded_at`` among the batches of this kind that hold a chart or
    are being read. A batch that is ``failed`` or ``nothing_loaded`` holds no chart
    and supersedes nothing; one still ``received`` will be applied after this one."""
    newest = db.execute(
        select(func.max(ImportBatch.uploaded_at)).where(
            ImportBatch.source_kind == batch.source_kind,
            ImportBatch.status.in_(("loaded", "loaded_with_issues", "processing")),
        )
    ).scalar_one()
    return newest is None or batch.uploaded_at >= newest


def normalize_chart(
    db: Session,
    tenant_id: UUID,
    import_batch_id: UUID,
    actor: Actor = SYSTEM,
    *,
    present: set[str] | None = None,
) -> ChartCounts:
    """``present``: every account number in the batch's file. ``None`` = unknown, so
    nothing is deactivated; an empty set is a file nobody could read, not a chart
    from which every account was removed, so nothing is deactivated either."""
    batch = db.get(ImportBatch, import_batch_id)
    if batch is None:
        raise LookupError("import batch not found in this tenant")
    if not _is_newest_chart_batch(db, batch):
        batch.followup_outcome = "superseded"
        return ChartCounts(superseded=True)
    batch.followup_outcome = "applied"
    counts = ChartCounts()
    existing = {a.account_no: a for a in db.execute(select(GlAccount)).scalars()}
    seen: set[str] = set()
    for raw in _latest_in_batch(db, import_batch_id):
        p = raw.payload if isinstance(raw.payload, dict) else {}
        account_no = str(p.get("account_no") or raw.external_id).strip()
        name = str(p.get("account_name") or "").strip()[:200]
        ledger_type = str(p.get("ledger_type") or "").strip()[:60]
        seen.add(account_no)
        row = existing.get(account_no)
        if row is None:
            row = GlAccount(
                tenant_id=tenant_id,
                account_no=account_no,
                name=name,
                ledger_type=ledger_type,
                active=True,
                source="file",
                raw_record_id=raw.id,
            )
            db.add(row)
            db.flush()
            existing[account_no] = row
            counts.added += 1
            audit(
                db,
                tenant_id,
                TenantEvent.gl_account_added,
                "gl_account",
                row.id,
                actor,
                after={"account_no": account_no, "name": name, "ledger_type": ledger_type},
            )
            continue
        before = {"name": row.name, "ledger_type": row.ledger_type, "active": row.active}
        changed = row.name != name or row.ledger_type != ledger_type or not row.active
        if changed:
            row.name, row.ledger_type, row.active, row.raw_record_id = (
                name,
                ledger_type,
                True,
                raw.id,
            )
            db.flush()
            counts.renamed += 1
            audit(
                db,
                tenant_id,
                TenantEvent.gl_account_renamed,
                "gl_account",
                row.id,
                actor,
                before=before,
                after={"name": name, "ledger_type": ledger_type, "active": True},
            )
        else:
            row.raw_record_id = raw.id
            counts.unchanged += 1
    if present:
        for account_no, row in existing.items():
            if row.active and account_no not in present and account_no not in seen:
                row.active = False
                db.flush()
                counts.deactivated += 1
                audit(
                    db,
                    tenant_id,
                    TenantEvent.gl_account_deactivated,
                    "gl_account",
                    row.id,
                    actor,
                    before={"active": True},
                    after={"active": False},
                    reason="missing from the newest chart",
                )
    return counts


@dataclass
class SuggestCounts:
    suggested: int = 0
    unchanged: int = 0
    removed: int = 0
    unmatched: int = 0


def suggest(db: Session, tenant_id: UUID, actor: Actor = SYSTEM) -> SuggestCounts:
    """Idempotent. Never overwrites a confirmed mapping."""
    ensure_cost_categories(db, tenant_id)
    rules = compile_rules(list(db.execute(select(AccountSuggestRule)).scalars()))
    divisions_by_digit = {
        d.code_digit: d.id
        for d in db.execute(select(Division).where(Division.active)).scalars()
        if d.code_digit
    }
    categories_by_slot = {
        c.slot: c.id for c in db.execute(select(CostCategory).where(CostCategory.active)).scalars()
    }
    maps = {m.gl_account_id: m for m in db.execute(select(AccountMap)).scalars()}
    counts = SuggestCounts()
    for account in db.execute(select(GlAccount).where(GlAccount.active)).scalars():
        current = maps.get(account.id)
        if current is not None and current.status == "confirmed":
            continue
        s = suggest_for(account.account_no, rules, divisions_by_digit, categories_by_slot)
        if s is None:
            counts.unmatched += 1
            if current is not None:
                audit(
                    db,
                    tenant_id,
                    TenantEvent.account_map_suggestion_removed,
                    "account_map",
                    current.id,
                    actor,
                    before=_map_plain(current),
                    account_no=account.account_no,
                )
                db.delete(current)
                db.flush()
                counts.removed += 1
            continue
        if current is None:
            current = AccountMap(
                tenant_id=tenant_id,
                gl_account_id=account.id,
                division_id=s.division_id,
                cost_category_id=s.cost_category_id,
                in_job_cost=s.in_job_cost,
                status="suggested",
                suggested_by_rule=s.rule_name,
            )
            db.add(current)
            db.flush()
            counts.suggested += 1
            audit(
                db,
                tenant_id,
                TenantEvent.account_map_suggested,
                "account_map",
                current.id,
                actor,
                after=_map_plain(current),
                account_no=account.account_no,
            )
            continue
        same = (
            current.division_id == s.division_id
            and current.cost_category_id == s.cost_category_id
            and current.in_job_cost == s.in_job_cost
            and current.suggested_by_rule == s.rule_name
        )
        if same:
            counts.unchanged += 1
            continue
        before = _map_plain(current)
        current.division_id = s.division_id
        current.cost_category_id = s.cost_category_id
        current.in_job_cost = s.in_job_cost
        current.suggested_by_rule = s.rule_name
        db.flush()
        counts.suggested += 1
        audit(
            db,
            tenant_id,
            TenantEvent.account_map_suggested,
            "account_map",
            current.id,
            actor,
            before=before,
            after=_map_plain(current),
            account_no=account.account_no,
        )
    return counts


def _map_plain(m: AccountMap) -> dict:
    return {
        "division_id": str(m.division_id) if m.division_id else None,
        "cost_category_id": str(m.cost_category_id) if m.cost_category_id else None,
        "in_job_cost": m.in_job_cost,
        "status": m.status,
        "suggested_by_rule": m.suggested_by_rule,
    }


def chart_account_numbers(store: ObjectStore, batch: ImportBatch) -> set[str]:
    """Every account number in the batch's stored file (the evidence of what the
    chart held on that day)."""
    from app.ingest.imports import open_batch_object
    from app.integrations.base import RawItem
    from app.integrations.chart_of_accounts import parse_chart

    with open_batch_object(store, batch) as f:
        return {
            item.external_id
            for item in parse_chart(f)
            if isinstance(item, RawItem) and item.entity_type == "account"
        }


@task(NORMALIZE_CHART)
def normalize_chart_task(tenant_id: UUID, *, engine: Engine, import_batch_id: str) -> None:
    from app.core.config import get_settings
    from app.core.storage import build_object_store

    batch_id = UUID(import_batch_id)
    with tenant_session(engine, tenant_id) as s:
        batch = s.get(ImportBatch, batch_id)
        if batch is None:
            raise LookupError("import batch not found in this tenant")
        s.expunge(batch)
    present = chart_account_numbers(build_object_store(get_settings()), batch)
    with tenant_session(engine, tenant_id) as s:
        counts = normalize_chart(s, tenant_id, batch_id, present=present)
    if counts.superseded:
        # Nothing at all changes, suggestions included.
        log.info(
            "tenant=%s batch=%s chart not applied: a newer chart batch exists",
            tenant_id,
            batch_id,
        )
        return
    log.info(
        "tenant=%s batch=%s chart normalized: added=%d renamed=%d deactivated=%d unchanged=%d",
        tenant_id,
        batch_id,
        counts.added,
        counts.renamed,
        counts.deactivated,
        counts.unchanged,
    )
    with tenant_session(engine, tenant_id) as s:
        sc = suggest(s, tenant_id)
    log.info(
        "tenant=%s batch=%s suggestions: new=%d unchanged=%d removed=%d unmatched=%d",
        tenant_id,
        batch_id,
        sc.suggested,
        sc.unchanged,
        sc.removed,
        sc.unmatched,
    )
