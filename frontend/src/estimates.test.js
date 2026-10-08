import assert from "node:assert/strict";
import { test } from "node:test";
import { detailLead, listSentence } from "./estimates.js";

// F09.2: the count is of what the page holds; the words come first.

test("the sentence under the heading counts estimates and those needing attention", () => {
  assert.equal(listSentence(null), "");
  assert.equal(listSentence([]), "");
  assert.equal(listSentence([{ attention: [] }]), "1 estimate; nothing needs attention.");
  assert.equal(listSentence([{ attention: [{}] }, { attention: [] }]), "2 estimates; 1 needs attention.");
  assert.equal(
    listSentence([{ attention: [{}] }, { attention: [{}, {}] }, {}]),
    "3 estimates; 2 need attention.",
  );
});

test("the line under an estimate's name gives its id and its status in words", () => {
  assert.equal(detailLead({ external_id: "6115758", status_label: "Sold" }), "Estimate 6115758 \u00b7 Sold");
});
