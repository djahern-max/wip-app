"""What the Connections page shows about the copy (F05): counts, attention items,
month totals, the last runs. Reads only; money leaves as strings."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    QboAttentionOut,
    QboCopyOut,
    QboEntityCountOut,
    QboMonthTotalsOut,
    QboSyncRunOut,
)
from app.domain.billing.report import current_counts, skipped_counts
from app.domain.billing.sync import chart_match, unlinked_deposit_lines
from app.domain.billing.totals import month_totals
from app.ingest.models import Connection, SyncRun
from app.integrations.qbo.entities import ENTITIES
from app.integrations.qbo.schedule import cursor_too_old, open_backfill, resume_cursor

KIND_LABELS = {"backfill": "Backfill", "cdc": "Change poll", "drift": "Nightly check"}
OUTCOME_LABELS = {
    None: "Running",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "drift": "Difference found",
}
FAILURE_MESSAGES = {
    "needs_reconnect": "QuickBooks stopped accepting this connection; reconnect it.",
    "cursor_too_old": "The copy is more than 29 days behind QuickBooks; start a fresh backfill.",
    "no_cursor": "No backfill has completed for this company; start one.",
    "unavailable": "QuickBooks could not be reached; it will be tried again.",
}


@dataclass(frozen=True)
class _Run:
    row: SyncRun


def _money(value) -> str:
    return f"{value:.2f}"


def _run_out(run: SyncRun | None) -> QboSyncRunOut | None:
    if run is None:
        return None
    message = None
    if run.outcome == "failed":
        message = FAILURE_MESSAGES.get(run.error or "", "The run failed; it will be tried again.")
    elif run.outcome == "drift":
        detail = run.detail or {}
        parts = []
        for entity, c in (detail.get("counts") or {}).items():
            parts.append(
                f"{entity}: QuickBooks {c.get('quickbooks')}, platform {c.get('platform')}"
            )
        for month, columns in (detail.get("months") or {}).items():
            for column, v in columns.items():
                parts.append(
                    f"{month} {column.replace('_', ' ')}: rows {v.get('normalized')}, "
                    f"raw {v.get('raw')}"
                )
        message = "; ".join(parts) or "A difference was found."
    return QboSyncRunOut(
        kind=run.kind,
        kind_label=KIND_LABELS.get(run.kind, run.kind),
        outcome=run.outcome,
        outcome_label=OUTCOME_LABELS.get(run.outcome, str(run.outcome)),
        started_at=run.started_at.isoformat(),
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        error_detail=run.error,
        message=message,
    )


def _latest_run(db: Session, connection_id: UUID, *kinds: str) -> SyncRun | None:
    return db.execute(
        select(SyncRun)
        .where(SyncRun.connection_id == connection_id, SyncRun.kind.in_(kinds))
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def copy_status(db: Session, tenant_id: UUID, connection: Connection) -> QboCopyOut:
    counts = current_counts(db, tenant_id)
    skipped = skipped_counts(db, tenant_id)
    entities = [
        QboEntityCountOut(entity=e, current=counts.get(e, 0), skipped=skipped.get(e))
        for e in ENTITIES
    ]
    attention: list[QboAttentionOut] = []
    if connection.status == "needs_reconnect":
        attention.append(
            QboAttentionOut(
                code="needs_reconnect",
                label="Needs reconnect",
                detail="QuickBooks stopped accepting this connection. Reconnect it.",
            )
        )
    backfill = open_backfill(db, connection.id) or _latest_run(db, connection.id, "backfill")
    cursor = resume_cursor(db, connection)
    backfill_needed = cursor is None or cursor_too_old(cursor, datetime.now(UTC))
    if backfill is not None and backfill.outcome is None:
        backfill_needed = False
    if backfill_needed and connection.status == "connected":
        attention.append(
            QboAttentionOut(
                code="backfill_needed",
                label="Fresh backfill needed",
                detail=(
                    "No backfill has completed for this company."
                    if cursor is None
                    else "The copy is more than 29 days behind QuickBooks."
                ),
            )
        )
    drift = _latest_run(db, connection.id, "drift")
    if drift is not None and drift.outcome == "drift":
        attention.append(
            QboAttentionOut(
                code="drift",
                label="Nightly check found a difference",
                detail=_run_out(drift).message,
            )
        )
    total_skipped = sum(v for v in skipped.values())
    if total_skipped:
        attention.append(
            QboAttentionOut(
                code="skipped",
                label="Payloads that could not be read",
                detail=", ".join(f"{e}: {n}" for e, n in skipped.items() if n),
            )
        )
    match = chart_match(db, tenant_id)
    numbers = match.numbers
    if numbers.without_number:
        attention.append(
            QboAttentionOut(
                code="accounts_without_number",
                label="Accounts without a number",
                detail=f"{numbers.without_number} of {numbers.total} active accounts",
            )
        )
    if numbers.duplicate_numbers:
        attention.append(
            QboAttentionOut(
                code="duplicate_account_numbers",
                label="Duplicate account numbers",
                detail=f"{numbers.duplicate_numbers}",
            )
        )
    if match.unmatched:
        attention.append(
            QboAttentionOut(
                code="numbered_not_in_chart",
                label="Numbered in QuickBooks but not in the chart",
                detail=", ".join(match.unmatched),
            )
        )
    if match.chart_only:
        attention.append(
            QboAttentionOut(
                code="chart_not_in_quickbooks",
                label="In the chart but not numbered in QuickBooks",
                detail=", ".join(match.chart_only),
            )
        )
    deposit_count, deposit_total = unlinked_deposit_lines(db, tenant_id)
    if deposit_count:
        attention.append(
            QboAttentionOut(
                code="unlinked_deposit_lines",
                label="Deposit lines not linked to a document",
                detail=f"{deposit_count} lines, {_money(deposit_total)}",
            )
        )
    return QboCopyOut(
        entities=entities,
        month_totals=[
            QboMonthTotalsOut(
                month=m.month,
                invoices=_money(m.invoices),
                credit_memos=_money(m.credit_memos),
                sales_receipts=_money(m.sales_receipts),
                payments=_money(m.payments),
            )
            for m in month_totals(db, tenant_id)
        ],
        attention=attention,
        last_sync=_run_out(_latest_run(db, connection.id, "cdc", "backfill")),
        backfill=_run_out(backfill),
        backfill_needed=backfill_needed,
    )
