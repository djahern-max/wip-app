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
  assign: "Assigning…", // F08.1 (D-45)
  clear: "Clearing…",
  assign_all: "Assigning…",
  draft: "Saving…", // F08.1 Part 2 (D-36)
  issue: "Issuing…",
  void: "Voiding…",
  discard: "Discarding…", // F08.3 (the owner's answer B)
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

// --- F08.1 (D-45): invoice lines and their work areas ---------------------------------------

// "Confirm all as suggested" sends the pairs the screen shows as suggested, as ids; the
// server assigns by id and compares no name (the owner's answer 4).
export function suggestedPairs(lines) {
  return (lines || [])
    .filter((ln) => ln.offered && !ln.work_area_id && ln.suggested_work_area_id)
    .map((ln) => ({ billing_line_id: ln.billing_line_id, estimate_work_area_id: ln.suggested_work_area_id }));
}

// The work area a press of Assign sends: the person's pick, else the suggestion, else nothing.
export function pickedWorkArea(line, picks) {
  const picked = picks && picks[line.billing_line_id];
  if (picked) return picked;
  return line.suggested_work_area_id || "";
}

// The action a line offers: Clear for an assigned line, Assign for an offered line with
// no tie; nothing for a line tied by its number or by a pay application, or not offered.
export function lineAction(line) {
  if (!line.offered) return null;
  if (line.how === "assigned") return { action: "clear", label: "Clear" };
  if (!line.work_area_id) return { action: "assign", label: "Assign" };
  return null;
}

// --- F08.1 Part 2 (D-36): the billing request and the pay application -------------------------

// The owner's answer C: a billing request is entered by client_pm as well; issue and void
// follow the roles that manage jobs. The server enforces both; this decides what is shown.
export const REQUEST_ROLES = ["firm_admin", "firm_staff", "client_admin", "client_pm"];
export const ISSUE_ROLES = ["firm_admin", "firm_staff", "client_admin"];

export function canEnterBillingRequest(role) {
  return REQUEST_ROLES.includes(role);
}

export function canIssuePayApplications(role) {
  return ISSUE_ROLES.includes(role);
}

// The request form is ready with a date; the surcharge choice may stay unanswered on a
// draft (issue refuses it in words), so it does not gate the draft.
export function requestReady(form) {
  return Boolean(form && form.application_date);
}

// The request body: percents as the person typed them (strings, never parsed here);
// "" for the surcharge is "not answered" (null), nothing is assumed (D-39).
export function requestBody(form) {
  return {
    application_date: form.application_date,
    surcharge_applies: form.surcharge_applies === "" ? null : form.surcharge_applies === "yes",
    percents: Object.entries(form.percents)
      .filter(([, p]) => p !== "")
      .map(([id, p]) => ({ estimate_work_area_id: id, percent: p })),
  };
}

// "Apply n% to every listed work area" (D-26): the same text in every row.
export function applyToAll(percents, value) {
  const out = {};
  for (const id of Object.keys(percents)) out[id] = value;
  return out;
}

// --- F08.3: the form starts from the draft or the prefill; Enter never submits -------------

// The open draft of a job, if any (a job holds one draft at a time).
export function openDraft(data) {
  return (data && data.applications && data.applications.find((a) => a.status === "draft")) || null;
}

// The billing request form's state: when the job holds a draft, the draft's application
// date, surcharge choice and percents (a listed work area the draft left off takes the
// prefill); otherwise today, no choice, and the prefill the API gives per work area (the
// last issued application's percent, else the percent the tied invoice lines' billing
// stands for, else 0.00). A held line (BILLING_NEGATIVE) stands at its previous percent;
// the entered percent is in the sentence only (the owner's answer A).
export function requestForm(data, today) {
  const draft = openDraft(data);
  const byArea = {};
  if (draft) for (const ln of draft.lines) byArea[ln.work_area_id] = ln.percent_complete;
  const percents = {};
  for (const a of data.schedule) percents[a.id] = byArea[a.id] !== undefined ? byArea[a.id] : a.previous_percent;
  let surcharge = "";
  if (draft && draft.surcharge_applies === true) surcharge = "yes";
  if (draft && draft.surcharge_applies === false) surcharge = "no";
  return {
    application_date: draft ? draft.application_date : today,
    surcharge_applies: surcharge,
    percents,
  };
}

// A form is submitted by its button only: Enter in an input (a percent, the date, the
// one-percent field, a reason) does not submit it. The button still works by keyboard:
// Enter or Space on the focused button is a click, not an implicit submission.
export function blockEnter(event) {
  if (!event || event.key !== "Enter") return false;
  const tag = event.target && event.target.tagName ? String(event.target.tagName).toLowerCase() : "";
  if (tag !== "input") return false;
  event.preventDefault();
  return true;
}
