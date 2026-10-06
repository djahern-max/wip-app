import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DEPOSIT_HINT, EditPolicy, FIND_ITEM_LABEL, NO_SURCHARGE, PolicyRow, RATE_LABEL, REFERENCE_LABEL, SHOW_INACTIVE_LABEL, SURCHARGE_HINT, WIP_BASIS_HINT, hasValue, itemValue, percentOfFraction, showValue, visibleOptions } from "./policyRow.js";

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

// --- F08: the two item keys (D-02, D-39) --------------------------------------------------

const ITEMS = [
  { value: "901", label: "Customer deposit" },
  { value: "902", label: "Fuel surcharge (EX)" },
  { value: "904", label: "Old deposit item (inactive)" },
];

function depositKey(over) {
  return key({ key: "deposit_identification", label: "Deposit identification", kind: "item_ids", description: "The QuickBooks items a deposit invoice uses (D-02).", options: ITEMS, ...over });
}

function surchargeKey(over) {
  return key({ key: "fuel_surcharge_treatment", label: "Fuel surcharge treatment", kind: "surcharge", description: "The QuickBooks items a fuel surcharge line uses, and the rate (D-39).", options: ITEMS, ...over });
}

test("a fraction reads as a percent, digits only", () => {
  assert.equal(percentOfFraction("0.0500"), "5.00%");
  assert.equal(percentOfFraction("0.05"), "5.00%");
  assert.equal(percentOfFraction("0.1"), "10.00%");
  assert.equal(percentOfFraction("0.1234"), "12.34%");
  assert.equal(percentOfFraction("0.0075"), "0.75%");
  assert.equal(percentOfFraction(null), "");
});

test("the item keys show the names of the ticked items, the rate as a percent, or no surcharge", () => {
  assert.equal(showValue(depositKey({ decided: true, value: { item_ids: ["901", "904"] } })), "Customer deposit, Old deposit item (inactive)");
  assert.equal(showValue(depositKey({ decided: true, value: { item_ids: ["999"] } })), "item 999");
  assert.equal(showValue(surchargeKey({ decided: true, value: { item_ids: ["902"], rate: "0.0500" } })), "Fuel surcharge (EX) at 5.00%");
  assert.equal(showValue(surchargeKey({ decided: true, value: { item_ids: ["902"], rate: null } })), "Fuel surcharge (EX), rate not decided");
  assert.equal(showValue(surchargeKey({ decided: true, value: { item_ids: [], rate: null } })), NO_SURCHARGE);
  const html = row(depositKey());
  assert.ok(html.includes("<td>Not decided</td>") && html.includes(">Decide</button>") && !html.includes("Set with the billing reports"));
});

test("the deposit edit is a pick-list of the company's items and needs one ticked", () => {
  const html = edit(depositKey());
  assert.ok(html.includes(DEPOSIT_HINT));
  // Three items (none carries a flag, so all count as active) and the "Show inactive
  // items" switch (F08.2); the search box is its own control.
  assert.equal((html.match(/type="checkbox"/g) || []).length, 4);
  assert.ok(html.includes(FIND_ITEM_LABEL) && html.includes(SHOW_INACTIVE_LABEL) && html.includes('type="search"'));
  assert.ok(html.includes("Old deposit item (inactive)"));
  assert.ok(!html.includes("checked"));
  assert.ok(recordButton(html).includes("disabled"));
  const stored = edit(depositKey({ decided: true, value: { item_ids: ["901"] } }));
  assert.equal((stored.match(/checked=""/g) || []).length, 1);
  assert.ok(!recordButton(stored).includes("disabled"));
  assert.equal(hasValue("item_ids", "", [], []), false);
  assert.equal(hasValue("item_ids", "", [], ["901"]), true);
  assert.deepEqual(itemValue("item_ids", "", ["901", "904"], false), { item_ids: ["901", "904"] });
  const none = edit(depositKey({ options: [] }));
  assert.ok(none.includes("No QuickBooks items are held yet") && recordButton(none).includes("disabled"));
});

