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
