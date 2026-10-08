"""Every code in scope of the queue (F09; BLUEPRINT §10; D-46): its severity, what it
is about, the key that tells two exceptions of one code on one subject apart, and the
detail fields its sentence *states* (the owner's answer B, 2026-10-08: a dismissed
exception opens again when those change). Pure; the generators are not touched: the key
and the stated fields are read from the ``Issue.detail`` they already return.

The identity of an exception is ``(subject_type, subject_id, code, item_key)``; the
sentence text is never part of it, so a reworded sentence or a changed amount never
makes a second row. Elapsed time, document counts and names quoted for orientation are
not stated unless listed here.

Severities: §10's where it has a row; the ten estimate codes §10 did not list are
``warn`` (the owner, 2026-10-08; F16's brief decides which codes block a close).
``EST_NO_CATEGORY_SPLIT`` (§10's ``EST_NO_COST``) is ``block_close`` when the estimate
is attached to a ``fixed_price`` job and ``warn`` otherwise (the owner, 2026-10-08;
§10, D-24): the run tells ``severity_of`` the job's revenue method.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.domain.estimates.exceptions import Issue

Key = tuple[str, str, str, str]  # (subject_type, subject_id, code, item_key)

JOB, ESTIMATE, CUSTOMER = "job", "estimate", "customer"
INFO, WARN, BLOCK = "info", "warn", "block_close"
# The one code whose severity depends on the job: block-close on a fixed-price job.
FIXED_PRICE_BLOCKS = "EST_NO_CATEGORY_SPLIT"


def _one(_detail: dict) -> str:
    return ""


def _field(name: str) -> Callable[[dict], str]:
    def key(detail: dict) -> str:
        return str(detail.get(name, ""))

    return key


def _order_and_code(detail: dict) -> str:
    return f"{detail.get('order', '')}:{detail.get('cost_code', '')}"


@dataclass(frozen=True)
class CodeSpec:
    code: str
    subject: str  # job | estimate | customer
    severity: str  # info | warn | block_close
    key: Callable[[dict], str]  # item_key from the generator's detail
    stated: tuple[str, ...]  # the detail fields the sentence states (answer B)
    listed: bool  # True: the severity is §10's row; False: added by F09 (warn)


SPECS: tuple[CodeSpec, ...] = (
    # --- the estimate set (app/domain/estimates/exceptions.py) --------------------------
    CodeSpec("EST_UNKNOWN_STATUS", ESTIMATE, WARN, _one, ("status",), False),
    CodeSpec("EST_ZERO_SOLD", ESTIMATE, WARN, _one, (), True),
    CodeSpec("EST_UNIT_PRICED", ESTIMATE, WARN, _field("order"), ("name",), False),
    CodeSpec("EST_NO_CATEGORY_SPLIT", ESTIMATE, WARN, _one, ("orders",), True),
    CodeSpec("EST_PRICE_MISMATCH", ESTIMATE, WARN, _one, ("kept_total", "price"), False),
    CodeSpec("EST_DEDUCTIVE_CHANGE", ESTIMATE, WARN, _field("order"), ("price",), False),
    CodeSpec(
        "EST_WORK_AREA_RENUMBERED",
        ESTIMATE,
        WARN,
        _field("order"),
        ("baseline_name", "name"),
        False,
    ),
    CodeSpec("EST_UNKNOWN_COST_CODE", ESTIMATE, WARN, _order_and_code, (), False),
    CodeSpec("EST_COST_LINE_ON_OMITTED", ESTIMATE, WARN, _field("order"), ("amount",), False),
    CodeSpec("EST_BURDEN_LINE", ESTIMATE, WARN, _one, ("orders",), False),
    CodeSpec("EST_NO_BURDEN_RATE", ESTIMATE, WARN, _one, ("divisions", "date"), False),
    CodeSpec("EST_NO_BURDEN_DATE", ESTIMATE, WARN, _one, (), False),
    # --- the job and link set (app/domain/jobs/issues.py) --------------------------------
    CodeSpec("EST_UNATTACHED", ESTIMATE, BLOCK, _one, (), True),
    CodeSpec("JOB_SECOND_ESTIMATE_FOR_CUSTOMER", ESTIMATE, WARN, _one, ("candidates",), True),
    CodeSpec("JOB_NO_LEDGER_LINK", JOB, BLOCK, _one, ("status",), True),
    CodeSpec("LEDGER_PROJECT_NO_JOB", CUSTOMER, BLOCK, _one, ("documents",), True),
    CodeSpec("JOB_DIVISION_UNSET", JOB, WARN, _one, (), True),
    CodeSpec(
        "CO_APPROVAL_NOT_CARRIED",
        JOB,
        WARN,
        _field("approval"),
        ("approved_price", "agreed_on"),
        True,
    ),
    # --- the billing set (issues.py and app/domain/billing/pay_applications.py) ----------
    CodeSpec("PAYMENT_UNAPPLIED", JOB, WARN, _one, ("amount", "payments"), True),
    CodeSpec("DEPOSIT_NOT_IDENTIFIED", JOB, WARN, _field("billing_id"), ("reason",), True),
    CodeSpec("BILLED_OVER_CONTRACT", JOB, WARN, _one, ("over",), True),
    CodeSpec("PAYMENT_OTHER_CREDIT", JOB, WARN, _field("payment_id"), ("amount", "date"), True),
    CodeSpec("BILLING_UNAPPROVED_CO", JOB, WARN, _one, ("amount", "work_areas"), True),
    # The tie sentences' details carry the ids and, for the mismatch, the amount difference;
    # the amount due and the surcharge figures are in the text only (generators unchanged;
    # Discovered).
    CodeSpec("PAYAPP_NOT_INVOICED", JOB, WARN, _field("application"), (), True),
    CodeSpec("INVOICE_NO_PAYAPP", JOB, WARN, _field("billing_id"), (), True),
    CodeSpec("PAYAPP_INVOICE_MISMATCH", JOB, WARN, _field("application"), ("difference",), True),
)

BY_CODE: dict[str, CodeSpec] = {s.code: s for s in SPECS}
CODES: frozenset[str] = frozenset(BY_CODE)


def spec(code: str) -> CodeSpec:
    return BY_CODE[code]


def item_key(issue: Issue) -> str:
    return BY_CODE[issue.code].key(issue.detail)[:120]


def identity(subject_type: str, subject_id: str, issue: Issue) -> Key:
    return (subject_type, subject_id, issue.code, item_key(issue))


def stated(code: str, detail: dict) -> dict[str, str]:
    """The stated fields of a detail, as strings, for the answer-B comparison; a field
    the generator did not supply reads as ''."""
    return {name: _text(detail.get(name)) for name in BY_CODE[code].stated}


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ", ".join(_text(v) for v in value)
    return str(value)


def severity_of(code: str, *, fixed_price: bool | None = None) -> str:
    """§10's severity; for ``EST_NO_CATEGORY_SPLIT`` block-close only when the estimate's
    job is ``fixed_price`` (``fixed_price`` is None for an estimate on no job)."""
    s = BY_CODE[code]
    if code == FIXED_PRICE_BLOCKS:
        return BLOCK if fixed_price else WARN
    return s.severity


def may_dismiss(severity: str) -> bool:
    """The owner's answer A (D-46): a block-close exception cannot be dismissed."""
    return severity != BLOCK
