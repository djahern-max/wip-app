"""F09 (Plan answer 3; D-46): every code in scope has a registry row; one constructed
``Issue`` per code maps to its identity key from the detail the generator returns, a
reworded sentence or a changed amount maps to the same key, and the stated fields are
the ones answer B compares. Pure: no database."""

from datetime import date
from decimal import Decimal

import pytest

from app.domain.billing.figures import words
from app.domain.estimates import exceptions as est
from app.domain.exceptions import registry
from app.domain.exceptions.models import SEVERITIES, SEVERITY_LABELS
from app.domain.jobs import issues
from app.domain.jobs.issues import JobState, LedgerRow

ESTIMATE_SET = {
    "EST_UNKNOWN_STATUS",
    "EST_ZERO_SOLD",
    "EST_UNIT_PRICED",
    "EST_NO_CATEGORY_SPLIT",
    "EST_PRICE_MISMATCH",
    "EST_DEDUCTIVE_CHANGE",
    "EST_WORK_AREA_RENUMBERED",
    "EST_UNKNOWN_COST_CODE",
    "EST_COST_LINE_ON_OMITTED",
    "EST_BURDEN_LINE",
    "EST_NO_BURDEN_RATE",
    "EST_NO_BURDEN_DATE",
}
JOB_AND_LINK_SET = {
    "EST_UNATTACHED",
    "JOB_SECOND_ESTIMATE_FOR_CUSTOMER",
    "JOB_NO_LEDGER_LINK",
    "LEDGER_PROJECT_NO_JOB",
    "JOB_DIVISION_UNSET",
    "CO_APPROVAL_NOT_CARRIED",
}
BILLING_SET = {
    "PAYMENT_UNAPPLIED",
    "DEPOSIT_NOT_IDENTIFIED",
    "BILLED_OVER_CONTRACT",
    "PAYMENT_OTHER_CREDIT",
    "BILLING_UNAPPROVED_CO",
    "PAYAPP_NOT_INVOICED",
    "INVOICE_NO_PAYAPP",
    "PAYAPP_INVOICE_MISMATCH",
}
NOT_QUEUED = {
    "BILLING_OVER_100",
    "BILLING_UNPRICED_CO",
    "BILLING_OMITTED_AREA",
    "BILLING_NEGATIVE",  # the owner's answer D (D-26 narrowed)
    "CUSTOMER_FUZZY",  # the Customers page's list (D-37)
    "EST_NO_ID",  # on the import batch
}


def test_every_code_in_scope_has_one_row_and_nothing_else_is_queued() -> None:
    assert registry.CODES == ESTIMATE_SET | JOB_AND_LINK_SET | BILLING_SET
    assert registry.CODES.isdisjoint(NOT_QUEUED)
    # Every sentence a generator in scope can produce has a row (the sentence tables).
    assert ESTIMATE_SET | {"EST_NO_ID"} == set(est.SENTENCES)
    assert JOB_AND_LINK_SET | BILLING_SET | NOT_QUEUED - {"EST_NO_ID"} == set(issues.SENTENCES)
    for s in registry.SPECS:
        assert s.severity in SEVERITIES and s.subject in ("job", "estimate", "customer")
    assert set(SEVERITY_LABELS) == set(SEVERITIES)


def test_severities_are_section_10s_and_warn_for_the_ten_codes_it_did_not_list() -> None:
    listed = {s.code: s.severity for s in registry.SPECS if s.listed}
    assert listed == {
        "EST_ZERO_SOLD": "warn",
        "EST_NO_CATEGORY_SPLIT": "warn",  # block-close on a fixed-price job (severity_of)
        "EST_UNATTACHED": "block_close",
        "JOB_SECOND_ESTIMATE_FOR_CUSTOMER": "warn",
        "JOB_NO_LEDGER_LINK": "block_close",
        "LEDGER_PROJECT_NO_JOB": "block_close",
        "JOB_DIVISION_UNSET": "warn",
        "CO_APPROVAL_NOT_CARRIED": "warn",
        "PAYMENT_UNAPPLIED": "warn",
        "DEPOSIT_NOT_IDENTIFIED": "warn",
        "BILLED_OVER_CONTRACT": "warn",
        "PAYMENT_OTHER_CREDIT": "warn",
        "BILLING_UNAPPROVED_CO": "warn",
        "PAYAPP_NOT_INVOICED": "warn",
        "INVOICE_NO_PAYAPP": "warn",
        "PAYAPP_INVOICE_MISMATCH": "warn",
    }
    added = {s.code for s in registry.SPECS if not s.listed}
    assert len(added) == 10 and all(registry.spec(c).severity == "warn" for c in added)
    # The owner, 2026-10-08 (§10, D-24): block-close when the estimate is on a fixed-price job.
    assert registry.severity_of("EST_NO_CATEGORY_SPLIT", fixed_price=True) == "block_close"
    assert registry.severity_of("EST_NO_CATEGORY_SPLIT", fixed_price=False) == "warn"
    assert registry.severity_of("EST_NO_CATEGORY_SPLIT") == "warn"  # on no job
    assert registry.severity_of("EST_UNATTACHED", fixed_price=True) == "block_close"
    assert registry.may_dismiss("warn") and registry.may_dismiss("info")
    assert not registry.may_dismiss("block_close")


