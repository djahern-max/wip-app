"""The cost figures (F10; BLUEPRINT §8.2 cost to date, §8.5 check 2 and check 3's ledger
side; D-04, D-22, D-51). Pure: ``Decimal`` in and out, no database, no clock (``as_of`` is
an argument). ``load.py`` reads the rows and calls this; nothing here is stored.

Each line is read with its account (``AccountIn``: the chart row, its confirmed
``account_map`` and the tenant's categories) and the tenant's WIP basis, at the time the
figure is computed. A line is one of five **parts**:

- **labor in the ledger**: the account's confirmed category is the Labor (10) or Labor
  Burden (20) slot; never job cost, never unassigned (§2.1; labor reaches jobs from hours
  and rates, F11);
- **outside job cost**: the account has no confirmed mapping as job cost (unmapped, not in
  the chart, no account, or ``in_job_cost`` false, such as shared fuel): listed, never a
  job's cost, never unassigned;
- **unassigned**: a job-cost line with no job (D-51);
- **on jobs**: a job-cost line on a job whose category is in the WIP basis (every job-cost
  line on a job while the basis is not decided);
- **memo on jobs**: a job-cost line on a job whose category is outside the basis
  (Equipment (owned), Vehicles (owned), Fuel, Equipment Maintenance under D-04).

The five parts sum to the ledger total of every account and month by construction, so the
tie-out's tick means "every line read" and a difference to the QuickBooks Profit and Loss is
a line the platform could not read, not one it reclassified (D-51).

- **Cost to date** on a job = Σ amount of its job-cost lines dated on or before ``as_of``,
  by cost category; **in the WIP basis** over the basis slots (``None`` while the basis is
  not decided: "Not computed"), **memo** over the rest. A job with no line reads 0.00.
- ``job_cost_lines`` is the §7 ``job_cost_line`` union as a function over rows, with
  ``basis = gl_direct``; F11 and F13.1 add their rows.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

ZERO = Decimal("0.00")
LABOR_SLOTS: frozenset[str] = frozenset({"10", "20"})
GL_DIRECT = "gl_direct"
PARTS: tuple[str, ...] = ("on_jobs", "memo", "unassigned", "labor", "outside")
NO_ACCOUNT = ""  # the account key of a line with no account
MEMO_LABEL = "Owned equipment, vehicles and fuel"


@dataclass(frozen=True)
class AccountIn:
    """What is known about one account when a figure is computed."""

    key: str  # the QuickBooks account id; "" for a line with no account
    gl_account_id: str | None
    account_no: str | None
    name: str | None
    in_chart: bool
    mapped: bool  # a confirmed account_map row
    in_job_cost: bool
    slot: str | None
    category_name: str | None
    division_id: str | None
    division_code: str | None
    division_name: str | None
    quickbooks_words: str | None = None  # an account the chart does not hold: its number and name

    @property
    def labor(self) -> bool:
        return self.mapped and self.slot in LABOR_SLOTS

    @property
    def job_cost(self) -> bool:
        return self.mapped and self.in_job_cost and not self.labor

    @property
    def label(self) -> str:
        if self.key == NO_ACCOUNT:
            return "No account"
        if not self.in_chart:
            words = self.quickbooks_words or f"QuickBooks account {self.key}"
            return f"Not in the chart: {words}"
        return " ".join(p for p in (self.account_no, self.name) if p)

    @property
    def sort_key(self) -> tuple:
        return (0 if self.in_chart else 1, self.account_no or "", self.key)


NO_ACCOUNT_IN = AccountIn(
    NO_ACCOUNT, None, None, None, False, False, False, None, None, None, None, None
)


@dataclass(frozen=True)
class LineIn:
    id: str
    txn_type: str
    txn_external_id: str
    line_external_id: str
    line_no: int
    doc_number: str | None
    txn_date: date
    account_key: str  # "" for no account
    vendor_external_id: str | None
    customer_external_id: str | None
    job_id: str | None
    amount: Decimal
    description: str | None
    memo: str | None
    deleted: bool


@dataclass(frozen=True)
class CostPolicy:
    basis: frozenset[str] | None  # None: wip_basis not decided
    categories: tuple[tuple[str, str], ...]  # (slot, name) in slot order

    def in_basis(self, slot: str | None) -> bool | None:
        if self.basis is None:
            return None
        return slot is not None and slot in self.basis


def account_of(line: LineIn, accounts: dict[str, AccountIn]) -> AccountIn:
    return accounts.get(line.account_key, NO_ACCOUNT_IN)


def part_of(line: LineIn, account: AccountIn, policy: CostPolicy) -> str | None:
    """The part a live line belongs to; ``None`` for a deleted line."""
    if line.deleted:
        return None
    if account.labor:
        return "labor"
    if not account.job_cost:
        return "outside"
    if line.job_id is None:
        return "unassigned"
    if policy.in_basis(account.slot) is False:
        return "memo"
    return "on_jobs"


def month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _add(table: dict, key, amount: Decimal) -> None:
    table[key] = table.get(key, ZERO) + amount


# --- cost to date on a job ------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryCost:
    slot: str
    name: str
    amount: Decimal
    in_basis: bool | None  # None: not decided


@dataclass(frozen=True)
class JobCost:
    by_category: tuple[CategoryCost, ...]
    in_basis: Decimal | None  # None: the WIP basis is not decided
    memo: Decimal
    lines: int  # live job-cost lines counted
    basis_decided: bool

    @property
    def total(self) -> Decimal:
        return sum((c.amount for c in self.by_category), ZERO)


def job_cost(
    lines: Sequence[LineIn], accounts: dict[str, AccountIn], policy: CostPolicy, *, as_of: date
) -> JobCost:
    """``lines``: one job's lines (any part; the function keeps the job-cost ones)."""
    amounts: dict[str, Decimal] = {}
    counted = 0
    for ln in lines:
        account = account_of(ln, accounts)
        if ln.deleted or not account.job_cost or ln.txn_date > as_of or account.slot is None:
            continue
        _add(amounts, account.slot, ln.amount)
        counted += 1
    rows = tuple(
        CategoryCost(slot, name, amounts.get(slot, ZERO), policy.in_basis(slot))
        for slot, name in policy.categories
    )
    decided = policy.basis is not None
    in_basis = sum((c.amount for c in rows if c.in_basis), ZERO) if decided else None
    memo = sum((c.amount for c in rows if c.in_basis is False), ZERO)
    return JobCost(rows, in_basis, memo, counted, decided)


