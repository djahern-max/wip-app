import assert from "node:assert/strict";
import { test } from "node:test";
import { formatRate } from "./rates.js";

test("fractions become percentages with two decimals", () => {
  assert.equal(formatRate("0.3250"), "32.50%");
  assert.equal(formatRate("0.1"), "10.00%");
  assert.equal(formatRate("0"), "0.00%");
  assert.equal(formatRate("1.5"), "150.00%");
  assert.equal(formatRate("0.32505"), "32.51%"); // half up on the fifth digit
  assert.equal(formatRate("0.32504"), "32.50%");
  assert.equal(formatRate("12.3456"), "1,234.56%");
});

test("dash for nothing, throws for the wrong type", () => {
  assert.equal(formatRate(null), "—");
  assert.throws(() => formatRate(0.325), TypeError);
  assert.throws(() => formatRate("32.5%"), TypeError);
});
