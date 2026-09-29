// Configuration lists hide inactive rows by default (F06.1, D-22). Nothing is deleted:
// a "Show inactive (n)" control above the table shows them again. The choice is page
// state only (not stored, not per user); the API still returns every row.

export function inactiveCount(rows) {
  return (rows || []).filter((r) => r.active === false).length;
}

export function visibleRows(rows, showInactive) {
  const all = rows || [];
  return showInactive ? all : all.filter((r) => r.active !== false);
}

export function inactiveLabel(count, showing) {
  return `${showing ? "Hide" : "Show"} inactive (${count})`;
}

// The empty-table sentence: a list with only inactive rows says so rather than
// looking empty.
export function emptyMessage(rows, noun) {
  return (rows || []).length === 0 ? `No ${noun} yet.` : `No active ${noun}. Use "Show inactive" to see the others.`;
}
