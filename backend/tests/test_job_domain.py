"""F07: the pure job modules (names, suggest, contract, issues, duplicates). No
database. Customer names are synthetic (Client NN); estimate ids are the fixtures'."""

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.domain.jobs.contract import AreaIn, AttachedIn, job_contract
from app.domain.jobs.duplicates import NamedRow, duplicate_pairs
from app.domain.jobs.issues import JobState, LedgerRow, job_issues, ledger_issues, unattached_issue
from app.domain.jobs.names import (
    customer_name_key,
    estimate_id_digits,
    name_contains_estimate_number,
    name_starts_with_estimate_id,
    street_tokens,
)
from app.domain.jobs.suggest import (
    ADDRESS,
    BY_CLIENT_NAME,
    BY_NAME,
    CUSTOMER_NAME,
    CUSTOMER_NAME_AND_ADDRESS,
    ID_IN_NAME,
    SAME_CUSTOMER,
    CustomerRow,
    EstimateText,
    JobForMatch,
    attach_candidates,
    number_in_name,
    qbo_candidates,
    suggest_division,
)

D = Decimal


def _id() -> uuid.UUID:
    return uuid.uuid4()


# --- names ---------------------------------------------------------------------------


def test_estimate_id_in_a_quickbooks_name_with_or_without_the_prefix() -> None:
    assert estimate_id_digits("EST6115758") == "6115758"
    assert estimate_id_digits("6115758") == "6115758"
    assert estimate_id_digits("Mijal") is None
    for name in ("6611769 Client 05 - Site", "EST6611769 Client 05", "est6611769", "6611769"):
        assert name_starts_with_estimate_id(name, "6611769"), name
    for name in ("66117690 Client", "Client 05 6611769", "X6611769"):
        assert not name_starts_with_estimate_id(name, "6611769"), name
    # F08.2 (item 5): the number anywhere in the name, bounded by non-digits.
    for name in ("Est. 6611769 - Site", "Client 05 6611769", "X6611769", "Site (6611769)"):
        assert name_contains_estimate_number(name, "6611769"), name
    for name in ("66117690 Client", "16611769", "Client 05", "", None):
        assert not name_contains_estimate_number(name, "6611769"), name


def test_street_tokens_need_a_street_number() -> None:
    assert street_tokens("Client 26 - 378 E Dunbarton Rd") == {"378 dunbarton"}
    assert street_tokens("378 East Dunbarton Road-Enhancement") == {"378 dunbarton"}
    assert street_tokens("12 North Street") == {"12 street"}
    assert street_tokens("1701 Ocean Boulevard-Enhancement") == {"1701 ocean"}
    assert street_tokens("Landscape Projects 2026") == set()
    assert street_tokens("Old") == set()
    assert street_tokens(None) == set()


def test_customer_name_key_drops_the_generic_tokens() -> None:
    assert customer_name_key("Client 26") == ("26",)
    assert customer_name_key("client 26, LLC") == ("26",)
    assert customer_name_key("Client") is None
    assert customer_name_key("  ") is None
    assert customer_name_key("Client 05") != customer_name_key("Client 26")


# --- division --------------------------------------------------------------------------


def test_division_with_the_most_cost_and_nothing_on_a_tie() -> None:
    ls, ex = _id(), _id()
    assert suggest_division([(ex, D("10.00")), (ls, D("4.00")), (ex, D("1.00"))]) == ex
    assert suggest_division([(ex, D("5.00")), (ls, D("5.00"))]) is None
    assert suggest_division([]) is None
    assert suggest_division([(None, D("9.00"))]) is None


# --- QuickBooks candidates (the brief's synthetic customer set) ------------------------


def _customers() -> dict[str, CustomerRow]:
    c05, c26, c23, pool = _id(), _id(), _id(), _id()

    def row(ext, name, parent=None, parent_name=None, project=True, active=True, rid=None):
        return CustomerRow(rid or _id(), ext, name, parent, parent_name, project, active)

    return {
        "c05": row("1", "Client 05", project=False, rid=c05),
        "c26": row("2", "Client 26", project=False, rid=c26),
        "c23": row("3", "Client 23", project=False, rid=c23),
        "pool_customer": row("4", "Pool - Hydroseed", project=False, rid=pool),
        "elm": row("10", "6115758 Client 05 - 67 Elm St Parking Lot", c05, "Client 05"),
        "turley26": row("11", "EST6120638 Client 26 - Landscape Projects 2026", c26, "Client 26"),
        "dunbarton": row("12", "Client 26 - 378 E Dunbarton Rd", c26, "Client 26"),
        "ocean": row("13", "1701 Ocean Boulevard", c23, "Client 23"),
        "pool": row("14", "Pool - Hydroseed", pool, "Pool - Hydroseed"),
        "old": row("15", "Old", c26, "Client 26", project=False),
        "inactive": row("16", "6115758 Client 05 - old copy", c05, "Client 05", active=False),
        # F08.2 (item 5): the number not at the start; a top-level customer with the
        # number (outside the F07 scope); another number that contains the digits.
        "phase2": row("17", "Est. 6115758 - 67 Elm St Phase 2", c05, "Client 05"),
        "topcustomer": row("18", "Client 05 (6115758)", project=False),
        "other_number": row("19", "16115758 Client 05 - Lot", c05, "Client 05"),
    }


