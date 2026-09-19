// The Source chosen on Imports, remembered per company for the browser session.
// Imports is unmounted when another screen is opened and on a page refresh; without
// this the choice fell back to the first source and the next file went up under the
// wrong one (owner browser pass, 2026-09-19). The key carries the tenant id: a
// choice made for one company is never offered for another. Storage can be
// unavailable (private mode): every access is guarded and the page works without it.

export const DEFAULT_SOURCE = "unparsed_file";

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

// The source to show: the current one if the server still offers it, else the
// remembered one, else the default, else the first offered. `kinds` empty = not
// loaded yet, so nothing is ruled out.
export function chooseSource(kinds, current, remembered) {
  const offered = (name) => !!name && (kinds.length === 0 || kinds.some((k) => k.name === name));
  if (offered(current)) return current;
  if (offered(remembered)) return remembered;
  if (offered(DEFAULT_SOURCE)) return DEFAULT_SOURCE;
  return kinds.length ? kinds[0].name : DEFAULT_SOURCE;
}

export function sessionStore() {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}
