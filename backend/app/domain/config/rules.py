"""The suggestion engine (plan call 2; owner amendment B).

Rules are data: ordered per tenant, first match wins. A rule's ``pattern`` is a
regular expression matched against the **whole** account number string (any
length). Each output is explicit or derived: ``division_id`` or
``division_from_digit`` (a 1-based position in the account number whose character
is looked up in ``division.code_digit``); ``cost_category_id`` or
``cost_category_from_slot`` (the last two characters are the slot). A derivation
that finds nothing means the rule does not match and the next one is tried; no rule
matching means no suggestion (the account stays unmapped).

Patterns are compiled and validated when rules are loaded: an invalid pattern, or
one over ``PATTERN_MAX_LENGTH`` characters, is refused naming the rule.
"""

import re
from dataclasses import dataclass
from uuid import UUID

from app.domain.config.models import PATTERN_MAX_LENGTH, AccountSuggestRule


class RuleError(ValueError):
    """A rule that cannot be used; the message names the rule, never an account."""


@dataclass(frozen=True)
class CompiledRule:
    name: str
    regex: re.Pattern
    division_id: UUID | None
    division_from_digit: int | None
    cost_category_id: UUID | None
    cost_category_from_slot: bool
    in_job_cost: bool


@dataclass(frozen=True)
class Suggestion:
    division_id: UUID | None
    cost_category_id: UUID | None
    in_job_cost: bool
    rule_name: str


def compile_pattern(name: str, pattern: str) -> re.Pattern:
    if not pattern or len(pattern) > PATTERN_MAX_LENGTH:
        raise RuleError(f"rule {name!r}: pattern must be 1 to {PATTERN_MAX_LENGTH} characters")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RuleError(f"rule {name!r}: invalid pattern ({exc.msg})") from None


def compile_rules(rows: list[AccountSuggestRule]) -> list[CompiledRule]:
    out = []
    for r in sorted(rows, key=lambda r: r.sort_order):
        if not r.active:
            continue
        if r.division_from_digit is not None and r.division_from_digit < 1:
            raise RuleError(f"rule {r.name!r}: division_from_digit must be 1 or more")
        out.append(
            CompiledRule(
                name=r.name,
                regex=compile_pattern(r.name, r.pattern),
                division_id=r.division_id,
                division_from_digit=r.division_from_digit,
                cost_category_id=r.cost_category_id,
                cost_category_from_slot=r.cost_category_from_slot,
                in_job_cost=r.in_job_cost,
            )
        )
    return out


def suggest_for(
    account_no: str,
    rules: list[CompiledRule],
    divisions_by_digit: dict[str, UUID],
    categories_by_slot: dict[str, UUID],
) -> Suggestion | None:
    """Pure: the first rule that matches and can derive what it needs."""
    for rule in rules:
        if rule.regex.fullmatch(account_no) is None:
            continue
        division_id = rule.division_id
        if rule.division_from_digit is not None:
            pos = rule.division_from_digit - 1
            if pos >= len(account_no):
                continue
            division_id = divisions_by_digit.get(account_no[pos])
            if division_id is None:
                continue
        category_id = rule.cost_category_id
        if rule.cost_category_from_slot:
            category_id = categories_by_slot.get(account_no[-2:]) if len(account_no) >= 2 else None
            if category_id is None:
                continue
        return Suggestion(division_id, category_id, rule.in_job_cost, rule.name)
    return None