def test_elm_street_suggests_the_id_named_project_only() -> None:
    cs = _customers()
    got = qbo_candidates(
        [EstimateText("EST6115758", None, None, "67 Elm Street")], list(cs.values()), set()
    )
    assert [(c.row.display_name, c.reason) for c in got] == [
        ("6115758 Client 05 - 67 Elm St Parking Lot", ID_IN_NAME),
        ("Est. 6115758 - 67 Elm St Phase 2", number_in_name("6115758")),
    ]
    assert got[1].reason == "name contains estimate number 6115758" and got[0].rank < got[1].rank
    # Still the F07 scope (owner, 2026-10-06): a top-level customer is never suggested,
    # whatever its name; 16115758 is another number.
    assert not any(
        c.row.display_name in ("Client 05 (6115758)", "16115758 Client 05 - Lot") for c in got
    )


def test_turley_suggests_the_id_project_then_the_customer_name_rows() -> None:
    cs = _customers()
    got = qbo_candidates(
        [EstimateText("EST6120638", "Client 26", "Residence", "Landscape Projects 2026")],
        list(cs.values()),
        set(),
    )
    assert [(c.row.display_name, c.reason) for c in got] == [
        ("EST6120638 Client 26 - Landscape Projects 2026", ID_IN_NAME),
        ("Client 26 - 378 E Dunbarton Rd", CUSTOMER_NAME),
        ("Old", CUSTOMER_NAME),
    ]
    names = {c.row.display_name for c in got}
    assert "Pool - Hydroseed" not in names and "6115758 Client 05 - old copy" not in names


def test_name_and_address_rank_above_either_alone_and_aliased_rows_are_never_offered() -> None:
    cs = _customers()
    est = EstimateText("EST6366990", "Client 26", "Residence", "Enhancement")
    got = qbo_candidates([est], list(cs.values()), {cs["turley26"].external_id})
    assert [(c.row.display_name, c.reason) for c in got] == [
        ("Client 26 - 378 E Dunbarton Rd", CUSTOMER_NAME),
        ("Old", CUSTOMER_NAME),
    ]
    # "378 East Dunbarton" and "378 E Dunbarton" are one street (compass words skipped).
    est2 = EstimateText("EST6366990", "Client 26", None, "378 East Dunbarton Road-Enhancement")
    got2 = qbo_candidates([est2], list(cs.values()), set())
    assert (got2[0].row.display_name, got2[0].reason) == (
        "Client 26 - 378 E Dunbarton Rd",
        CUSTOMER_NAME_AND_ADDRESS,
    )
    assert all(got2[0].rank < c.rank for c in got2[1:])
    ocean = qbo_candidates(
        [EstimateText("EST6346291", "Client 23", "Residence", "1701 Ocean Boulevard-Enhancement")],
        list(cs.values()),
        set(),
    )
    assert [(c.row.display_name, c.reason) for c in ocean] == [
        ("1701 Ocean Boulevard", CUSTOMER_NAME_AND_ADDRESS)
    ]
    address_only = qbo_candidates(
        [EstimateText("EST1", None, "1701 Ocean Blvd", "x")], list(cs.values()), set()
    )
    assert [(c.row.display_name, c.reason) for c in address_only] == [
        ("1701 Ocean Boulevard", ADDRESS)
    ]


def test_two_equal_name_candidates_are_both_offered_and_nothing_is_chosen() -> None:
    parent = _id()
    a = CustomerRow(_id(), "a", "Client 40 - Front", parent, "Client 40", True, True)
    b = CustomerRow(_id(), "b", "Client 40 - Back", parent, "Client 40", True, True)
    got = qbo_candidates([EstimateText("EST9", "Client 40", None, "Work")], [a, b], set())
    assert [c.reason for c in got] == [CUSTOMER_NAME, CUSTOMER_NAME]
    assert {c.row.id for c in got} == {a.id, b.id}
    assert len({c.rank for c in got}) == 1


# --- attach candidates (D-03) ------------------------------------------------------------


