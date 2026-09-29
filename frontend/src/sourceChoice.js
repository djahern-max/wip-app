// The Source chosen on Imports, remembered per company for the browser session. The
// first visit per company per session opens on "Choose a source" (F06.1; F05 and F06
// Discovered: a default source cost the owner one upload), and Upload waits for a choice.
// Imports is unmounted when another screen is opened and on a page refresh; without
// this the choice fell back to the first source and the next file went up under the
// wrong one (owner browser pass, 2026-09-19). The key carries the tenant id: a
// choice made for one company is never offered for another. Storage can be
// unavailable (private mode): every access is guarded and the page works without it.

export const NO_SOURCE = ""; // the "Choose a source" option

const key = (tenantId) => `imports.source:${tenantId}`;

export function rememberSource(storage, tenantId, name) {
  if (!storage || !tenantId || !name) return;
  try {
    storage.setItem(key(tenantId), name);
  } catch {
    // nothing to do: the choice lasts as long as the screen
  }
}

export function rememberedSource(storage, tenantId) {
  if (!storage || !tenantId) return null;
  try {
    return storage.getItem(key(tenantId));
  } catch {
    return null;
  }
}

// The source to show: the current one if the server still offers it, else the one
// remembered for this company in this session, else none ("Choose a source"). `kinds`
// empty = not loaded yet, so nothing is ruled out.
export function chooseSource(kinds, current, remembered) {
  const offered = (name) => !!name && (kinds.length === 0 || kinds.some((k) => k.name === name));
  if (offered(current)) return current;
  if (offered(remembered)) return remembered;
  return NO_SOURCE;
}

export function sessionStore() {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}
