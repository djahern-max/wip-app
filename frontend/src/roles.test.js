import assert from "node:assert/strict";
import { test } from "node:test";
import { roleWords } from "./roles.js";

// F09.1: a role reaches the screen as words, never as its code.

test("each role reads as words", () => {
  assert.equal(roleWords("firm_admin"), "Firm admin");
  assert.equal(roleWords("firm_staff"), "Firm staff");
  assert.equal(roleWords("client_admin"), "Client admin");
  assert.equal(roleWords("client_pm"), "Project manager");
  assert.equal(roleWords("client_viewer"), "Viewer");
});

test("nothing and the unknown never show an underscore", () => {
  assert.equal(roleWords(null), "");
  assert.equal(roleWords("site_lead"), "Site lead");
});