def test_same_customer_by_id_by_client_name_and_by_name_only() -> None:
    cs = _customers()
    rows = list(cs.values())
    turley_job = JobForMatch(
        _id(),
        "Landscape Projects 2026",
        "fixed_price",
        cs["c26"].id,
        "Client 26",
        EstimateText("EST6120638", "Client 26", "Residence", "Landscape Projects 2026"),
    )
    pool_job = JobForMatch(_id(), "Pool - Hydroseed", "pool", None, None, None)
    est = EstimateText("EST6366990", "Client 26", "Residence", "378 East Dunbarton Road")
    got = attach_candidates(est, [turley_job, pool_job], rows)
    assert [(c.job.id, c.reasons) for c in got] == [(turley_job.id, (BY_CLIENT_NAME, BY_NAME))]
    assert got[0].same_customer
    # With a project named for EST6366990 under Client 26, the match is by id.
    named = CustomerRow(
        _id(), "99", "6366990 Client 26 - Dunbarton", cs["c26"].id, "Client 26", True, True
    )
    got2 = attach_candidates(est, [turley_job], [*rows, named])
    assert got2[0].reasons[0] == SAME_CUSTOMER
    # DeVellis: the same name text only.
    devellis = JobForMatch(
        _id(),
        "1701 Ocean Boulevard-Enhancement",
        "fixed_price",
        None,
        None,
        EstimateText("EST6346291", "Client 23", "Residence A", "1701 Ocean Boulevard-Enhancement"),
    )
    addon = EstimateText(
        "EST6281138", "Client 71", "Residence B", "1701 Ocean Boulevard-Enhancement"
    )
    got3 = attach_candidates(addon, [devellis, pool_job], rows)
    assert [(c.job.id, c.reasons, c.same_customer) for c in got3] == [
        (devellis.id, (BY_NAME,), False)
    ]


# --- contract (D-01, D-24, D-30; answers 5 and 7) ------------------------------------------


def _areas(*rows: tuple[int, bool, str, str | None]) -> tuple[AreaIn, ...]:
    return tuple(AreaIn(o, k, D(p), kind) for o, k, p, kind in rows)


def test_revised_contract_counts_only_confirmed_originals() -> None:
    areas = _areas((1, True, "100.00", None), (2, True, "50.00", None), (3, False, "9.00", None))
    c = job_contract("fixed_price", [AttachedIn("original", D("150.00"), areas, D("80.00"))])
    assert (c.revised_contract, c.unapproved_change_orders, c.to_confirm) == (
        D("0.00"),
        D("0.00"),
        2,
    )
    areas = _areas(
        (1, True, "100.00", "original"),
        (2, True, "50.00", "change_order"),
        (3, False, "9.00", None),
    )
    c = job_contract("fixed_price", [AttachedIn("original", D("150.00"), areas, D("80.00"))])
    assert (c.revised_contract, c.unapproved_change_orders, c.to_confirm) == (
        D("100.00"),
        D("50.00"),
        0,
    )
    assert c.eac_in_basis == D("80.00") and not c.from_header_price


def test_no_work_areas_counts_the_header_price_and_ignored_moves_nothing() -> None:
    c = job_contract(
        "fixed_price",
        [
            AttachedIn("original", D("39032.44"), None, None),
            AttachedIn("change_order", D("998.71"), None, None),
            AttachedIn("ignored", D("500.00"), None, D("1.00")),
        ],
    )
    assert c.revised_contract == D("39032.44") and c.from_header_price
    assert c.unapproved_change_orders == D("998.71")
    assert c.eac_in_basis is None and c.eac_not_computed


def test_eac_sums_attached_estimates_and_is_not_computed_when_one_is_not() -> None:
    orig = AttachedIn("original", D("10.00"), _areas((1, True, "10.00", None)), D("6.00"))
    co = AttachedIn("change_order", D("4.00"), _areas((1, True, "4.00", None)), D("3.00"))
    assert job_contract("fixed_price", [orig, co]).eac_in_basis == D("9.00")
    c = job_contract("fixed_price", [orig, co])
    assert c.unapproved_change_orders == D("4.00")
    broken = AttachedIn("change_order", D("4.00"), None, None)
    assert job_contract("fixed_price", [orig, broken]).eac_in_basis is None


def test_time_and_materials_and_pool() -> None:
    orig = AttachedIn("original", D("10.00"), _areas((1, True, "10.00", "original")), D("6.00"))
    tm = job_contract("time_and_materials", [orig])
    assert tm.revised_contract is None and tm.unapproved_change_orders is None
    assert tm.eac_in_basis == D("6.00")
    pool = job_contract("pool", [])
    assert (pool.revised_contract, pool.unapproved_change_orders, pool.eac_in_basis) == (
        None,
        None,
        None,
    )
    none = job_contract("fixed_price", [])
    assert none.revised_contract is None and not none.has_original
    # D-35: maintenance and snow programs are recognised as billed; no contract, no EAC.
    program = job_contract("recurring_service", [])
    assert (program.revised_contract, program.unapproved_change_orders, program.eac_in_basis) == (
        None,
        None,
        None,
    )


