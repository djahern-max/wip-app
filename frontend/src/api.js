// Thin fetch wrapper. Every request carries the CSRF header the API requires on
// state-changing routes, and the session cookie (HttpOnly; never touched here).
// Nothing auth-related is stored in local storage.
export const CSRF_HEADER = "X-Requested-With";

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(method, path, body) {
  const headers = { [CSRF_HEADER]: "fetch" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  let data = null;
  if (r.status !== 204) {
    try {
      data = await r.json();
    } catch {
      data = null;
    }
  }
  if (!r.ok) throw new ApiError(r.status, data && data.detail);
  return data;
}

// Multipart upload (F03). Same CSRF header and cookie handling as api(); the
// browser sets the multipart Content-Type itself. Never called from an effect.
export async function upload(path, formData) {
  const r = await fetch(path, {
    method: "POST",
    headers: { [CSRF_HEADER]: "fetch" },
    body: formData,
    credentials: "same-origin",
  });
  let data = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!r.ok) throw new ApiError(r.status, data && data.detail);
  return data;
}

export const getMe = () => api("GET", "/api/session/me");
