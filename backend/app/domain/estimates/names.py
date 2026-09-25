"""The two name rules (F06 plan question 8). Each is one constant; each is a
suggestion for a person, never a decision (D-01, D-24, CLAUDE.md: names never
decide anything)."""

import re

# "CO:", "C/O" or "CO " at the start of a work-area name suggests a change order.
# "Concrete Sidewalk" does not match: the rule is a prefix token, not "starts with CO".
CHANGE_ORDER_PREFIX = re.compile(r"^\s*(?:CO:|C/O\b|CO\s)", re.IGNORECASE)

# A work-area name that reads as a rate (D-24: a unit-priced line inside a fixed-price
# estimate is an exception for the controller, not a rule).
UNIT_PRICE_WORDS = re.compile(r"\bper\s+(?:day|hour|hr|load)\b", re.IGNORECASE)


def suggests_change_order(name: str) -> bool:
    return bool(CHANGE_ORDER_PREFIX.match(name or ""))


def reads_as_unit_price(name: str) -> bool:
    return bool(UNIT_PRICE_WORDS.search(name or ""))
