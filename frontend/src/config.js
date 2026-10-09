// Configuration (F09.4): the pure pieces of the six sections. The API decides every
// count and every label; this only reads what the page already holds and turns it into
// words (D-22). No request, no DOM, so it is tested with node --test.

/** True when there are active accounts and none is left without a confirmed mapping. */
export function allConfirmed(data) {
  return Boolean(data) && data.total_active > 0 && data.unmapped_count === 0;
}

/** The sentence beside "Done." on Accounts. */
export function confirmedSentence(totalActive) {
  return totalActive === 1 ? "The one active account is confirmed." : `All ${totalActive} active accounts are confirmed.`;
}

/** Under the Unmapped figure: how many of the unmapped accounts carry a suggestion. */
export function suggestedNote(suggested) {
  return `${suggested} suggested`;
}

/** Beside the Policy sentence: how many keys are decided. */
export function decidedWords(rows) {
  const all = rows || [];
  return `${all.filter((p) => p.decided).length} of ${all.length} decided`;
}
