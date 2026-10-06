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
  if (area.approved) return []; // F07.4: withdraw the approval first (the server refuses too)
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

// --- F08: the Sold Jobs Board -----------------------------------------------------------

// The board's filters as the query string the list and the exports share.
export function filterQuery(filters) {
  const q = new URLSearchParams();
  if (filters.status) q.set("status", filters.status);
  if (filters.division_id) q.set("division_id", filters.division_id);
  if (filters.revenue_method) q.set("revenue_method", filters.revenue_method);
  if (filters.no_link) q.set("no_link", "true");
  const qs = q.toString();
  return qs ? `?${qs}` : "";
}

// Days since the last billing or payment: a count, or the words.
export function daysWords(days) {
  return days === null || days === undefined ? "No activity" : String(days);
}

// A money figure the board may leave out: the note the API gives, or the page's words.
export function figureOr(value, note, words) {
  return amountOr(value, note || words);
}

// The payment history's "applied to" cell.
export function appliedWords(applied) {
  if (!applied || applied.length === 0) return "Nothing";
  return applied.map((a) => `${a.document}: ${formatMoney(a.amount)}`).join("; ");
}

// --- F08.2 ---------------------------------------------------------------------------

// The tie-out line (item 2): the board's rows show first; the status is its own
// request. null: not answered yet; false: the request failed; else the API's words.
export function tieOutText(tie) {
  if (tie === null || tie === undefined) return "Tie-out: checking…";
  if (tie === false) return "Tie-out: could not be checked. Refresh the page.";
  return `Tie-out: ${tie.status}`;
}

// The busy state of Link and Unlink (item 4; D-22): the pressed control reads what is
// happening while its request is in flight; the others are only disabled.
// `pending` is {id, action} or null; the id is the QuickBooks row's external id for
// a link and the alias id for an unlink.
export const PENDING_WORDS = {
  link: "Linking…",
  unlink: "Unlinking…",
  approve: "Approving…", // F07.4
  withdraw: "Withdrawing…",
};

export function pendingLabel(action, pending, id) {
  if (!pending || pending.action !== action || pending.id !== id) return null;
  return PENDING_WORDS[action] || null;
}

// --- F07.4 (D-42): change order approval ---------------------------------------------------

// The roles that approve a change order and withdraw an approval (can_approve_change_orders
// in app/core/authz.py; the owner's answer A). The server enforces it; this only decides
// whether the controls are shown.
export const APPROVER_ROLES = ["firm_admin", "client_pm"];

export function canApproveChangeOrders(role) {
  return APPROVER_ROLES.includes(role);
}

// The approval control of a work-area row: Approve for a change order without an applying
// approval, Withdraw approval for one with; nothing for an original, an unconfirmed or an
// omitted row (the row's approval_label is null then).
export function approvalActions(area) {
  if (!area.kept || !area.approval_label) return [];
  return area.approved
    ? [{ action: "withdraw", label: "Withdraw approval" }]
    : [{ action: "approve", label: "Approve" }];
}

// The approval form is ready with the date the customer agreed, and a reference when the
// company's policy requires one (the server refuses either way; this disables the button).
export function approvalReady(form, referenceRequired) {
  if (!form.agreed_on) return false;
  if (referenceRequired && !(form.evidence_ref && form.evidence_ref.trim())) return false;
  return true;
}
