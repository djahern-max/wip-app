"""The JSON codec (F03, owner conditions on plan call 2): Decimals are JSON numbers
with their digits, floats are refused with the key path, hashes are canonical."""

import json
from decimal import Decimal

import pytest

from app.core.jsoncodec import JSONEncodeError, canonical_json, json_loads, payload_sha256

AMOUNTS = {
    "total": Decimal("141366.68"),
    "rate": Decimal("0.10"),
    "drift": Decimal("141366.68000000002"),
    "big": 12345678901234567890123,
    "lines": [{"amount": Decimal("362.07")}, {"amount": Decimal("-0.01")}],
}


def test_decimals_are_written_as_json_numbers_with_their_digits() -> None:
    text = canonical_json(AMOUNTS)
    assert (
        text == '{"big":12345678901234567890123,"drift":141366.68000000002,'
        '"lines":[{"amount":362.07},{"amount":-0.01}],"rate":0.10,"total":141366.68}'
    )
    # Valid JSON for any parser, and ours reads the digits back exactly.
    assert json.loads(text)["drift"] == 141366.68000000002
    back = json_loads(text)
    assert back["drift"] == Decimal("141366.68000000002") and str(back["drift"]).endswith("002")
    assert back["rate"] == Decimal("0.10") and str(back["rate"]) == "0.10"
    assert isinstance(back["big"], int) and back["big"] == AMOUNTS["big"]
    assert all(isinstance(line["amount"], Decimal) for line in back["lines"])


@pytest.mark.parametrize("text", ["1E+2", "-0", "0E-7", "1.5E-7", "100"])
def test_exponent_and_zero_forms_are_valid_json(text: str) -> None:
    assert canonical_json({"v": Decimal(text)}) == f'{{"v":{text}}}'
    json.loads(canonical_json({"v": Decimal(text)}))


def test_keys_sorted_no_whitespace_bool_before_int() -> None:
    assert canonical_json({"b": True, "a": 1, "c": None, "d": [False, 0]}) == (
        '{"a":1,"b":true,"c":null,"d":[false,0]}'
    )
    assert canonical_json('é"\n') == '"é\\"\\n"'
    assert canonical_json(()) == "[]"


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ({"x": 1.5}, "$.x"),
        ({"lines": [{"amount": Decimal("1")}, {"amount": 2.0}]}, "$.lines[1].amount"),
        ([Decimal("NaN")], "$[0]"),
        ({"inf": Decimal("Infinity")}, "$.inf"),
        ({1: "x"}, "$"),
        ({"when": object()}, "$.when"),
    ],
)
def test_refused_values_name_the_path_not_the_value(value: object, path: str) -> None:
    with pytest.raises(JSONEncodeError) as excinfo:
        canonical_json(value)
    assert path in str(excinfo.value)
    assert "1.5" not in str(excinfo.value) and "2.0" not in str(excinfo.value)


def test_hash_is_over_the_canonical_form() -> None:
    a = payload_sha256({"z": Decimal("1E+2"), "a": [1, 2]})
    b = payload_sha256({"a": [1, 2], "z": Decimal("1E+2")})
    assert a == b
    # jsonb would render 1E+2 as 100; the hash must not treat them alike.
    assert payload_sha256({"z": Decimal("100")}) != payload_sha256({"z": Decimal("1E+2")})
    assert payload_sha256({"amount": Decimal("0.10")}) != payload_sha256({"amount": Decimal("0.1")})
