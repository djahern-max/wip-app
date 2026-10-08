// A role as a person reads it (D-22: no machine words on screen; F09.1). The codes are
// compared elsewhere, never shown.

const ROLE_WORDS = {
  firm_admin: "Firm admin",
  firm_staff: "Firm staff",
  client_admin: "Client admin",
  client_pm: "Project manager",
  client_viewer: "Viewer",
};

/** The words for a role code; an unknown code is shown as plain words, never raw. */
export function roleWords(role) {
  if (!role) return "";
  if (ROLE_WORDS[role]) return ROLE_WORDS[role];
  const words = String(role).replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
