// Small display rules for the Jobs screens (F07). Pure: no requests, no state.
import { formatMoney } from "./money.js";

// An amount the API may leave out (None = not shown or not computed): the words
// say which, never a dash (D-22: zero is 0.00; a missing figure is said in words).
export function amountOr(value, words) {
  return value === null || value === undefined ? words : formatMoney(value);
}

// The reasons a job or a QuickBooks row was suggested, as one phrase:
// ["by client name only", "by name only"] → "By client name only; by name only".
export function reasonText(reasons) {
  const list = (Array.isArray(reasons) ? reasons : [reasons]).filter(Boolean);
  if (list.length === 0) return "";
  const text = list.join("; ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// Review: the next estimate after skipping one; wraps to the first. Nothing is
// written by skipping.
export function nextIndex(index, count) {
  if (count <= 0) return 0;
  return (index + 1) % count;
}

// Attach: the note is required only for "ignored" (the API says so too).
export function attachReady({ jobId, role, note }) {
  if (!jobId || !role) return false;
  return role !== "ignored" || Boolean(note && note.trim());
}

// The Jobs table's QuickBooks cell (D-35). A QuickBooks project is created when the
// first money moves, so a sold job with no link is backlog, not a problem; from in
// progress on, a missing link is (the attention column says so).
export function qboCell(job) {
  if (job.qbo_linked) return job.qbo_names.join("; ");
  if (job.status === "sold") return "Not yet: sold, no money moved (backlog)";
  if (job.status === "cancelled") return "Not linked";
  return "Not linked: link its project";
}

// Linking offers to set a sold job in progress in the same action (D-35).
export function offersInProgress(job) {
  return job.status === "sold";
}

// Attach: choosing a maintenance or snow program job presets the only role it accepts,
// ignored, and the note (owner, 2026-09-29; D-35). The person may still edit the note.
export const PROGRAM_NOTE = "Maintenance contract; billed as service";

export function chooseJob(attach, jobId, jobs) {
  const job = jobs.find((j) => j.id === jobId);
  if (job && job.revenue_method === "recurring_service") {
    return { jobId, role: "ignored", note: attach.note && attach.note.trim() ? attach.note : PROGRAM_NOTE };
  }
  return { ...attach, jobId };
}

// --- F07.1 -----------------------------------------------------------------------------

// The Action column: what a click on a work-area row will do, each action its own
// control (D-22: words first). Unconfirmed: confirm at either kind. Confirmed: change
// to the other. Omitted: nothing.
export function kindActions(area) {
  if (!area.kept) return [];
  if (area.kind === null || area.kind === undefined) {
    return [
      { kind: "original", label: "Confirm original" },
      { kind: "change_order", label: "Confirm change order" },
    ];
  }
  return area.kind === "original"
    ? [{ kind: "change_order", label: "Change to change order" }]
    : [{ kind: "original", label: "Change to original" }];
}

// Review: where the queue opens. The chosen estimate's position, or the start (with
// `missing` when an id was asked for and is no longer queued: already a job, or no
// longer sold).
export function startIndex(entries, estimateId) {
  if (!estimateId || entries.length === 0) return { index: 0, missing: false };
  const index = entries.findIndex((e) => e.estimate_id === estimateId);
  return index < 0 ? { index: 0, missing: true } : { index, missing: false };
}

export function missingSentence(externalId) {
  return `${externalId} is not waiting for review any more, so the queue opens at its first estimate.`;
}

// One entry of the "Estimate" select: id, name, price.
export function estimateOption(entry) {
  return `${entry.external_id} · ${entry.name} · ${formatMoney(entry.price)}`;
}