# --- unassigned, by document ---------------------------------------------------------------------


@dataclass(frozen=True)
class UnassignedDoc:
    txn_type: str
    txn_external_id: str
    doc_number: str | None
    txn_date: date
    vendor_external_id: str | None
    customer_external_id: str | None  # None: coded to no customer
    account_labels: tuple[str, ...]
    account_numbers: tuple[str, ...]
    amount: Decimal
    lines: int

    @property
    def document(self) -> str:
        return self.doc_number or f"QuickBooks id {self.txn_external_id}"


def unassigned_documents(
    lines: Sequence[LineIn], accounts: dict[str, AccountIn], policy: CostPolicy
) -> list[UnassignedDoc]:
    """One row per document and customer reference, over the unassigned lines, oldest first."""
    groups: dict[tuple[str, str, str | None], list[tuple[LineIn, AccountIn]]] = {}
    for ln in lines:
        account = account_of(ln, accounts)
        if part_of(ln, account, policy) != "unassigned":
            continue
        groups.setdefault((ln.txn_type, ln.txn_external_id, ln.customer_external_id), []).append(
            (ln, account)
        )
    out: list[UnassignedDoc] = []
    for (txn_type, external_id, customer), rows in groups.items():
        labels: list[str] = []
        numbers: list[str] = []
        for _ln, account in rows:
            if account.label not in labels:
                labels.append(account.label)
                numbers.append(account.account_no or account.key)
        first = rows[0][0]
        out.append(
            UnassignedDoc(
                txn_type=txn_type,
                txn_external_id=external_id,
                doc_number=first.doc_number,
                txn_date=first.txn_date,
                vendor_external_id=first.vendor_external_id,
                customer_external_id=customer,
                account_labels=tuple(labels),
                account_numbers=tuple(numbers),
                amount=sum((ln.amount for ln, _a in rows), ZERO),
                lines=len(rows),
            )
        )
    return sorted(
        out, key=lambda d: (d.txn_date, d.txn_type, d.txn_external_id, d.customer_external_id or "")
    )


# --- labor in the ledger -------------------------------------------------------------------


@dataclass(frozen=True)
class LaborRow:
    month: str
    division_code: str | None
    division_name: str | None
    amount: Decimal


def labor_in_ledger(
    lines: Sequence[LineIn], accounts: dict[str, AccountIn], policy: CostPolicy
) -> list[LaborRow]:
    sums: dict[tuple[str, str | None, str | None], Decimal] = {}
    for ln in lines:
        account = account_of(ln, accounts)
        if part_of(ln, account, policy) != "labor":
            continue
        _add(
            sums, (month_key(ln.txn_date), account.division_code, account.division_name), ln.amount
        )
    return [
        LaborRow(m, code, name, amount)
        for (m, code, name), amount in sorted(
            sums.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
        )
    ]


# --- the tie-out (§8.5 check 2) ----------------------------------------------------------------


@dataclass(frozen=True)
class CostTieRow:
    month: str
    account: AccountIn
    on_jobs: Decimal
    memo: Decimal
    unassigned: Decimal
    labor: Decimal
    outside: Decimal
    total: Decimal  # Σ of every live line on the account in the month

    @property
    def parts(self) -> Decimal:
        return self.on_jobs + self.memo + self.unassigned + self.labor + self.outside

    @property
    def difference(self) -> Decimal:
        return self.parts - self.total

    @property
    def balanced(self) -> bool:
        return self.difference == ZERO


