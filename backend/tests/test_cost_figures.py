"""F10: the pure cost figures (``app/domain/cost/figures.py``) and the two generators, on
constructed lines. No database. Every amount is a ``Decimal``; nothing rounds."""

from datetime import date
from decimal import Decimal

from app.domain.cost import figures
from app.domain.cost.exceptions import division_mismatch_issue, unassigned_issue
from app.domain.cost.figures import (
    AccountIn,
    CostPolicy,
    LineIn,
    job_cost,
    job_cost_lines,
    labor_in_ledger,
    part_of,
    tie_out,
    tie_out_status,
    unassigned_documents,
)

D = Decimal
BASIS = frozenset({"10", "20", "30", "35", "40", "50", "60", "70", "90"})  # D-05
CATEGORIES = (
    ("10", "Labor"),
    ("20", "Labor Burden"),
    ("30", "Materials"),
    ("35", "Supplies"),
    ("40", "Subcontractors"),
    ("45", "Equipment (owned)"),
    ("55", "Equipment Maintenance"),
    ("65", "Fuel"),
)
POLICY = CostPolicy(BASIS, CATEGORIES)
EX, LS = ("d-ex", "EX", "Excavation"), ("d-ls", "LS", "Landscaping")


def account(key, no, name, slot, division=EX, *, mapped=True, in_job_cost=True, in_chart=True):
    return AccountIn(
        key=key,
        gl_account_id=f"gl-{no}" if in_chart else None,
        account_no=no if in_chart else None,
        name=name if in_chart else None,
        in_chart=in_chart,
        mapped=mapped,
        in_job_cost=in_job_cost,
        slot=slot if mapped else None,
        category_name=dict(CATEGORIES).get(slot) if mapped else None,
        division_id=division[0] if mapped and division else None,
        division_code=division[1] if mapped and division else None,
        division_name=division[2] if mapped and division else None,
        quickbooks_words=None if in_chart else "5900 Legacy COGS",
    )


ACCOUNTS = {
    "304": account("304", "5240", "Subcontractors - EX", "40"),
    "303": account("303", "5230", "Materials - EX", "30"),
    "355": account(
        "355", "5355", "Equipment Maintenance - GC", "55", ("d-gc", "GC", "Grounds Care")
    ),
    "302": account("302", "5210", "Gross Payroll - EX", "10"),
    "610": account("610", "5610", "On-Road Diesel", "65", None, in_job_cost=False),
    "999": account("999", None, None, None, None, mapped=False, in_chart=False),
    "888": account("888", "5290", "Unmapped - EX", None, None, mapped=False),
}


def line(n, txn_type, doc, day, key, amount, job=None, customer=None, *, deleted=False, line_no=1):
    return LineIn(
        id=f"l{n}",
        txn_type=txn_type,
        txn_external_id=doc,
        line_external_id=str(line_no),
        line_no=line_no,
        doc_number=f"N-{doc}",
        txn_date=date.fromisoformat(day),
        account_key=key,
        vendor_external_id="7001",
        customer_external_id=customer,
        job_id=job,
        amount=D(amount),
        description=None,
        memo=None,
        deleted=deleted,
    )


LINES = [
    line(1, "Bill", "1", "2026-09-04", "304", "1250.00", "j1", "201"),
    line(2, "Bill", "1", "2026-09-04", "303", "480.50", "j1", "201", line_no=2),
    line(3, "Bill", "2", "2026-09-10", "304", "2000.00"),  # no customer: unassigned
    line(4, "VendorCredit", "3", "2026-09-12", "304", "-150.00", "j1", "201"),
    line(5, "JournalEntry", "4", "2026-09-30", "302", "9800.00"),  # labor in the ledger
    line(6, "Purchase", "5", "2026-10-01", "355", "300.00", "j1", "201"),  # memo: slot 55 is out
    line(7, "Purchase", "6", "2026-10-02", "610", "120.00", "j1", "201"),  # fuel: outside job cost
    line(8, "Bill", "7", "2026-10-03", "", "90.00", "j1", "201"),  # no account
    line(9, "Bill", "8", "2026-10-04", "999", "75.00", "j1", "201"),  # not in the chart
    line(10, "Bill", "9", "2026-10-05", "304", "500.00", "j1", "201", deleted=True),
    line(11, "Bill", "10", "2026-10-09", "304", "4000.00", "j2", "202"),  # EX account on an LS job
    line(12, "Bill", "11", "2026-10-10", "304", "60.00", None, "777"),  # a customer with no job
    line(13, "Bill", "11", "2026-10-10", "303", "40.00", None, "777", line_no=2),
    line(14, "JournalEntry", "12", "2026-10-11", "302", "500.00", "j1", "201"),  # labor, on a job
    line(15, "Bill", "13", "2026-10-12", "888", "33.00", "j1", "201"),  # unmapped account
]


