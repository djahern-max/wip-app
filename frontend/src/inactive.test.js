import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyMessage, inactiveCount, inactiveLabel, visibleRows } from "./inactive.js";

// The Burden rates list as Rye Beach has it (D-34): four active rows, four deactivated.
const RATES = [
  ...["LS", "EX", "GC", "SNOW"].map((d) => ({ id: `a-${d}`, division_code: d, active: true })),
  ...["LS", "EX", "GC", "SNOW"].map((d) => ({ id: `i-${d}`, division_code: d, active: false })),
];

test("inactive rows are hidden by default and counted for the control", () => {
  assert.equal(visibleRows(RATES, false).length, 4);
  assert.ok(visibleRows(RATES, false).every((r) => r.active));
  assert.equal(inactiveCount(RATES), 4);
  assert.equal(inactiveLabel(4, false), "Show inactive (4)");
});

test("the control shows every row, and says how to hide them again", () => {
  assert.equal(visibleRows(RATES, true).length, 8);
  assert.equal(inactiveLabel(4, true), "Hide inactive (4)");
});

test("a row deactivated on the page drops out of the default view", () => {
  const after = RATES.map((r) => (r.id === "a-EX" ? { ...r, active: false } : r));
  assert.deepEqual(
    visibleRows(after, false).map((r) => r.id),
    ["a-LS", "a-GC", "a-SNOW"],
  );
  assert.equal(inactiveCount(after), 5);
});

test("an empty list and a list with only inactive rows read differently", () => {
  assert.equal(emptyMessage([], "divisions"), "No divisions yet.");
  assert.equal(
    emptyMessage([{ active: false }], "divisions"),
    'No active divisions. Use "Show inactive" to see the others.',
  );
  assert.deepEqual(visibleRows(null, false), []);
});
