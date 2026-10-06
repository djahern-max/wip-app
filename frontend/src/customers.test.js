import assert from "node:assert/strict";
import { test } from "node:test";
import {
  PENDING_WORDS,
  jobCell,
  pageLabel,
  pendingLabel,
  resultSentence,
  trackLabel,
  trackedSentence,
  untrackLabel,
} from "./customers.js";

// F07.2 (D-37): words first; the sentence from the API is shown as it is.
const SENTENCE =
  'QuickBooks project "1701 Ocean Boulevard" is tracked and has no job. Link it to its job, or make the job first.';
const linked = { active: true, tracked: true, job: { id: "j1", name: "67 Elm Street" }, needs_job: null };
const needs = { active: true, tracked: true, job: null, needs_job: SENTENCE };
const inactive = { active: false, tracked: true, job: null, needs_job: null };
const plain = { active: true, tracked: false, job: null, needs_job: null };

test("the Job column: the job with a link, the review sentence, or inactive in words", () => {
  assert.deepEqual(jobCell(linked), { text: "Job: 67 Elm Street", jobId: "j1" });
  assert.deepEqual(jobCell(needs), { text: SENTENCE, jobId: null });
  assert.deepEqual(jobCell(inactive), { text: "Inactive in QuickBooks", jobId: null });
  assert.deepEqual(jobCell(plain), { text: "", jobId: null });
});

test("an empty search says what to type; no match names the text; counts read as rows", () => {
  assert.equal(resultSentence("", 0), "Type part of a customer or project name, then search.");
  assert.equal(resultSentence("   ", 0), "Type part of a customer or project name, then search.");
  assert.equal(resultSentence("Ocean", 0), "No active customer or project is named like “Ocean”.");
  assert.equal(resultSentence("Ocean", 1), "1 row.");
  assert.equal(resultSentence("Client", 55), "55 rows.");
});

test("paging reads only when there is more than one page", () => {
  assert.equal(pageLabel(1, 1), "");
  assert.equal(pageLabel(2, 3), "Page 2 of 3");
});

test("the actions say what happens, and a linked row says where to untrack", () => {
  assert.equal(trackLabel(plain), "Track");
  assert.equal(trackLabel(needs), "Tracked");
  assert.equal(untrackLabel(needs), "Untrack");
  assert.equal(untrackLabel(linked), "Unlink on the job first");
  assert.equal(
    trackedSentence([]),
    "Nothing is tracked yet. Search below, then track the customers and projects to work on.",
  );
  assert.equal(trackedSentence([needs]), "1 tracked.");
  assert.equal(trackedSentence([needs, linked]), "2 tracked.");
});

test("Track and Untrack say what is happening on the pressed row only (F08.2)", () => {
  assert.deepEqual(PENDING_WORDS, { track: "Tracking…", untrack: "Untracking…" });
  assert.equal(pendingLabel("track", null, "c1"), null);
  assert.equal(pendingLabel("track", { id: "c1", action: "track" }, "c1"), "Tracking…");
  assert.equal(pendingLabel("track", { id: "c1", action: "track" }, "c2"), null);
  assert.equal(pendingLabel("untrack", { id: "c1", action: "untrack" }, "c1"), "Untracking…");
  assert.equal(pendingLabel("untrack", { id: "c1", action: "track" }, "c1"), null);
  // After the request the words return to the row's own.
  assert.equal(pendingLabel("track", null, "c1") || trackLabel({ tracked: false }), "Track");
});

