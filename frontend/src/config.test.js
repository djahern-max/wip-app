import assert from "node:assert/strict";
import { test } from "node:test";
import { allConfirmed, confirmedSentence, decidedWords, suggestedNote } from "./config.js";

// F09.4: the words Configuration builds from what the API already sent.

test("Accounts is done only when there are active accounts and none is unmapped", () => {
  assert.equal(allConfirmed({ total_active: 169, unmapped_count: 0 }), true);
  assert.equal(allConfirmed({ total_active: 169, unmapped_count: 3 }), false);
  assert.equal(allConfirmed({ total_active: 0, unmapped_count: 0 }), false); // no chart yet
  assert.equal(allConfirmed(null), false);
});

test("the done sentence names the count the API gave", () => {
  assert.equal(confirmedSentence(169), "All 169 active accounts are confirmed.");
  assert.equal(confirmedSentence(1), "The one active account is confirmed.");
});

test("the suggested note repeats the API's count", () => {
  assert.equal(suggestedNote(12), "12 suggested");
  assert.equal(suggestedNote(0), "0 suggested");
});

test("the policy count is decided keys of all keys", () => {
  assert.equal(decidedWords([{ decided: true }, { decided: false }, { decided: false }]), "1 of 3 decided");
  assert.equal(decidedWords([]), "0 of 0 decided");
  assert.equal(decidedWords(undefined), "0 of 0 decided");
});
