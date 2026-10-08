"""Home (F07.3): the reads. Each fact comes from the function the linked page already
calls, so every count on Home equals the page's count by construction; the pure
functions in ``checklist.py`` turn the facts into lines. Requires ``app.tenant_id`` on
the session. Reads only; nothing here writes."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.authz import CAPABILITIES
from app.domain.billing.board import load_board, money_str
from app.domain.config.burden import active_burden_rates, pick_rate
from app.domain.config.models import (
    AccountMap,
    AccountSuggestRule,
    Division,
    GlAccount,
    TenantPolicy,
)
from app.domain.config.policy import POLICY_KEYS, WIP_BASIS
from app.domain.config.service import unmapped_count
from app.domain.estimates.exceptions import Issue
from app.domain.estimates.models import Estimate
from app.domain.exceptions import service as exceptions
from app.domain.exceptions.collect import job_attention
from app.domain.home.checklist import (
    Item,
    JobFacts,
    JobLine,
    Line,
    SetupFacts,
    job_lines,
    review_item,
    setup_lines,
    tracked_items,
)
from app.domain.jobs import service as jobs
from app.domain.jobs.models import JOB_STATUS_LABELS
from app.ingest.models import Connection
from app.integrations.qbo import SYSTEM as QBO_SYSTEM
from app.integrations.qbo.status import copy_status
from app.tenancy.models import Role


@dataclass(frozen=True)
class HomeView:
    setup: list[Line] | None
    review: Item | None
    jobs: list[JobLine]
    tracked_without_job: list[Item]


def _may(role: Role | None, capability: str) -> bool:
    return role is not None and role in CAPABILITIES[capability][1]


def _connection_facts(
    db: Session, tenant_id: UUID
) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    row = db.execute(select(Connection).where(Connection.system == QBO_SYSTEM)).scalar_one_or_none()
    if row is None:
        return None, ()
    if row.realm_id is None:
        return row.status, ()
    held = copy_status(db, tenant_id, row)
    return row.status, tuple((a.code, a.detail or a.label) for a in held.attention)


def _count(db: Session, stmt) -> int:
    return db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()


def setup_facts(db: Session, tenant_id: UUID, role: Role | None) -> SetupFacts:
    status, attention = _connection_facts(db, tenant_id)
    active_accounts = _count(db, select(GlAccount.id).where(GlAccount.active))
    suggested = _count(
        db,
        select(AccountMap.id)
        .join(GlAccount, GlAccount.id == AccountMap.gl_account_id)
        .where(GlAccount.active, AccountMap.status == "suggested"),
    )
    policies = {r.key: r for r in db.execute(select(TenantPolicy)).scalars()}
    basis_row = policies.get(WIP_BASIS)
    basis = frozenset(str(v) for v in basis_row.value) if basis_row is not None else None
    divisions = list(
        db.execute(
            select(Division)
            .where(Division.active, Division.code_digit.is_not(None))
            .order_by(Division.sort_order, Division.code)
        ).scalars()
    )
    rates = active_burden_rates(db)
    today = jobs.tenant_today(db)
    without = tuple(d.code for d in divisions if pick_rate(rates, today, d.id) is None)
    return SetupFacts(
        connection_status=status,
        attention=attention,
        active_rules=_count(db, select(AccountSuggestRule.id).where(AccountSuggestRule.active)),
        active_accounts=active_accounts,
        unmapped_accounts=unmapped_count(db) if active_accounts else 0,
        suggested_accounts=suggested,
        all_policy_keys=tuple(POLICY_KEYS),
        decided_keys=frozenset(policies),
        policy_labels={k: spec.label for k, spec in POLICY_KEYS.items()},
        wip_basis=basis,
        divisions_with_digit=tuple(d.code for d in divisions),
        divisions_without_rate=without,
        can_view_connections=_may(role, "view_connections"),
        can_view_config=_may(role, "view_tenant_config"),
    )


def _first(by_code: dict[str, list[Issue]], code: str) -> Issue | None:
    found = by_code.get(code)
    return found[0] if found else None


def _amount(detail: dict, key: str) -> str | None:
    value = detail.get(key)
    return None if value is None else money_str(Decimal(str(value)))


def job_facts(db: Session, tenant_id: UUID) -> list[JobFacts]:
    """F09: every fact that is a review sentence comes from the one computation
    (``job_attention``), with the dismissed exceptions left out, so Home's needs and the
    board's sentences cannot disagree; the contract facts come from the view."""
    views = jobs.list_jobs(db, tenant_id)
    board = load_board(db, tenant_id, views, other=False)  # F08: the billing needs
    dismissed = exceptions.dismissed_keys(db)
    out = []
    for v in views:
        f = board.per_job[v.job.id]
        issues = exceptions.without_dismissed(
            job_attention(v, f, board.work_areas.get(v.job.id), board.applications.get(v.job.id)),
            "job",
            v.job.id,
            dismissed,
        )
        by_code: dict[str, list[Issue]] = {}
        for i in issues:
            by_code.setdefault(i.code, []).append(i)
        unapplied = _first(by_code, "PAYMENT_UNAPPLIED")
        over = _first(by_code, "BILLED_OVER_CONTRACT")
        flag = _first(by_code, "BILLING_UNAPPROVED_CO")
        credits = by_code.get("PAYMENT_OTHER_CREDIT", [])
        credit_total = sum((Decimal(str(i.detail["amount"])) for i in credits), Decimal("0.00"))
        out.append(
            JobFacts(
                id=str(v.job.id),
                name=v.job.name,
                status=v.job.status,
                status_label=JOB_STATUS_LABELS[v.job.status],
                revenue_method=v.job.revenue_method,
                to_confirm=v.contract.to_confirm,
                qbo_linked=bool(v.qbo_aliases),
                needs_link="JOB_NO_LEDGER_LINK" in by_code,
                unapplied_payments=_amount(unapplied.detail, "amount") if unapplied else None,
                deposit_not_identified=len(by_code.get("DEPOSIT_NOT_IDENTIFIED", [])),
                billed_over_contract=_amount(over.detail, "over") if over else None,
                unapproved_change_orders=money_str(v.contract.unapproved_change_orders),
                unapproved_count=v.contract.unapproved_count,
                approvals_ended=len(by_code.get("CO_APPROVAL_NOT_CARRIED", [])),
                billing_unapproved_co=_amount(flag.detail, "amount") if flag else None,
                billing_unapproved_labels=tuple(flag.detail["work_areas"]) if flag else (),
                other_credits_applied=money_str(credit_total) if credits else None,
                other_credit_count=len(credits),
                payapp_not_invoiced=len(by_code.get("PAYAPP_NOT_INVOICED", [])),
                payapp_invoice_mismatch=len(by_code.get("PAYAPP_INVOICE_MISMATCH", [])),
                invoice_no_payapp=len(by_code.get("INVOICE_NO_PAYAPP", [])),
            )
        )
    return out


def home(db: Session, tenant_id: UUID, role: Role | None) -> HomeView:
    can_manage = _may(role, "manage_jobs")
    return HomeView(
        setup=setup_lines(setup_facts(db, tenant_id, role)),
        review=review_item(
            len(jobs.review_queue(db, tenant_id)),
            _count(db, select(Estimate.id)),
            can_review=can_manage,
            can_import=_may(role, "manage_imports"),
        ),
        jobs=job_lines(job_facts(db, tenant_id)),
        tracked_without_job=tracked_items(
            [i.message for i in jobs.ledger_items(db)], can_track=_may(role, "track_customers")
        ),
    )