test("the surcharge edit takes items and a rate, or records no surcharge", () => {
  const html = edit(surchargeKey());
  assert.ok(html.includes(SURCHARGE_HINT) && html.includes(RATE_LABEL) && html.includes(NO_SURCHARGE));
  assert.equal((html.match(/type="checkbox"/g) || []).length, 5); // three items, the inactive switch, the no-surcharge box
  assert.ok(recordButton(html).includes("disabled"));
  assert.equal(hasValue("surcharge", "", [], [], false), false);
  assert.equal(hasValue("surcharge", "", [], ["902"], false), true);
  assert.equal(hasValue("surcharge", "", [], [], true), true);
  assert.deepEqual(itemValue("surcharge", "0.05", ["902"], false), { item_ids: ["902"], rate: "0.05" });
  assert.deepEqual(itemValue("surcharge", "  ", ["902"], false), { item_ids: ["902"], rate: null });
  assert.deepEqual(itemValue("surcharge", "0.05", ["902"], true), { item_ids: [], rate: null });
  const stored = edit(surchargeKey({ decided: true, value: { item_ids: ["902"], rate: "0.0500" } }));
  assert.ok(stored.includes('value="0.0500"') && (stored.match(/checked=""/g) || []).length === 1);
  const none = edit(surchargeKey({ decided: true, value: { item_ids: [], rate: null } }));
  assert.equal((none.match(/checked=""/g) || []).length, 1); // the no-surcharge box
  assert.ok(!recordButton(none).includes("disabled"));
});

// F08.2 (item 3): the pick-list filters only what the API gave it, builds no label.
const FLAGGED = [
  { value: "901", label: "Customer deposit", active: true },
  { value: "902", label: "Fuel surcharge (EX)", active: true },
  { value: "904", label: "Old deposit item (inactive)", active: false },
  { value: "905", label: "Gone deposit item (deleted)", active: false },
];

test("the pick-list hides inactive and deleted items unless the switch is on or the item is ticked, and narrows as typed", () => {
  const values = (o) => o.map((x) => x.value);
  assert.deepEqual(values(visibleOptions(FLAGGED, "", false, [])), ["901", "902"]);
  assert.deepEqual(values(visibleOptions(FLAGGED, "", true, [])), ["901", "902", "904", "905"]);
  assert.deepEqual(values(visibleOptions(FLAGGED, "", false, ["905"])), ["901", "902", "905"]);
  assert.deepEqual(values(visibleOptions(FLAGGED, "deposit", false, [])), ["901"]);
  assert.deepEqual(values(visibleOptions(FLAGGED, "DEPOSIT", true, [])), ["901", "904", "905"]);
  assert.deepEqual(values(visibleOptions(FLAGGED, "  fuel ", false, [])), ["902"]);
  assert.deepEqual(values(visibleOptions(FLAGGED, "nothing", true, [])), []);
  assert.deepEqual(values(visibleOptions(ITEMS, "", false, [])), ["901", "902", "904"]); // no flag: active
  assert.deepEqual(values(visibleOptions(undefined, "", false, [])), []);
});

test("a ticked inactive item is always shown and marked; an unticked one waits behind the switch", () => {
  const plain = edit(depositKey({ options: FLAGGED }));
  assert.ok(plain.includes("Customer deposit") && !plain.includes("Old deposit item (inactive)") && !plain.includes("(deleted)"));
  assert.equal((plain.match(/type="checkbox"/g) || []).length, 3); // two active items and the switch
  const stored = edit(depositKey({ options: FLAGGED, decided: true, value: { item_ids: ["905"] } }));
  assert.ok(stored.includes("Gone deposit item (deleted)") && !stored.includes("Old deposit item (inactive)"));
  assert.equal((stored.match(/checked=""/g) || []).length, 1);
  assert.equal(showValue(depositKey({ options: FLAGGED, decided: true, value: { item_ids: ["904"] } })), "Old deposit item (inactive)");
});


// --- F07.4 (D-42): a choice key ----------------------------------------------------------

test("a choice key shows the chosen value's words and offers its options", () => {
  const p = key({
    key: "change_order_evidence",
    label: "Change order evidence",
    kind: "choice",
    description: "What an approval of a change order must carry (D-42).",
    options: [
      { value: "none", label: "None required" },
      { value: "reference", label: "A reference is required" },
    ],
  });
  assert.equal(showValue({ ...p, decided: true, value: "reference", value_label: "A reference is required" }, []), "A reference is required");
  assert.equal(showValue({ ...p, decided: true, value: "none", value_label: null }, []), "none");
  assert.equal(hasValue("choice", "", [], []), false);
  assert.equal(hasValue("choice", "none", [], []), true);
  const html = renderToStaticMarkup(createElement(EditPolicy, { p, categories: [], busy: false, onCancel() {}, onSave() {} }));
  assert.ok(html.includes("None required") && html.includes("A reference is required"));
  assert.ok(html.includes("<select"));
});
