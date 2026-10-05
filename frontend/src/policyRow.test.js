import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { EditPolicy, PolicyRow, REFERENCE_LABEL, WIP_BASIS_HINT, hasValue, showValue } from "./policyRow.js";

// F04.1: the Policy table asks only what a person can answer today, in words they know.

const ZONES = [
  ["America/New_York", "Eastern (America/New_York)"],
  ["America/Chicago", "Central (America/Chicago)"],
  ["America/Denver", "Mountain (America/Denver)"],
  ["America/Phoenix", "Arizona (America/Phoenix)"],
  ["America/Los_Angeles", "Pacific (America/Los_Angeles)"],
  ["America/Anchorage", "Alaska (America/Anchorage)"],
  ["Pacific/Honolulu", "Hawaii (Pacific/Honolulu)"],
];
const OPTIONS = ZONES.map(([value, label]) => ({ value, label }));

function key(over) {
  return {
    key: "timezone",
    label: "Time zone",
    kind: "timezone",
    description: "Period boundaries are tenant-local months.",
    decided: false,
    value: null,
    value_label: null,
    decided_by_email: null,
    decided_at: null,
    decision_ref: null,
    waiting: null,
    options: OPTIONS,
    ...over,
  };
}

function row(p, canSetPolicy = true) {
  return renderToStaticMarkup(createElement(PolicyRow, { p, categories: [], canSetPolicy, busy: false, onEdit: () => {} }));
}

function edit(p, categories = []) {
  return renderToStaticMarkup(createElement(EditPolicy, { p, categories, busy: false, onCancel: () => {}, onSave: () => {} }));
}

// Built from a string so the effects scanner (tests/test_frontend_effects.py), which
// reads no regex literals, sees one string and not stray quotes.
const OPTION = new RegExp('<option value="([^"]*)"[^>]*>([^<]*)</option>', "g");

function options(html) {
  return [...html.matchAll(OPTION)].map((m) => [m[1], m[2]]);
}

function recordButton(html) {
  return html.match(/<button([^>]*)>Record decision<\/button>/)[1];
}

test("the time zone control is a select carrying the seven zones with their names as values", () => {
  const html = edit(key());
  assert.ok(html.includes("<select"));
  const found = options(html);
  assert.deepEqual(found[0], ["", "Choose…"]);
  assert.deepEqual(found.slice(1), ZONES);
  assert.equal(found.length, 8);
  assert.ok(html.includes("Time zone"));
  assert.ok(!html.includes("e.g. America/New_York"));
});

test("a stored zone off the list is offered by the API and kept selected", () => {
  const p = key({ decided: true, value: "Europe/London", value_label: "Europe/London", options: [...OPTIONS, { value: "Europe/London", label: "Europe/London" }] });
  const html = edit(p);
  assert.equal(options(html).length, 9);
  assert.ok(html.includes('<option value="Europe/London" selected="">Europe/London</option>'));
  assert.equal(showValue(p, []), "Europe/London");
  assert.equal(showValue(key({ decided: true, value: "America/New_York", value_label: "Eastern (America/New_York)" }), []), "Eastern (America/New_York)");
});

test("Record decision is enabled with a value and an empty reference, disabled without a value", () => {
  const decided = edit(key({ decided: true, value: "America/New_York", value_label: "Eastern (America/New_York)" }));
  assert.ok(!recordButton(decided).includes("disabled"));
  assert.ok(decided.includes(REFERENCE_LABEL));
  assert.ok(!decided.includes("required"));
  assert.ok(recordButton(edit(key())).includes("disabled"));
  assert.equal(hasValue("timezone", "America/Denver", []), true);
  assert.equal(hasValue("timezone", "  ", []), false);
  assert.equal(hasValue("month", "", []), false);
  assert.equal(hasValue("month", "3", []), true);
  assert.equal(hasValue("category_slots", "", []), false);
  assert.equal(hasValue("category_slots", "", ["10"]), true);
  assert.equal(hasValue("money", "25000", []), true);
});

test("a waiting key draws its sentence and no Decide link; a settable key draws Decide or Change", () => {
  const waiting = row(key({ key: "deposit_identification", label: "Deposit identification", kind: "text", waiting: "Set with the billing reports (F08).", options: null }));
  assert.ok(waiting.includes("Set with the billing reports (F08).") && !waiting.includes("Decide") && !waiting.includes("<button"));
  const stored = row(key({ key: "deposit_identification", label: "Deposit identification", kind: "text", waiting: "Set with the billing reports (F08).", options: null, decided: true, value: "Deposit item", decided_by_email: "a@example.test", decided_at: "2026-10-05T12:00:00+00:00", decision_ref: "D-02" }));
  assert.ok(stored.includes("Deposit item") && stored.includes("a@example.test") && stored.includes("D-02"));
  assert.ok(stored.includes("Set with the billing reports (F08).") && !stored.includes("<button"));
  assert.ok(row(key()).includes(">Decide</button>"));
  assert.ok(row(key({ decided: true, value: "America/New_York", value_label: "Eastern (America/New_York)" })).includes(">Change</button>"));
  assert.ok(!row(key({ waiting: "x" }), false).includes("x"));
});

test("Not decided carries no .num class; a decided money value does; a blank reference shows nothing", () => {
  const money = key({ key: "small_job_threshold", label: "Small job threshold", kind: "money", options: null, waiting: "Not decided (D-08). Set when the WIP schedule arrives." });
  const undecided = row(money);
  assert.ok(undecided.includes("<td>Not decided</td>"));
  assert.ok(!undecided.includes('class="num"'));
  const decided = row({ ...money, decided: true, value: "25000.50" });
  assert.ok(decided.includes('<td class="num">25,000.50</td>'));
  assert.ok(row(key({ decided: true, value: "America/New_York", value_label: "Eastern (America/New_York)" })).includes('<td class="wrap-anywhere"></td>'));
});

test("the WIP basis edit says what to tick and pre-ticks nothing", () => {
  const categories = [
    { id: "c10", slot: "10", name: "Labor", active: true },
    { id: "c20", slot: "20", name: "Labor Burden", active: true },
  ];
  const html = edit(key({ key: "wip_basis", label: "WIP basis", kind: "category_slots", options: null }), categories);
  assert.ok(html.includes(WIP_BASIS_HINT));
  assert.equal((html.match(/type="checkbox"/g) || []).length, 2);
  assert.ok(!html.includes("checked"));
});
