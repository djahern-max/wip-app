"""Home (F07.3): what to do next, as pure functions. No DB, no clock: the service hands
in facts and these decide the lines. Two ordered lists are the data: ``SETUP_STEPS``
(the six set-up lines, in the brief's order as the owner set it on 2026-10-04) and
``JOB_NEEDS`` (what a job needs next, first rule that applies). F08 onward append a
rule, its sentence and a test; nothing here is rewritten. D-22 throughout: sentences,
never codes; links only to pages the person's role can open (the facts say which).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.domain.config.policy import TIMEZONE, WIP_BASIS
from app.domain.estimates.totals import BURDEN_SLOT

# The policy keys a feature already reads. Each feature that starts reading a key adds
# it here (owner, 2026-10-04): F07 reads the time zone (Sold on, the estimate dates); F06
# reads the WIP basis (EAC in the basis). The other keys are decided when their features
# arrive and never block a first job. The names come from the registry's constants: a key
# is a string literal only in ``app/domain/config/policy.py`` (tests/test_policy.py).
REQUIRED_POLICY_KEYS: tuple[str, ...] = (TIMEZONE, WIP_BASIS)

LISTED_JOB_STATUSES = frozenset({"sold", "in_progress", "substantially_complete"})


@dataclass(frozen=True)
class Link:
    page: str  # connections | imports | config | jobs | customers | estimates
    section: str | None = None  # config: accounts | divisions | burden | policy
    job_id: str | None = None  # jobs: open this job
    review: bool = False  # jobs: open the review queue


@dataclass(frozen=True)
class Line:
    code: str
    label: str
    done: bool
    message: str
    link: Link | None = None
    note: str | None = None
    count: int | None = None  # the number in the sentence, for tests and pairing
    total: int | None = None
    primary: bool = False


@dataclass(frozen=True)
class SetupFacts:
    # QuickBooks (``GET /api/qbo/status``): the connection row's status, or None when the
    # company has no connection; the attention codes and their sentences from copy_status.
    connection_status: str | None
    attention: tuple[tuple[str, str], ...]  # (code, detail)
    active_rules: int
    active_accounts: int
    unmapped_accounts: int  # active accounts without a confirmed mapping (the Accounts page)
    suggested_accounts: int
    all_policy_keys: tuple[str, ...]
    decided_keys: frozenset[str]
    policy_labels: dict[str, str]
    wip_basis: frozenset[str] | None  # None: undecided
    divisions_with_digit: tuple[str, ...]  # active division codes, grid order
    divisions_without_rate: tuple[str, ...]  # of those, no rate in force on the tenant's today
    can_view_connections: bool = True
    can_view_config: bool = True


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def quickbooks(f: SetupFacts) -> Line | None:
    if not f.can_view_connections:
        return None
    label = "QuickBooks"
    link = Link("connections")
    if f.connection_status is None or f.connection_status == "disconnected":
        return Line("quickbooks", label, False, "Connect to QuickBooks.", link)
    codes = dict(f.attention)
    if "needs_reconnect" in codes:
        return Line("quickbooks", label, False, codes["needs_reconnect"], link)
    if "backfill_needed" in codes:
        return Line(
            "quickbooks",
            label,
            False,
            f"{codes['backfill_needed']} Run a backfill on Connections.",
            link,
        )
    if f.connection_status != "connected":
        return Line(
            "quickbooks",
            label,
            False,
            "The QuickBooks connection needs attention on Connections.",
            link,
        )
    return Line(
        "quickbooks", label, True, "QuickBooks is connected and a backfill has succeeded.", link
    )


def suggestion_rules(f: SetupFacts) -> Line:
    label = "Suggestion rules"
    if f.active_rules == 0:
        return Line(
            "suggestion_rules",
            label,
            False,
            "No suggestion rules are loaded. The firm loads them from the company's rules "
            "file (OPERATIONS, Editing suggestion rules); there is no screen for this.",
            None,
            count=0,
        )
    n = f.active_rules
    return Line(
        "suggestion_rules",
        label,
        True,
        f"{n} suggestion {_plural(n, 'rule is', 'rules are')} loaded.",
        None,
        count=n,
    )


def chart_of_accounts(f: SetupFacts) -> Line:
    label = "Chart of accounts"
    if f.active_accounts == 0:
        return Line(
            "chart_of_accounts",
            label,
            False,
            "Upload the chart of accounts.",
            Link("imports"),
            count=0,
        )
    n = f.active_accounts
    return Line(
        "chart_of_accounts",
        label,
        True,
        f"{n} active {_plural(n, 'account is', 'accounts are')} in the chart.",
        Link("imports"),
        count=n,
    )


def account_mapping(f: SetupFacts) -> Line:
    label = "Account mapping"
    link = Link("config", "accounts")
    if f.active_accounts == 0:
        return Line(
            "account_mapping",
            label,
            False,
            "Account mapping waits for the chart of accounts.",
            None,
            count=0,
            total=0,
        )
    if f.unmapped_accounts == 0:
        return Line(
            "account_mapping",
            label,
            True,
            "Every active account is mapped.",
            link,
            count=0,
            total=f.active_accounts,
        )
    s = f.suggested_accounts
    return Line(
        "account_mapping",
        label,
        False,
        f"{f.unmapped_accounts} of {f.active_accounts} accounts to confirm; "
        f"{s} {_plural(s, 'has', 'have')} a suggestion.",
        link,
        count=f.unmapped_accounts,
        total=f.active_accounts,
    )


def policy(f: SetupFacts) -> Line:
    label = "Policy"
    link = Link("config", "policy")
    missing = [k for k in REQUIRED_POLICY_KEYS if k not in f.decided_keys]
    later = [
        k for k in f.all_policy_keys if k not in REQUIRED_POLICY_KEYS and k not in f.decided_keys
    ]
    if missing:
        names = ", ".join(f.policy_labels.get(k, k) for k in missing)
        return Line(
            "policy",
            label,
            False,
            f"{len(missing)} of {len(REQUIRED_POLICY_KEYS)} policy "
            f"{_plural(len(missing), 'key', 'keys')} needed now "
            f"{_plural(len(missing), 'is', 'are')} "
            f"not decided: {names}.",
            link,
            count=len(missing),
            total=len(REQUIRED_POLICY_KEYS),
        )
    note = None
    if later:
        note = (
            f"{len(later)} more {_plural(len(later), 'key is', 'keys are')} decided when "
            f"{_plural(len(later), 'its feature arrives', 'their features arrive')}."
        )
    return Line(
        "policy",
        label,
        True,
        "The policy keys needed now are decided.",
        link,
        note,
        count=0,
        total=len(REQUIRED_POLICY_KEYS),
    )


def burden_rates(f: SetupFacts) -> Line:
    label = "Burden rates"
    if f.wip_basis is None:
        return Line(
            "burden_rates",
            label,
            False,
            "Burden rates are set after the WIP basis is decided.",
            Link("config", "policy"),
        )
    if BURDEN_SLOT not in f.wip_basis:
        return Line(
            "burden_rates",
            label,
            True,
            "Burden rates are not needed.",
            Link("config", "burden"),
            note="Labor Burden is not in the WIP basis.",
        )
    if not f.divisions_with_digit:
        return Line(
            "burden_rates",
            label,
            False,
            "No division has a cost-code digit yet; set the divisions first.",
            Link("config", "divisions"),
        )
    if f.divisions_without_rate:
        names = ", ".join(f.divisions_without_rate)
        return Line(
            "burden_rates",
            label,
            False,
            f"No burden rate in force today for {names}.",
            Link("config", "burden"),
            count=len(f.divisions_without_rate),
            total=len(f.divisions_with_digit),
        )
    return Line(
        "burden_rates",
        label,
        True,
        "Every division has a burden rate in force today.",
        Link("config", "burden"),
        count=0,
        total=len(f.divisions_with_digit),
    )


# The brief's order as the owner set it (2026-10-04): Policy before Burden rates.
SETUP_STEPS: tuple[Callable[[SetupFacts], Line | None], ...] = (
    quickbooks,
    suggestion_rules,
    chart_of_accounts,
    account_mapping,
    policy,
    burden_rates,
)


def setup_lines(f: SetupFacts) -> list[Line] | None:
    """The checklist for a role that can view tenant configuration, else None. At most
    one line is primary: the first not-done line that has an action."""
    if not f.can_view_config:
        return None
    lines = [line for step in SETUP_STEPS if (line := step(f)) is not None]
    out: list[Line] = []
    marked = False
    for line in lines:
        if not marked and not line.done and line.link is not None:
            line = Line(
                line.code,
                line.label,
                line.done,
                line.message,
                line.link,
                line.note,
                line.count,
                line.total,
                True,
            )
            marked = True
        out.append(line)
    return out


# --- the jobs ------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobFacts:
    id: str
    name: str
    status: str
    status_label: str
    revenue_method: str
    to_confirm: int
    qbo_linked: bool
    needs_link: bool  # JOB_NO_LEDGER_LINK is on the job (F07, D-35)


@dataclass(frozen=True)
class Need:
    code: str | None  # None: nothing needed
    message: str
    link: Link | None = None
    count: int | None = None


def _confirm(j: JobFacts) -> Need | None:
    if j.to_confirm:
        n = j.to_confirm
        return Need(
            "to_confirm",
            f"{n} work {_plural(n, 'area', 'areas')} to confirm.",
            Link("jobs", job_id=j.id),
            n,
        )
    return None


def _link(j: JobFacts) -> Need | None:
    if j.needs_link:
        return Need(
            "needs_link",
            "Needs its QuickBooks project: link it on the job.",
            Link("jobs", job_id=j.id),
        )
    return None


CONTRACT_METHODS = frozenset({"fixed_price", "time_and_materials"})


def _backlog(j: JobFacts) -> Need | None:
    """D-35: a sold construction job with no QuickBooks project is backlog; a pool or a
    program (made by hand) is not backlog and needs nothing."""
    if j.status == "sold" and not j.qbo_linked and j.revenue_method in CONTRACT_METHODS:
        return Need(None, "Backlog: sold, no money moved yet (D-35). Nothing needed.")
    return None


# First rule that applies wins. F08 onward add theirs (a billing request due, a cost
# without a job…) with a sentence and a test.
JOB_NEEDS: tuple[Callable[[JobFacts], Need | None], ...] = (_confirm, _link, _backlog)


def next_need(j: JobFacts) -> Need:
    for rule in JOB_NEEDS:
        need = rule(j)
        if need is not None:
            return need
    return Need(None, "Nothing needed.")


@dataclass(frozen=True)
class JobLine:
    job: JobFacts
    need: Need


def job_lines(jobs: Sequence[JobFacts]) -> list[JobLine]:
    """Every job that is not closed or cancelled, in the order given."""
    return [JobLine(j, next_need(j)) for j in jobs if j.status in LISTED_JOB_STATUSES]


@dataclass(frozen=True)
class Item:
    code: str
    message: str
    link: Link | None = None
    count: int | None = None


def review_item(
    to_review: int, estimates_total: int, *, can_review: bool, can_import: bool
) -> Item | None:
    if estimates_total == 0:
        return Item(
            "no_estimates", "Upload an estimate.", Link("imports") if can_import else None, 0
        )
    if to_review > 0:
        return Item(
            "to_review",
            f"{to_review} sold {_plural(to_review, 'estimate', 'estimates')} to review.",
            Link("jobs", review=True) if can_review else None,
            to_review,
        )
    return None


def tracked_items(messages: Sequence[str], *, can_track: bool) -> list[Item]:
    """D-37: the tracked QuickBooks rows with no job, one sentence each, as the Jobs
    page words them (``ledger_items``)."""
    link = Link("customers") if can_track else None
    return [Item("LEDGER_PROJECT_NO_JOB", m, link) for m in messages]