def test_every_live_line_is_one_part_and_labor_and_outside_never_become_unassigned() -> None:
    parts = {ln.id: part_of(ln, figures.account_of(ln, ACCOUNTS), POLICY) for ln in LINES}
    assert parts == {
        "l1": "on_jobs",
        "l2": "on_jobs",
        "l3": "unassigned",
        "l4": "on_jobs",
        "l5": "labor",
        "l6": "memo",
        "l7": "outside",
        "l8": "outside",
        "l9": "outside",
        "l10": None,
        "l11": "on_jobs",
        "l12": "unassigned",
        "l13": "unassigned",
        "l14": "labor",  # a labor-slot line on a job is still labor in the ledger (§2.1)
        "l15": "outside",
    }
    # With the basis undecided, every job-cost line on a job is on jobs; nothing is memo.
    undecided = CostPolicy(None, CATEGORIES)
    assert part_of(LINES[5], ACCOUNTS["355"], undecided) == "on_jobs"


def test_cost_to_date_by_category_in_the_basis_and_memo_as_of_a_day() -> None:
    mine = [ln for ln in LINES if ln.job_id == "j1"]
    cost = job_cost(mine, ACCOUNTS, POLICY, as_of=date(2026, 10, 31))
    by_slot = {c.slot: c.amount for c in cost.by_category}
    assert by_slot["40"] == D("1100.00")  # 1,250.00 − 150.00; the deleted 500.00 is out
    assert by_slot["30"] == D("480.50") and by_slot["55"] == D("300.00")
    assert by_slot["10"] == D("0.00")  # labor never reaches a job from the ledger
    assert by_slot["65"] == D("0.00")  # 5610 is not job cost
    assert cost.in_basis == D("1580.50") and cost.memo == D("300.00")
    assert cost.lines == 4 and cost.basis_decided
    # As of September: the October memo line is not yet counted.
    sept = job_cost(mine, ACCOUNTS, POLICY, as_of=date(2026, 9, 30))
    assert sept.in_basis == D("1580.50") and sept.memo == D("0.00")
    # No line at all: 0.00, never "not computed"; an undecided basis: not computed.
    empty = job_cost([], ACCOUNTS, POLICY, as_of=date(2026, 10, 31))
    assert empty.in_basis == D("0.00") and empty.memo == D("0.00") and empty.lines == 0
    undecided = job_cost(mine, ACCOUNTS, CostPolicy(None, CATEGORIES), as_of=date(2026, 10, 31))
    assert undecided.in_basis is None and not undecided.basis_decided
    assert all(c.in_basis is None for c in undecided.by_category)


def test_the_five_parts_sum_to_the_ledger_total_of_every_month_and_account() -> None:
    rows = tie_out(LINES, ACCOUNTS, POLICY)
    assert all(r.balanced for r in rows)
    cells = {(r.month, r.account.key): r for r in rows}
    sept = cells[("2026-09", "304")]
    assert (sept.on_jobs, sept.unassigned, sept.total) == (D("1100.00"), D("2000.00"), D("3100.00"))
    assert cells[("2026-09", "302")].labor == D("9800.00")
    assert cells[("2026-10", "355")].memo == D("300.00")
    assert cells[("2026-10", "610")].outside == D("120.00")
    assert (
        cells[("2026-10", "")].outside == D("90.00")
        and cells[("2026-10", "")].account.label == "No account"
    )
    assert cells[("2026-10", "999")].account.label == "Not in the chart: 5900 Legacy COGS"
    assert cells[("2026-10", "888")].outside == D("33.00")
    oct_sub = cells[("2026-10", "304")]
    assert (oct_sub.on_jobs, oct_sub.unassigned, oct_sub.total) == (
        D("4000.00"),
        D("60.00"),
        D("4060.00"),
    )
    # Chart accounts by number first, then the rest; months oldest first.
    assert [r.account.key for r in rows if r.month == "2026-10"] == [
        "302",  # 5210
        "303",  # 5230
        "304",  # 5240
        "888",  # 5290
        "355",  # 5355
        "610",  # 5610
        "",  # no account
        "999",  # not in the chart
    ]
    assert tie_out_status(rows, skipped=0).startswith(
        "Ties to the cent for 2 months and 8 accounts; every payload read"
    )
    assert tie_out_status(rows, skipped=3).endswith("3 payloads could not be read.")
    assert tie_out_status([], skipped=0) == "Nothing to tie out yet: no cost lines held."


