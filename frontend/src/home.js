// Home (F07.3): the pure pieces of the page. The API decides the lines, their order,
// which one is primary and which links a role may open; this only turns them into words
// a person reads (D-22) and tells the Shell where a link goes.

const SECTION_NAMES = {
  accounts: "Accounts",
  divisions: "Divisions",
  burden: "Burden rates",
  policy: "Policy",
};

/** The text on a link control, from the API's link. */
export function linkLabel(link) {
  if (!link) return "";
  switch (link.page) {
    case "connections":
      return "Open Connections";
    case "imports":
      return "Open Imports";
    case "config":
      return `Open Configuration, ${SECTION_NAMES[link.section] || "Accounts"}`;
    case "jobs":
      if (link.review) return "Review sold estimates";
      return link.job_id ? "Open the job" : "Open Jobs";
    case "customers":
      return "Open Customers";
    case "estimates":
      return "Open Estimates";
    default:
      return "";
  }
}

export function doneWord(done) {
  return done ? "Done" : "Not done";
}

/** How many lines claim the primary action; the API promises at most one. */
export function primaryCount(setup) {
  return (setup || []).filter((l) => l.primary).length;
}

/** One sentence above the checklist. */
export function setupSentence(setup) {
  const open = (setup || []).filter((l) => !l.done).length;
  if (open === 0) return "Set-up is complete.";
  return open === 1 ? "1 set-up step is not done." : `${open} set-up steps are not done.`;
}

/** One sentence above the jobs table. */
export function jobsSentence(jobs) {
  if (jobs.length === 0) return "No job is open yet.";
  const needing = jobs.filter((j) => j.code !== null).length;
  if (needing === 0) return jobs.length === 1 ? "1 job, nothing needed." : `${jobs.length} jobs, nothing needed.`;
  return `${jobs.length} ${jobs.length === 1 ? "job" : "jobs"}; ${needing} ${needing === 1 ? "needs" : "need"} something.`;
}
