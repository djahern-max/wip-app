// Customers picker (F07.2, D-37): the pure pieces of the page, tested without a browser.
// A row is a CustomerPickOut from the API: display_name, kind_label, active, tracked,
// job ({id, name} or null) and needs_job (the review sentence, or null).

/** What the Job column says for one row, and the job to open when there is one. */
export function jobCell(row) {
  if (row.job) return { text: `Job: ${row.job.name}`, jobId: row.job.id };
  if (!row.active) return { text: "Inactive in QuickBooks", jobId: null };
  if (row.needs_job) return { text: row.needs_job, jobId: null };
  return { text: "", jobId: null };
}

/** One sentence above the search results. */
export function resultSentence(query, total) {
  const q = query.trim();
  if (!q) return "Type part of a customer or project name, then search.";
  if (total === 0) return `No active customer or project is named like “${q}”.`;
  return total === 1 ? "1 row." : `${total} rows.`;
}

export function pageLabel(page, pages) {
  return pages > 1 ? `Page ${page} of ${pages}` : "";
}

/** The action a row offers in the search results: Track, or the word that it already is. */
export function trackLabel(row) {
  return row.tracked ? "Tracked" : "Track";
}

/** The action a tracked row offers: Untrack, or why it cannot be untracked here. */
export function untrackLabel(row) {
  return row.job ? "Unlink on the job first" : "Untrack";
}

export function trackedSentence(rows) {
  if (rows.length === 0) {
    return "Nothing is tracked yet. Search below, then track the customers and projects to work on.";
  }
  return rows.length === 1 ? "1 tracked." : `${rows.length} tracked.`;
}

// F08.2 (item 4; D-22): the pressed Track or Untrack control reads what is happening
// while its request is in flight; the others are only disabled. `pending` is
// {id, action} or null, the id the row's customer_id.
export const PENDING_WORDS = { track: "Tracking…", untrack: "Untracking…" };

export function pendingLabel(action, pending, customerId) {
  if (!pending || pending.action !== action || pending.id !== customerId) return null;
  return PENDING_WORDS[action] || null;
}