def test_unassigned_is_by_document_and_customer_reference_with_its_sentence() -> None:
    docs = unassigned_documents(LINES, ACCOUNTS, POLICY)
    assert [(d.txn_external_id, d.customer_external_id, d.amount, d.lines) for d in docs] == [
        ("2", None, D("2000.00"), 1),
        ("11", "777", D("100.00"), 2),
    ]
    assert docs[1].account_labels == ("5240 Subcontractors - EX", "5230 Materials - EX")
    none = unassigned_issue(docs[0], None, False)
    assert none.code == "COST_UNASSIGNED"
    assert (
        none.message == "Bill N-2, 2,000.00 to 5240 Subcontractors - EX, is coded to no customer."
    )
    assert none.detail["amount"] == "2000.00" and none.detail["account_no"] == "5240"
    named = unassigned_issue(docs[1], "Hess Residence", True)
    assert named.message == (
        "Bill N-11, 100.00 to 5240 Subcontractors - EX and 5230 Materials - EX, is coded to "
        '"Hess Residence", which has no job.'
    )
    assert named.detail["account_no"] == "5240, 5230"
    unknown = unassigned_issue(docs[1], None, True)
    assert unknown.message.endswith("is coded to QuickBooks customer 777, which has no job.")


def test_labor_in_the_ledger_by_month_and_division() -> None:
    rows = labor_in_ledger(LINES, ACCOUNTS, POLICY)
    assert [(r.month, r.division_code, r.amount) for r in rows] == [
        ("2026-09", "EX", D("9800.00")),
        ("2026-10", "EX", D("500.00")),
    ]


def test_division_mismatch_is_per_document_and_job_and_never_on_a_labor_line() -> None:
    jobs = {"j1": ("d-ex", "Excavation"), "j2": ("d-ls", "Landscaping"), "j3": (None, "")}
    found = figures.division_mismatches(LINES, ACCOUNTS, POLICY, jobs)
    # The Grounds Care maintenance line on the Excavation job is a mismatch too, memo or not.
    assert sorted((m.txn_external_id, m.job_id, m.amount) for m in found) == [
        ("10", "j2", D("4000.00")),
        ("5", "j1", D("300.00")),
    ]
    issue = division_mismatch_issue(next(m for m in found if m.txn_external_id == "10"))
    assert issue.code == "COST_DIVISION_MISMATCH"
    assert issue.message == (
        "Bill N-10 posts 4,000.00 to 5240 Subcontractors - EX (Excavation) on a Landscaping job."
    )
    assert issue.detail == {
        "amount": "4000.00",
        "account_no": "5240",
        "txn_type": "Bill",
        "txn_external_id": "10",
        "document": "N-10",
        "job_id": "j2",
    }
    # A labor line on a job of another division raises nothing; a job with no division neither.
    extra = [
        line(20, "JournalEntry", "20", "2026-10-11", "302", "500.00", "j2", "202"),
        line(21, "Bill", "21", "2026-10-11", "304", "10.00", "j3", "203"),
    ]
    assert figures.division_mismatches(extra, ACCOUNTS, POLICY, jobs) == []
    # A pool job is exempt only when the account's division is its own (D-30): the same rule.
    pool = {"p1": ("d-ls", "Landscaping")}
    same = [line(30, "Bill", "30", "2026-09-18", "305", "100.00", "p1", "205")]
    accounts = {**ACCOUNTS, "305": account("305", "5135", "Supplies - LS", "35", LS)}
    assert figures.division_mismatches(same, accounts, POLICY, pool) == []
    other = [line(31, "Bill", "31", "2026-09-18", "304", "100.00", "p1", "205")]
    assert len(figures.division_mismatches(other, accounts, POLICY, pool)) == 1


def test_job_cost_lines_is_the_gl_direct_union_over_the_lines_on_jobs() -> None:
    rows = job_cost_lines(LINES, ACCOUNTS, POLICY)
    assert all(r.basis == "gl_direct" for r in rows)
    assert sorted(r.source_ref for r in rows) == sorted(
        [
            "Bill 1 line 1",
            "Bill 1 line 2",
            "VendorCredit 3 line 1",
            "Purchase 5 line 1",
            "Bill 10 line 1",
        ]
    )
    assert sum((r.amount for r in rows if r.job_id == "j1"), D("0.00")) == D("1880.50")