# --- review items ------------------------------------------------------------------------


def test_job_items_and_ledger_items() -> None:
    open_job = JobState(_id(), "Job A", "in_progress", None, 0)
    assert [i.code for i in job_issues(open_job)] == ["JOB_DIVISION_UNSET", "JOB_NO_LEDGER_LINK"]
    linked = JobState(_id(), "Job C", "in_progress", _id(), 2)
    assert job_issues(linked) == []

    # D-37 (F07.2): tracked, active rows with no job raise, documents or not; an
    # untracked row with documents, a linked row and an inactive tracked row never do.
    rows = [
        LedgerRow(_id(), "13", "1701 Ocean Boulevard", "Project", 1, False, tracked=True),
        LedgerRow(_id(), "10", "6115758 Client 05", "Project", 0, False, tracked=True),
        LedgerRow(_id(), "15", "Old", "Sub-customer", 3, True, tracked=True),
        LedgerRow(_id(), "16", "Untracked with money", "Sub-customer", 4, False),
        LedgerRow(_id(), "17", "Gone", "Customer", 0, False, tracked=True, active=False),
    ]
    issues = ledger_issues(rows)
    assert [i.code for i in issues] == ["LEDGER_PROJECT_NO_JOB", "LEDGER_PROJECT_NO_JOB"]
    assert (
        "1701 Ocean Boulevard" in issues[0].message
        and "is tracked and has no job (1 billing or payment document)." in issues[0].message
    )
    assert '"6115758 Client 05" is tracked and has no job. Link' in issues[1].message
    assert issues[1].detail["documents"] == 0
    u = unattached_issue("EST6115758", date(2026, 9, 20), date(2026, 9, 29))
    assert u.code == "EST_UNATTACHED" and "9 days ago" in u.message and u.detail["age_days"] == 9
    for issue in (*issues, u, *job_issues(open_job)):
        assert issue.code not in issue.message


def test_no_ledger_link_is_raised_from_in_progress_never_for_sold() -> None:
    """D-35: a project is created when the first money moves; a sold job with no link is
    backlog, not a problem. In progress and substantially complete need one; closed and
    cancelled never warn (owner, 2026-09-29)."""
    raised = {
        status: [i.code for i in job_issues(JobState(_id(), "Job", status, _id(), 0))]
        for status in ("sold", "in_progress", "substantially_complete", "closed", "cancelled")
    }
    assert raised == {
        "sold": [],
        "in_progress": ["JOB_NO_LEDGER_LINK"],
        "substantially_complete": ["JOB_NO_LEDGER_LINK"],
        "closed": [],  # finished before go-live: no project, no warning
        "cancelled": [],
    }
    (issue,) = job_issues(JobState(_id(), "Job", "in_progress", _id(), 0))
    assert "In progress" in issue.message and "D-35" in issue.message


# --- duplicates ------------------------------------------------------------------------


def test_duplicate_pairs_on_synthetic_names() -> None:
    a = NamedRow(_id(), "Client 31, Pat & Sam")
    b = NamedRow(_id(), "Client 31 Pat and Sam")
    c = NamedRow(_id(), "Client 05")
    d = NamedRow(_id(), "Client 26")
    e = NamedRow(_id(), "Client 26 Pat")  # a first name added to a key with no word
    f = NamedRow(_id(), "Mowbray Farms LLC")
    g = NamedRow(_id(), "Mowbray Farms")
    h = NamedRow(_id(), "Dana Mowbray Farms")
    pairs = {frozenset((x.id, y.id)) for x, y in duplicate_pairs([a, b, c, d, e, f, g, h])}
    assert frozenset((a.id, b.id)) in pairs
    assert frozenset((c.id, d.id)) not in pairs
    assert frozenset((d.id, e.id)) not in pairs  # "26" alone has no word of 3+ letters
    assert frozenset((f.id, g.id)) in pairs  # a suffix removed
    assert frozenset((g.id, h.id)) in pairs and frozenset((f.id, h.id)) in pairs
    assert len(pairs) == 4


# --- money hygiene --------------------------------------------------------------------------


def test_no_float_on_the_contract_path() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "domain" / "jobs"
    for p in root.glob("*.py"):
        src = p.read_text()
        assert "float(" not in src, p.name
        assert ": float" not in src, p.name
