import { test } from "node:test";
import assert from "node:assert/strict";
import { amountOr, attachReady, nextIndex, reasonText } from "./jobs.js";

test("a missing amount is words, a zero is 0.00", () => {
  assert.equal(amountOr(null, "Not computed"), "Not computed");
  assert.equal(amountOr(undefined, "None"), "None");
  assert.equal(amountOr("0.00", "None"), "0.00");
  assert.equal(amountOr("465469.59", "None"), "465,469.59");
});

test("reasons read as one phrase", () => {
  assert.equal(reasonText(["by client name only", "by name only"]), "By client name only; by name only");
  assert.equal(reasonText("estimate id in name"), "Estimate id in name");
  assert.equal(reasonText([]), "");
});

test("skip wraps and never writes", () => {
  assert.equal(nextIndex(0, 3), 1);
  assert.equal(nextIndex(2, 3), 0);
  assert.equal(nextIndex(0, 0), 0);
});

test("attach needs a job and a role, and a note only when ignored", () => {
  assert.equal(attachReady({ jobId: "", role: "change_order", note: "" }), false);
  assert.equal(attachReady({ jobId: "j", role: "change_order", note: "" }), true);
  assert.equal(attachReady({ jobId: "j", role: "ignored", note: "  " }), false);
  assert.equal(attachReady({ jobId: "j", role: "ignored", note: "Sold in error" }), true);
});

test("a sold job with no link reads as backlog; later statuses need the link (D-35)", async () => {
  const { qboCell, offersInProgress } = await import("./jobs.js");
  assert.equal(qboCell({ qbo_linked: true, qbo_names: ["6115758 Client 05", "Old"], status: "in_progress" }), "6115758 Client 05; Old");
  assert.equal(qboCell({ qbo_linked: false, qbo_names: [], status: "sold" }), "Not yet: sold, no money moved (backlog)");
  assert.equal(qboCell({ qbo_linked: false, qbo_names: [], status: "in_progress" }), "Not linked: link its project");
  assert.equal(qboCell({ qbo_linked: false, qbo_names: [], status: "closed" }), "Not linked: link its project");
  assert.equal(offersInProgress({ status: "sold" }), true);
  assert.equal(offersInProgress({ status: "in_progress" }), false);
});

test("choosing a program job presets ignored and the note", async () => {
  const { chooseJob, PROGRAM_NOTE } = await import("./jobs.js");
  const jobs = [
    { id: "p", revenue_method: "recurring_service" },
    { id: "j", revenue_method: "fixed_price" },
  ];
  const start = { jobId: "", role: "change_order", note: "" };
  assert.deepEqual(chooseJob(start, "p", jobs), { jobId: "p", role: "ignored", note: PROGRAM_NOTE });
  assert.deepEqual(chooseJob(start, "j", jobs), { jobId: "j", role: "change_order", note: "" });
  assert.equal(chooseJob({ ...start, note: "Snow contract" }, "p", jobs).note, "Snow contract");
});
