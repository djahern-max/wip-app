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

// --- F07.1 -----------------------------------------------------------------------------

test("the Action column: two separate confirmations before, one change after (D-22)", async () => {
  const { kindActions } = await import("./jobs.js");
  assert.deepEqual(kindActions({ kept: true, kind: null }), [
    { kind: "original", label: "Confirm original" },
    { kind: "change_order", label: "Confirm change order" },
  ]);
  assert.deepEqual(kindActions({ kept: true, kind: "original" }), [
    { kind: "change_order", label: "Change to change order" },
  ]);
  assert.deepEqual(kindActions({ kept: true, kind: "change_order" }), [
    { kind: "original", label: "Change to original" },
  ]);
  assert.deepEqual(kindActions({ kept: false, kind: null }), []);
});

test("the review queue opens on the chosen estimate, or at its start with one sentence", async () => {
  const { startIndex, missingSentence, estimateOption } = await import("./jobs.js");
  const entries = [{ estimate_id: "a" }, { estimate_id: "b" }, { estimate_id: "c" }];
  assert.deepEqual(startIndex(entries, "b"), { index: 1, missing: false });
  assert.deepEqual(startIndex(entries, "c"), { index: 2, missing: false });
  assert.deepEqual(startIndex(entries, "zz"), { index: 0, missing: true });
  assert.deepEqual(startIndex(entries, null), { index: 0, missing: false });
  assert.deepEqual(startIndex(entries, undefined), { index: 0, missing: false });
  assert.deepEqual(startIndex([], "a"), { index: 0, missing: false });
  assert.equal(
    missingSentence("EST6115758"),
    "EST6115758 is not waiting for review any more, so the queue opens at its first estimate.",
  );
  assert.equal(
    estimateOption({ external_id: "EST6115758", name: "67 Elm Street | Parking Lot", price: "519173.72" }),
    "EST6115758 · 67 Elm Street | Parking Lot · 519,173.72",
  );
});

test("the board's words: estimate under the name, days since activity, applied-to (F08)", async () => {
  const { filterQuery, daysWords, figureOr, appliedWords } = await import("./jobs.js");
  assert.equal(filterQuery({ status: "", division_id: "", revenue_method: "", no_link: false }), "");
  assert.equal(filterQuery({ status: "sold", division_id: "", revenue_method: "", no_link: true }), "?status=sold&no_link=true");
  assert.equal(daysWords(0), "0");
  assert.equal(daysWords(46), "46");
  assert.equal(daysWords(null), "No activity");
  assert.equal(figureOr("-16494.48", null, "Not decided"), "(16,494.48)");
  assert.equal(figureOr(null, "Not shown: Pool has no contract.", "Not decided"), "Not shown: Pool has no contract.");
  assert.equal(figureOr(null, null, "Not decided"), "Not decided");
  assert.equal(appliedWords([]), "Nothing");
  assert.equal(appliedWords([{ document: "EST6115758_PMT2", amount: "149800.00" }, { document: "CM-1", amount: "-100.00" }]), "EST6115758_PMT2: 149,800.00; CM-1: (100.00)");
});

test("the tie-out line reads checking until its own request answers, then the API's words (F08.2)", async () => {
  const { tieOutText, pendingLabel, PENDING_WORDS } = await import("./jobs.js");
  assert.equal(tieOutText(null), "Tie-out: checking…");
  assert.equal(tieOutText(undefined), "Tie-out: checking…");
  assert.equal(tieOutText(false), "Tie-out: could not be checked. Refresh the page.");
  assert.equal(tieOutText({ status: "Ties to the cent to the QuickBooks month totals (308 months)." }), "Tie-out: Ties to the cent to the QuickBooks month totals (308 months).");
  // Link and Unlink: the pressed control says what is happening; the others keep their word.
  // F07.4 added Approve and Withdraw approval to the same convention.
  assert.deepEqual(PENDING_WORDS, {
    link: "Linking…",
    unlink: "Unlinking…",
    approve: "Approving…",
    withdraw: "Withdrawing…",
    assign: "Assigning…",
    clear: "Clearing…",
    assign_all: "Assigning…",
  });
  assert.equal(pendingLabel("link", null, "201"), null);
  assert.equal(pendingLabel("link", { id: "201", action: "link" }, "201"), "Linking…");
  assert.equal(pendingLabel("link", { id: "201", action: "link" }, "202"), null);
  assert.equal(pendingLabel("unlink", { id: "a1", action: "unlink" }, "a1"), "Unlinking…");
  assert.equal(pendingLabel("unlink", { id: "a1", action: "link" }, "a1"), null);
});


