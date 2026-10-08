import assert from "node:assert/strict";
import { test } from "node:test";
import { doneWord, jobsSentence, linkLabel, markerText, primaryCount, progressWords } from "./home.js";

// F07.3: words first; the Shell gets a page and a section from the API's link.

test("a link reads as the page it opens, with the Configuration section named", () => {
  assert.equal(linkLabel(null), "");
  assert.equal(linkLabel({ page: "connections" }), "Open Connections");
  assert.equal(linkLabel({ page: "imports" }), "Open Imports");
  assert.equal(linkLabel({ page: "config", section: "accounts" }), "Open Configuration, Accounts");
  assert.equal(linkLabel({ page: "config", section: "burden" }), "Open Configuration, Burden rates");
  assert.equal(linkLabel({ page: "config", section: "policy" }), "Open Configuration, Policy");
  assert.equal(linkLabel({ page: "jobs", review: true }), "Review sold estimates");
  assert.equal(linkLabel({ page: "jobs", job_id: "j1" }), "Open the job");
  assert.equal(linkLabel({ page: "customers" }), "Open Customers");
});

test("status is a word, and the primary action is counted, never assumed", () => {
  assert.equal(doneWord(true), "Done");
  assert.equal(doneWord(false), "Not done");
  assert.equal(primaryCount(null), 0);
  assert.equal(primaryCount([{ primary: false }, { primary: true }]), 1);
});

test("the sentences above the checklist and the jobs", () => {
  assert.equal(progressWords([{ done: true }, { done: true }]), "Set-up is complete.");
  assert.equal(progressWords([{ done: false }, { done: true }]), "1 of 2 steps done");
  assert.equal(progressWords([{ done: false }]), "0 of 1 step done");
  assert.equal(progressWords(null), "0 of 0 steps done");
  assert.equal(markerText({ done: true }, 0), "\u2713");
  assert.equal(markerText({ done: false }, 2), "3");
  assert.equal(jobsSentence([]), "No job is open yet.");
  assert.equal(jobsSentence([{ code: null }]), "1 job, nothing needed.");
  assert.equal(jobsSentence([{ code: "to_confirm" }, { code: null }]), "2 jobs; 1 needs something.");
});
