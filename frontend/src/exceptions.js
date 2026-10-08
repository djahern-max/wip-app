// Pure helpers for the Exceptions page and the company picker (F09; D-22, D-46). No
// request, no DOM: everything here is a function of what the API returned, so it is
// tested with node --test. Severity and status reach the screen as the API's words
// (severity_label, status_label); the codes are compared, never shown.

export const PENDING_WORDS = {
  assign: "Assigning…",
  note: "Adding…",
  dismiss: "Dismissing…",
  reopen: "Reopening…",
};

export function pendingLabel(action, pending, id) {
  if (!pending || pending.action !== action || pending.id !== id) return null;
  return PENDING_WORDS[action] || null;
}

// The filter query from the three controls; an empty control sends nothing.
export function filterQuery({ status, severity, mine }) {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (severity) q.set("severity", severity);
  if (mine) q.set("mine", "true");
  const s = q.toString();
  return s ? `?${s}` : "";
}

// "n open" for the Exceptions link; the words carry the number, colour never does.
export function openWords(counts) {
  if (!counts) return "";
  const n = (counts.block_close || 0) + (counts.warn || 0) + (counts.info || 0);
  return `${n} open`;
}

// The company picker's words for one company (firm users see every company's):
// "12 open, 2 block the close" or "nothing open".
export function pickerWords(counts) {
  if (!counts) return "";
  const n = (counts.block_close || 0) + (counts.warn || 0) + (counts.info || 0);
  if (n === 0) return "nothing open";
  const block = counts.block_close || 0;
  const blocks = block === 0 ? "" : `, ${block} ${block === 1 ? "blocks" : "block"} the close`;
  return `${n} open${blocks}`;
}

// A timestamp from the API (ISO, UTC) as a date for a person: YYYY-MM-DD.
export function dayOf(iso) {
  if (!iso) return "";
  return String(iso).slice(0, 10);
}

// One line for an event in an exception's history, who and when first (D-22).
export function eventLine(e) {
  const who = e.actor || "The platform";
  const day = dayOf(e.occurred_at);
  if (e.kind === "assigned") {
    return `${who} assigned it to ${e.assigned_to || "nobody"} on ${day}.`;
  }
  if (e.kind === "note") return `${who} on ${day}: ${e.text}`;
  if (e.kind === "dismissed") return `${who} dismissed it on ${day}: ${e.text}`;
  if (e.kind === "reopened") {
    if (e.actor) return `${who} reopened it on ${day}.`;
    return `Opened again on ${day}: what the sentence states changed.`;
  }
  if (e.kind === "resolved") return `Resolved on ${day}: no longer raised.`;
  if (e.kind === "raised_again") return `Raised again on ${day}.`;
  return `Raised on ${day}.`;
}

// The sentence of a dismissed exception as the job and estimate pages list it.
export function dismissedLine(d) {
  const who = d.dismissed_by || "a user";
  return `Dismissed by ${who} on ${dayOf(d.dismissed_at)}: ${d.note}`;
}

// A note must have words before it is sent (the API refuses a blank one too).
export function noteReady(text) {
  return Boolean(text && text.trim());
}

// Which controls a row offers (the server enforces the roles; this decides what shows).
export function rowActions(e, canManage) {
  if (!canManage) return [];
  const out = [];
  if (e.status === "open") {
    out.push({ action: "assign", label: "Assign" });
    if (e.may_dismiss) out.push({ action: "dismiss", label: "Dismiss" });
  }
  if (e.status === "dismissed") out.push({ action: "reopen", label: "Reopen" });
  return out;
}
