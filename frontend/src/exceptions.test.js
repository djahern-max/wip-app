import { test } from "node:test";
import assert from "node:assert/strict";
import {
  dismissedLine,
  eventLine,
  filterQuery,
  noteReady,
  openWords,
  pendingLabel,
  pickerWords,
  rowActions,
} from "./exceptions.js";

test("the filter query sends only what is chosen", () => {
  assert.equal(filterQuery({ status: "", severity: "", mine: false }), "");
  assert.equal(filterQuery({ status: "open", severity: "", mine: false }), "?status=open");
  assert.equal(filterQuery({ status: "open", severity: "warn", mine: true }), "?status=open&severity=warn&mine=true");
});

test("the counts are words, never a colour alone", () => {
  assert.equal(openWords({ info: 0, warn: 10, block_close: 2 }), "12 open");
  assert.equal(openWords(null), "");
  assert.equal(pickerWords({ info: 0, warn: 0, block_close: 0 }), "nothing open");
  assert.equal(pickerWords({ info: 1, warn: 10, block_close: 1 }), "12 open, 1 blocks the close");
  assert.equal(pickerWords({ info: 0, warn: 10, block_close: 2 }), "12 open, 2 block the close");
  assert.equal(pickerWords({ info: 0, warn: 3, block_close: 0 }), "3 open");
});

test("history lines say who and when first (D-22)", () => {
  assert.equal(eventLine({ kind: "raised", occurred_at: "2026-10-08T12:00:00+00:00" }), "Raised on 2026-10-08.");
  assert.equal(
    eventLine({ kind: "dismissed", actor: "Dana", occurred_at: "2026-10-08T12:00:00+00:00", text: "T&M (D-24)." }),
    "Dana dismissed it on 2026-10-08: T&M (D-24).",
  );
  assert.equal(
    eventLine({ kind: "assigned", actor: "Dana", assigned_to: null, occurred_at: "2026-10-09" }),
    "Dana assigned it to nobody on 2026-10-09.",
  );
  assert.equal(
    eventLine({ kind: "reopened", actor: null, occurred_at: "2026-10-10" }),
    "Opened again on 2026-10-10: what the sentence states changed.",
  );
  assert.equal(eventLine({ kind: "reopened", actor: "Dana", occurred_at: "2026-10-10" }), "Dana reopened it on 2026-10-10.");
  assert.equal(eventLine({ kind: "resolved", occurred_at: "2026-10-11" }), "Resolved on 2026-10-11: no longer raised.");
  assert.equal(eventLine({ kind: "note", actor: "Dana", occurred_at: "2026-10-11", text: "Seen." }), "Dana on 2026-10-11: Seen.");
  assert.equal(
    dismissedLine({ dismissed_by: "Dana", dismissed_at: "2026-10-08T12:00:00+00:00", note: "Known." }),
    "Dismissed by Dana on 2026-10-08: Known.",
  );
});

test("a note needs words; the controls follow the status, the severity and the role (D-46)", () => {
  assert.equal(noteReady(""), false);
  assert.equal(noteReady("   "), false);
  assert.equal(noteReady("Fine."), true);
  const open = { status: "open", may_dismiss: true };
  assert.deepEqual(rowActions(open, true).map((a) => a.action), ["assign", "dismiss"]);
  assert.deepEqual(rowActions({ status: "open", may_dismiss: false }, true).map((a) => a.action), ["assign"]);
  assert.deepEqual(rowActions({ status: "dismissed", may_dismiss: true }, true).map((a) => a.action), ["reopen"]);
  assert.deepEqual(rowActions({ status: "resolved", may_dismiss: true }, true), []);
  assert.deepEqual(rowActions(open, false), []);
});

test("busy labels name the action in flight", () => {
  assert.equal(pendingLabel("dismiss", { action: "dismiss", id: "e1" }, "e1"), "Dismissing…");
  assert.equal(pendingLabel("dismiss", { action: "dismiss", id: "e1" }, "e2"), null);
  assert.equal(pendingLabel("assign", null, "e1"), null);
});
