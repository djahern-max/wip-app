#!/usr/bin/env python
"""F08.2 item 1: why the collected side of the board's tie-out does not balance.
Read-only, from the tenant's canonical rows under RLS; never calls Intuit; prints no
customer name (ids, dates, kinds and amounts only).

    cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \\
        scripts/collected_tieout.py --tenant rye-beach

With no ``--month`` it reports every month whose collected side does not tie, found
by the same ``tie_out`` the Jobs page calls (so the figures are the screen's);
``--month 2018-12`` (repeatable) limits it to the months named; ``--counts`` prints only
the row counts the copy holds (for seeding a dev database to the same size).

For each month: the board's collected side (jobs, not on a job, of which unapplied),
QuickBooks payments + sales receipts, the difference; then every live payment dated
in the month whose signed applications + unapplied differ from its total (QuickBooks
id, kind, date, total, unapplied, Σ applications, difference); then every application
on a payment of the month that does not point at an invoice (payment id, line, the
linked type and id, amount, whether the copy holds the document). At the end, one
line per linked type over the months printed and one sentence saying in how many
months the difference equals twice the sum of the month's non-CreditMemo credit lines
(the suspect: a credit applied through a payment counted as cash, Plan answer 2).
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.domain.billing.board import TieRow, tie_out
from app.domain.billing.figures import CREDIT_TXN_TYPES, words
from app.domain.billing.models import Billing, BillingLine, Customer, Payment, PaymentApplication
from app.domain.billing.totals import month_of
from app.tenancy.models import Tenant

ZERO = Decimal("0.00")
# An application that points at an invoice: an Invoice line, or a sales receipt's own
# collected side (F05: a receipt is applied to itself).
INVOICE_TYPES: frozenset[str] = frozenset({"Invoice", "SalesReceipt"})
COUNTED_TABLES = (
    ("customers", Customer),
    ("documents", Billing),
    ("document lines", BillingLine),
    ("payments", Payment),
    ("payment applications", PaymentApplication),
)


@dataclass(frozen=True)
class PaymentOff:
    external_id: str
    kind: str
    txn_date: date
    total: Decimal
    unapplied: Decimal
    applied: Decimal  # Σ signed applications (the board's rule)
    linked_types: tuple[str, ...]

    @property
    def difference(self) -> Decimal:
        return self.applied + self.unapplied - self.total


@dataclass(frozen=True)
class OddApplication:
    payment_external_id: str
    payment_kind: str
    txn_date: date
    line_no: int
    linked_txn_type: str
    linked_txn_external_id: str
    amount: Decimal
    held: bool  # the copy holds the document the line names
    payment_deleted: bool


@dataclass(frozen=True)
class MonthReport:
    row: TieRow
    unapplied: Decimal  # Σ unapplied of the month's live payments (inside the board's sum)
    payments_off: tuple[PaymentOff, ...]
    odd: tuple[OddApplication, ...]

    @property
    def twice_the_credits(self) -> Decimal:
        """Twice the month's non-CreditMemo credit lines on live payments: what the
        difference would be if such a line were counted as cash (Plan answer 2)."""
        return 2 * sum(
            (
                o.amount
                for o in self.odd
                if o.linked_txn_type not in CREDIT_TXN_TYPES and not o.payment_deleted
            ),
            ZERO,
        )

    @property
    def explained(self) -> bool:
        return self.row.collected_difference == self.twice_the_credits


def _sign(linked_txn_type: str) -> int:
    return -1 if linked_txn_type in CREDIT_TXN_TYPES else 1


def off_months(db: Session, tenant_id: UUID) -> list[TieRow]:
    return [r for r in tie_out(db, tenant_id) if r.collected_difference != ZERO]


def month_report(db: Session, tenant_id: UUID, row: TieRow) -> MonthReport:
    in_month = month_of(Payment.txn_date) == row.month
    payments = list(
        db.execute(
            select(Payment).where(in_month).order_by(Payment.txn_date, Payment.external_id)
        ).scalars()
    )
    apps: dict[UUID, list[PaymentApplication]] = {}
    for a in db.execute(
        select(PaymentApplication)
        .where(PaymentApplication.payment_id.in_(select(Payment.id).where(in_month)))
        .order_by(PaymentApplication.line_no)
    ).scalars():
        apps.setdefault(a.payment_id, []).append(a)
    off: list[PaymentOff] = []
    odd: list[OddApplication] = []
    unapplied = ZERO
    for p in payments:
        lines = apps.get(p.id, [])
        deleted = p.deleted_at is not None
        if not deleted:
            unapplied += p.unapplied_amount
            applied = sum((_sign(a.linked_txn_type) * a.amount for a in lines), ZERO)
            if applied + p.unapplied_amount != p.total:
                off.append(
                    PaymentOff(
                        p.external_id,
                        p.kind,
                        p.txn_date,
                        p.total,
                        p.unapplied_amount,
                        applied,
                        tuple(a.linked_txn_type for a in lines),
                    )
                )
        for a in lines:
            if a.linked_txn_type not in INVOICE_TYPES:
                odd.append(
                    OddApplication(
                        p.external_id,
                        p.kind,
                        p.txn_date,
                        a.line_no,
                        a.linked_txn_type,
                        a.linked_txn_external_id,
                        a.amount,
                        a.billing_id is not None,
                        deleted,
                    )
                )
    return MonthReport(row, unapplied, tuple(off), tuple(odd))


def counts(db: Session) -> dict[str, int]:
    return {
        name: db.execute(select(func.count()).select_from(model)).scalar_one()
        for name, model in COUNTED_TABLES
    }


def report(reports: list[MonthReport], months_held: int, months_off: int) -> str:
    """``reports``: the months printed (the off ones, or the ones asked for);
    ``months_off``: how many of the ``months_held`` do not tie."""
    if months_off == 0 and not reports:
        return f"Every month ties on the collected side ({months_held} months)."
    lines = [
        f"{months_off} of {months_held} months do not tie on the collected side; "
        f"{len(reports)} reported "
        "(board = jobs + not on a job, unapplied included; QuickBooks = payments + sales receipts)."
    ]
    by_type: dict[str, list[Decimal]] = {}
    for m in reports:
        r = m.row
        lines.append("")
        lines.append(
            f"{r.month}  board {words(r.jobs_collected)} + {words(r.other_collected)} "
            f"= {words(r.jobs_collected + r.other_collected)} (of which unapplied "
            f"{words(m.unapplied)}); QuickBooks {words(r.ledger_collected)}; "
            f"difference {words(r.collected_difference)}"
        )
        lines.append(
            f"  payments whose signed applications + unapplied differ from their total: "
            f"{len(m.payments_off)}"
        )
        for p in m.payments_off:
            lines.append(
                f"    {p.kind} {p.external_id}  {p.txn_date.isoformat()}  total {words(p.total)}  "
                f"unapplied {words(p.unapplied)}  applications {words(p.applied)}  "
                f"difference {words(p.difference)}  lines: {', '.join(p.linked_types) or 'none'}"
            )
        lines.append(f"  applications that do not point at an invoice: {len(m.odd)}")
        for o in m.odd:
            held = "document held" if o.held else "document not held"
            deleted = "; payment deleted" if o.payment_deleted else ""
            lines.append(
                f"    {o.payment_kind} {o.payment_external_id} line {o.line_no}  "
                f"{o.linked_txn_type} {o.linked_txn_external_id}  {words(o.amount)}  "
                f"({held}{deleted})"
            )
            if not o.payment_deleted:
                by_type.setdefault(o.linked_txn_type, []).append(o.amount)
        lines.append(
            f"  twice the month's non-CreditMemo credit lines: {words(m.twice_the_credits)}"
            f"{' = the difference' if m.explained else ' (not the difference)'}"
        )
    lines.append("")
    lines.append(f"Summary over the {len(reports)} months, by linked type (live payments):")
    for kind in sorted(by_type):
        amounts = by_type[kind]
        total = sum(amounts, ZERO)
        lines.append(f"  {kind}: {len(amounts)} lines, {words(total)}, twice {words(2 * total)}")
    if not by_type:
        lines.append("  (none)")
    off = [m for m in reports if m.row.collected_difference != ZERO]
    explained = sum(1 for m in off if m.explained)
    lines.append(
        f"In {explained} of {len(off)} months that do not tie, the difference equals twice the "
        "sum of the month's non-CreditMemo credit lines (a credit applied through a payment, "
        "counted as cash)."
    )
    rest = [m.row.month for m in off if not m.explained]
    if rest:
        lines.append(f"Not explained that way: {', '.join(rest)}.")
    return "\n".join(lines)


def run(engine: Engine, tenant_id: UUID, months: list[str] | None) -> str:
    with tenant_session(engine, tenant_id) as db:
        rows = tie_out(db, tenant_id)
        off = [r for r in rows if r.collected_difference != ZERO]
        chosen = [r for r in rows if r.month in months] if months else off
        reports = [month_report(db, tenant_id, r) for r in chosen]
        return report(reports, len(rows), len(off))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant", required=True, help="tenant slug")
    parser.add_argument("--month", action="append", help="YYYY-MM; repeatable")
    parser.add_argument("--counts", action="store_true", help="print the row counts only")
    args = parser.parse_args(argv)
    engine = create_app_engine()
    with untenanted_session(engine) as db:
        tenant_id = db.execute(
            select(Tenant.id).where(Tenant.slug == args.tenant)
        ).scalar_one_or_none()
    if tenant_id is None:
        print(f"no tenant with slug {args.tenant!r}", file=sys.stderr)
        return 2
    if args.counts:
        with tenant_session(engine, tenant_id) as db:
            for name, n in counts(db).items():
                print(f"{name}: {n}")
        return 0
    print(run(engine, tenant_id, args.month))
    return 0


if __name__ == "__main__":
    sys.exit(main())
