import { useState } from "react";
import { api } from "../api.js";
import { styles } from "./Login.jsx";

// Reached from the one-time activation link a firm administrator hands over
// (D-16: no e-mail). The token arrives in the URL fragment (App.jsx reads it and
// clears the address bar) and is sent only in the POST body. Sets the password;
// for a firm user the server opens an enrolment-only session and the app continues
// into TOTP enrolment and the recovery codes on "/", so the account becomes usable
// only once that is done.
export default function Activate({ token }) {
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setError(null);
    if (!token) return setError("This page needs the activation link. Open the link you were given.");
    if (password.length < 12) return setError("Use at least 12 characters.");
    if (password !== again) return setError("The two passwords differ.");
    try {
      const r = await api("POST", "/api/auth/activate", { token, new_password: password });
      if (r && r.next === "totp_enrol") {
        window.location.assign("/");
        return;
      }
      setDone(true);
    } catch (err) {
      setError(
        err.status === 429
          ? "Too many attempts from this address. Try again in 15 minutes."
          : "This link is invalid, used, or has expired. Ask your firm administrator for a new one.",
      );
    }
  }

  return (
    <main style={styles.page}>
      <form onSubmit={submit} style={styles.card}>
        <h1 style={{ marginTop: 0 }}>Set your password</h1>
        {done ? (
          <p>
            Done. <a href="/">Sign in</a>.
          </p>
        ) : (
          <>
            <label style={styles.label}>
              New password (12+ characters)
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required style={styles.input} />
            </label>
            <label style={styles.label}>
              Again
              <input type="password" value={again} onChange={(e) => setAgain(e.target.value)} required style={styles.input} />
            </label>
            {error && <p style={styles.error}>{error}</p>}
            <button type="submit" style={styles.button}>Save</button>
          </>
        )}
      </form>
    </main>
  );
}
