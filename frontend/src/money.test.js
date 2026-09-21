// node --test (no dependency). Run with `npm test` from frontend/.
import assert from "node:assert/strict";
import { test } from "node:test";
import { addMoney, formatMoney } from "./money.js";

test("cents always, thousands grouped", () => {
  assert.equal(formatMoney("0"), "0.00");
  assert.equal(formatMoney("0.5"), "0.50");
  assert.equal(formatMoney("7"), "7.00");
  assert.equal(formatMoney("1234.5"), "1,234.50");
  assert.equal(formatMoney("141366.68"), "141,366.68");
  assert.equal(formatMoney("1234567890123.45"), "1,234,567,890,123.45");
});

test("negatives in parentheses; zero is never negative", () => {
  assert.equal(formatMoney("-1234.5"), "(1,234.50)");
  assert.equal(formatMoney("-0.01"), "(0.01)");
  assert.equal(formatMoney("-0"), "0.00");
  assert.equal(formatMoney("-0.00"), "0.00");
  assert.equal(formatMoney("-0.004"), "0.00");
});

test("digits are handled as text: no binary float on the way", () => {
  assert.equal(formatMoney("141366.68000000002"), "141,366.68");
  assert.equal(formatMoney("0.10"), "0.10");
  assert.equal(formatMoney("0.125"), "0.13"); // half up, like ROUND_HALF_UP
  assert.equal(formatMoney("0.124"), "0.12");
  assert.equal(formatMoney("2.675"), "2.68"); // parseFloat would give 2.67
  assert.equal(formatMoney("99999999999999999999.999"), "100,000,000,000,000,000,000.00");
});

test("null and undefined render as a dash", () => {
  assert.equal(formatMoney(null), "—");
  assert.equal(formatMoney(undefined), "—");
});

test("numbers and garbled strings throw instead of rendering NaN or 0", () => {
  assert.throws(() => formatMoney(12.5), TypeError);
  assert.throws(() => formatMoney(0), TypeError);
  assert.throws(() => formatMoney(""), TypeError);
  assert.throws(() => formatMoney("1,234.50"), TypeError);
  assert.throws(() => formatMoney("$12"), TypeError);
  assert.throws(() => formatMoney("1e5"), TypeError);
  assert.throws(() => formatMoney("NaN"), TypeError);
});

test("addMoney adds decimal strings exactly, at cent scale", () => {
  assert.equal(addMoney("0.10", "0.20"), "0.30");
  assert.equal(addMoney("1234.56", "-1234.56"), "0.00");
  assert.equal(addMoney("-5.00", "2.25"), "-2.75");
  assert.equal(addMoney("99999999999.99", "0.01"), "100000000000.00");
  assert.equal(formatMoney(addMoney("-100.00", "-0.50")), "(100.50)");
  assert.throws(() => addMoney("1.005", "0"), TypeError);
  assert.throws(() => addMoney(1, "0"), TypeError);
});
