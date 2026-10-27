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

// F09.6 (D-50): the set-up view of a job is drawn for firm roles only; client_admin is a
// client role on the screens even where the API still lets it manage jobs.
test("firm roles are the two firm codes and nothing else", async () => {
  const { FIRM_ROLES, isFirmRole } = await import("./roles.js");
  assert.deepEqual(FIRM_ROLES, ["firm_admin", "firm_staff"]);
  assert.equal(isFirmRole("firm_admin"), true);
  assert.equal(isFirmRole("firm_staff"), true);
  assert.equal(isFirmRole("client_admin"), false);
  assert.equal(isFirmRole("client_pm"), false);
  assert.equal(isFirmRole("client_viewer"), false);
  assert.equal(isFirmRole(null), false);
});