// --- F07.4 (D-42) ----------------------------------------------------------------------

test("approval words and actions: a confirmed change order is approved or not; only two roles act", async () => {
  const { approvalActions, canApproveChangeOrders, kindActions, pendingLabel } = await import("./jobs.js");
  assert.equal(canApproveChangeOrders("client_pm"), true);
  assert.equal(canApproveChangeOrders("firm_admin"), true);
  assert.equal(canApproveChangeOrders("client_admin"), false);
  assert.equal(canApproveChangeOrders("firm_staff"), false);
  assert.equal(canApproveChangeOrders("client_viewer"), false);
  const unapproved = { kept: true, kind: "change_order", approved: false, approval_label: "Change order, not approved" };
  const approved = { kept: true, kind: "change_order", approved: true, approval_label: "Change order, approved 2026-09-14 by Dane" };
  assert.deepEqual(approvalActions(unapproved), [{ action: "approve", label: "Approve" }]);
  assert.deepEqual(approvalActions(approved), [{ action: "withdraw", label: "Withdraw approval" }]);
  assert.deepEqual(approvalActions({ kept: true, kind: "original", approved: false, approval_label: null }), []);
  assert.deepEqual(approvalActions({ kept: true, kind: null, approved: false, approval_label: null }), []);
  assert.deepEqual(approvalActions({ kept: false, kind: null, approved: false, approval_label: null }), []);
  // An approved change order offers no "Change to original": the server refuses it too.
  assert.deepEqual(kindActions(approved), []);
  assert.deepEqual(kindActions(unapproved), [{ kind: "original", label: "Change to original" }]);
  // The busy labels follow the F08.2 convention.
  assert.equal(pendingLabel("approve", { id: "w1", action: "approve" }, "w1"), "Approving…");
  assert.equal(pendingLabel("withdraw", { id: "w1", action: "withdraw" }, "w1"), "Withdrawing…");
  assert.equal(pendingLabel("approve", { id: "w1", action: "approve" }, "w2"), null);
});

test("an approval is ready with the agreed date, and a reference only when the policy requires one", async () => {
  const { approvalReady } = await import("./jobs.js");
  assert.equal(approvalReady({ agreed_on: "", evidence_ref: "" }, false), false);
  assert.equal(approvalReady({ agreed_on: "2026-09-14", evidence_ref: "" }, false), true);
  assert.equal(approvalReady({ agreed_on: "2026-09-14", evidence_ref: "  " }, true), false);
  assert.equal(approvalReady({ agreed_on: "2026-09-14", evidence_ref: "e-mail" }, true), true);
});

test("F08.1: suggested pairs are ids of offered, untied lines; a pick beats the suggestion", async () => {
  const { suggestedPairs, pickedWorkArea, lineAction } = await import("./jobs.js");
  const lines = [
    { billing_line_id: "l1", offered: true, work_area_id: null, suggested_work_area_id: "w1", how: "none" },
    { billing_line_id: "l2", offered: true, work_area_id: "w3", suggested_work_area_id: null, how: "number" },
    { billing_line_id: "l3", offered: true, work_area_id: null, suggested_work_area_id: null, how: "none" },
    { billing_line_id: "l4", offered: false, work_area_id: null, suggested_work_area_id: "w1", how: "none" },
    { billing_line_id: "l5", offered: true, work_area_id: "w2", suggested_work_area_id: null, how: "assigned" },
  ];
  assert.deepEqual(suggestedPairs(lines), [{ billing_line_id: "l1", estimate_work_area_id: "w1" }]);
  assert.deepEqual(suggestedPairs([]), []);
  assert.equal(pickedWorkArea(lines[0], {}), "w1");
  assert.equal(pickedWorkArea(lines[0], { l1: "w9" }), "w9");
  assert.equal(pickedWorkArea(lines[2], {}), "");
  assert.deepEqual(lineAction(lines[0]), { action: "assign", label: "Assign" });
  assert.equal(lineAction(lines[1]), null);
  assert.equal(lineAction(lines[3]), null);
  assert.deepEqual(lineAction(lines[4]), { action: "clear", label: "Clear" });
});