def tie_out(
    lines: Sequence[LineIn], accounts: dict[str, AccountIn], policy: CostPolicy
) -> list[CostTieRow]:
    """Every month and account with a live line, oldest month first, chart accounts by
    number then the rest."""
    sums: dict[tuple[str, str], dict[str, Decimal]] = {}
    for ln in lines:
        account = account_of(ln, accounts)
        part = part_of(ln, account, policy)
        if part is None:
            continue
        cell = sums.setdefault(
            (month_key(ln.txn_date), account.key), dict.fromkeys((*PARTS, "total"), ZERO)
        )
        cell[part] += ln.amount
        cell["total"] += ln.amount
    rows = [
        CostTieRow(month, accounts.get(key, NO_ACCOUNT_IN), **cell)
        for (month, key), cell in sums.items()
    ]
    return sorted(rows, key=lambda r: (r.month, r.account.sort_key))


def tie_out_status(rows: Sequence[CostTieRow], *, skipped: int) -> str:
    """Words first (D-22, D-49): the tick means every line was read."""
    if not rows:
        return "Nothing to tie out yet: no cost lines held."
    months = len({r.month for r in rows})
    accounts = len({r.account.key for r in rows})
    off = [r for r in rows if not r.balanced]
    if off:
        return f"Does not tie in {len(off)} of {len(rows)} rows."
    read = (
        "every payload read"
        if skipped == 0
        else f"{skipped} payload{'' if skipped == 1 else 's'} could not be read"
    )
    return (
        f"Ties to the cent for {months} month{'' if months == 1 else 's'} and {accounts} "
        f"account{'' if accounts == 1 else 's'}; {read}."
    )


# --- division mismatch (§5; COST_DIVISION_MISMATCH) ----------------------------------------------


@dataclass(frozen=True)
class DivisionMismatch:
    txn_type: str
    txn_external_id: str
    doc_number: str | None
    job_id: str
    amount: Decimal
    account_label: str
    account_no: str | None
    account_division_name: str
    job_division_name: str

    @property
    def document(self) -> str:
        return self.doc_number or f"QuickBooks id {self.txn_external_id}"


def division_mismatches(
    lines: Sequence[LineIn],
    accounts: dict[str, AccountIn],
    policy: CostPolicy,
    job_divisions: dict[str, tuple[str, str]],  # job id → (division id, division name)
) -> list[DivisionMismatch]:
    """One per document and job where a job-cost line's account division differs from the
    job's (both set). A pool is exempt only when the account's division is its own, which
    is the same rule as any job (D-30). Labor-slot lines never raise."""
    groups: dict[tuple[str, str, str, str], list[tuple[LineIn, AccountIn]]] = {}
    for ln in lines:
        account = account_of(ln, accounts)
        if ln.deleted or not account.job_cost or ln.job_id is None or account.division_id is None:
            continue
        job = job_divisions.get(ln.job_id)
        if job is None or job[0] is None or job[0] == account.division_id:
            continue
        groups.setdefault((ln.txn_type, ln.txn_external_id, ln.job_id, account.key), []).append(
            (ln, account)
        )
    out = []
    for (txn_type, external_id, job_id, _key), rows in groups.items():
        first, account = rows[0]
        out.append(
            DivisionMismatch(
                txn_type=txn_type,
                txn_external_id=external_id,
                doc_number=first.doc_number,
                job_id=job_id,
                amount=sum((ln.amount for ln, _a in rows), ZERO),
                account_label=account.label,
                account_no=account.account_no,
                account_division_name=account.division_name or account.division_code or "",
                job_division_name=job_divisions[job_id][1],
            )
        )
    return sorted(out, key=lambda m: (m.txn_type, m.txn_external_id, m.job_id))


# --- the §7 view as a function --------------------------------------------------------------------


@dataclass(frozen=True)
class JobCostLine:
    basis: str  # gl_direct | labor_computed | allocated
    job_id: str
    txn_date: date
    account_key: str
    slot: str | None
    amount: Decimal
    source_ref: str  # "Bill 98892 line 1"


def job_cost_lines(
    lines: Sequence[LineIn], accounts: dict[str, AccountIn], policy: CostPolicy
) -> list[JobCostLine]:
    out = []
    for ln in lines:
        account = account_of(ln, accounts)
        if part_of(ln, account, policy) not in ("on_jobs", "memo"):
            continue
        assert ln.job_id is not None
        out.append(
            JobCostLine(
                GL_DIRECT,
                ln.job_id,
                ln.txn_date,
                ln.account_key,
                account.slot,
                ln.amount,
                f"{ln.txn_type} {ln.txn_external_id} line {ln.line_no}",
            )
        )
    return out
