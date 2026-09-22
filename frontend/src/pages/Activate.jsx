import { useState } from "react";
import { api } from "../api.js";

// Reached from the one-time activation link a firm administrator hands over
// (D-16: no e-mail). The token arrives in the URL fragment (App.jsx reads it; the
// address keeps it until the link has been used, so a reload of this page still
// works) and is sent only in the POST body. Sets the password; for a firm user the
// server opens an enrolment-only session and the app continues into TOTP enrolment
// and the recovery codes on "/", so the account becomes usable only once that is
// done. A reload during enrolment re-requests the same pending secret from the
// server (start_totp_enrolment is idempotent).
export default function Activate({ token }) {
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError(null);
    if (!token) return setError("This page needs the activation link. Open the link you were given.");
    if (password.length < 12) return setError("Use at least 12 characters.");
    if (password !== again) return setError("The two passwords differ.");
    // The token works once: a second post would answer "invalid link" over a success.
    setBusy(true);
    try {
      const r = await api("POST", "/api/auth/activate", { token, new_password: password });
      // The link is used: drop the token from the address now, not before.
      window.history.replaceState(null, "", "/activate");
      if (r && r.next === "totp_enrol") {
        window.location.assign("/"); // stays busy while the page reloads
        return;
      }
      setDone(true);
      setBusy(false);
    } catch (err) {
      setBusy(false);
      setError(
        err.status === 429
          ? "Too many attempts from this address. Try again in 15 minutes."
          : "This link is invalid, used, or has expired. Ask your firm administrator for a new one.",
      );
    }
  }

  return (
    <main className="page">
      <form onSubmit={submit} className="card">
        <h1>Set your password</h1>
        {done ? (
          <p>
            Done. <a href="/">Sign in</a>.
          </p>
        ) : (
          <>
            <label className="label">
              New password (12+ characters)
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required className="input" />
            </label>
            <label className="label">
              Again
              <input type="password" value={again} onChange={(e) => setAgain(e.target.value)} required className="input" />
            </label>
            {error && <p className="error">{error}</p>}
            <button type="submit" disabled={busy} className="button-primary">{busy ? "Saving…" : "Save password"}</button>
          </>
        )}
      </form>
    </main>
  );
}
