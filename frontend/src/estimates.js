// Estimates (F09.2): the pure pieces of the page. The API decides every figure and every
// sentence; this only counts what the page already holds and turns the count into words
// (D-22). No request, no DOM, so it is tested with node --test.

/** One sentence under the heading: how many estimates are listed and how many need attention. */
export function listSentence(estimates) {
  const rows = estimates || [];
  if (rows.length === 0) return "";
  const needing = rows.filter((e) => (e.attention || []).length > 0).length;
  const count = `${rows.length} ${rows.length === 1 ? "estimate" : "estimates"}`;
  if (needing === 0) return `${count}; nothing needs attention.`;
  return `${count}; ${needing} ${needing === 1 ? "needs" : "need"} attention.`;
}

/** The line under an estimate's name: its id in the estimating system and its status in words. */
export function detailLead(d) {
  return `Estimate ${d.external_id} \u00b7 ${d.status_label}`;
}