def _issue(code: str, **detail) -> est.Issue:
    return est.Issue(code, "a sentence", detail)


@pytest.mark.parametrize(
    ("code", "detail", "key"),
    [
        ("EST_UNIT_PRICED", {"order": 18, "name": "CO: Ledge Removal per Day"}, "18"),
        ("EST_DEDUCTIVE_CHANGE", {"order": 7, "price": "100.00"}, "7"),
        ("EST_UNKNOWN_COST_CODE", {"order": 3, "cost_code": "999"}, "3:999"),
        ("EST_COST_LINE_ON_OMITTED", {"order": 17, "amount": "5.00"}, "17"),
        ("EST_WORK_AREA_RENUMBERED", {"order": 2, "baseline_name": "a", "name": "b"}, "2"),
        ("EST_ZERO_SOLD", {"price": "0.00"}, ""),
        ("EST_NO_CATEGORY_SPLIT", {"orders": [1, 2]}, ""),
        ("CO_APPROVAL_NOT_CARRIED", {"approval": "abc", "order_no": 18}, "abc"),
        ("DEPOSIT_NOT_IDENTIFIED", {"billing_id": "b1", "reason": "r"}, "b1"),
        ("PAYMENT_OTHER_CREDIT", {"payment_id": "p1", "amount": "200.00"}, "p1"),
        ("PAYAPP_NOT_INVOICED", {"application": "a1", "number": 3}, "a1"),
        ("INVOICE_NO_PAYAPP", {"billing_id": "b2"}, "b2"),
        ("PAYAPP_INVOICE_MISMATCH", {"application": "a1", "difference": "1.00"}, "a1"),
        ("BILLING_UNAPPROVED_CO", {"amount": "49275.00", "work_areas": ["#18"]}, ""),
        ("PAYMENT_UNAPPLIED", {"amount": "100.00", "payments": 1}, ""),
        ("LEDGER_PROJECT_NO_JOB", {"customer_id": "c", "documents": 2}, ""),
    ],
)
def test_the_identity_key_comes_from_the_detail_and_never_from_the_sentence(
    code: str, detail: dict, key: str
) -> None:
    a = est.Issue(code, "one wording", detail)
    b = est.Issue(code, "another wording of the same fact", detail)
    assert registry.item_key(a) == key
    assert registry.identity("job", "j1", a) == registry.identity("job", "j1", b)
    assert registry.identity("job", "j1", a) == ("job", "j1", code, key)
    assert registry.identity("job", "j2", a) != registry.identity("job", "j1", a)


def test_a_changed_amount_keeps_the_identity_and_changes_what_is_stated() -> None:
    nine = _issue("BILLING_UNAPPROVED_CO", amount="49275.00", work_areas=["#18", "#22"])
    eight = _issue("BILLING_UNAPPROVED_CO", amount="43800.00", work_areas=["#22"])
    assert registry.identity("job", "j", nine) == registry.identity("job", "j", eight)
    assert registry.stated(nine.code, nine.detail) == {
        "amount": "49275.00",
        "work_areas": "#18, #22",
    }
    assert registry.stated(nine.code, nine.detail) != registry.stated(eight.code, eight.detail)
    # Elapsed time is not a statement: an older EST_UNATTACHED is the same exception, unchanged.
    today = date(2026, 10, 8)
    young = issues.unattached_issue("EST1", date(2026, 10, 1), today)
    old = issues.unattached_issue("EST1", date(2026, 9, 1), today)
    assert young.message != old.message
    assert registry.stated("EST_UNATTACHED", young.detail) == registry.stated(
        "EST_UNATTACHED", old.detail
    )
    assert registry.item_key(young) == ""


def test_the_generators_details_carry_every_key_and_stated_field() -> None:
    """The registry reads fields the generators already return (no generator changed)."""
    job = issues.job_issues(JobState(None, "J", "in_progress", None, 0))
    for i in job:
        registry.item_key(i)
        registry.stated(i.code, i.detail)
    row = LedgerRow(None, "1", "Row", "Project", 2, False, tracked=True)
    ledger = issues.ledger_issue(row)
    assert ledger is not None and registry.stated(ledger.code, ledger.detail) == {"documents": "2"}
    carried = issues.approval_not_carried_issue(
        order_no=18,
        name="CO",
        estimate="EST6115758",
        agreed_on=date(2026, 9, 14),
        approved=Decimal("5475.00"),
        who="PM",
        ended="a later version changed its price",
        approval_id=None,
    )
    assert registry.item_key(carried) == "None"  # the approval's id, as text
    assert registry.stated(carried.code, carried.detail) == {
        "approved_price": "5475.00",
        "agreed_on": "2026-09-14",
    }
    assert words(Decimal("5475.00")) == "5,475.00"
