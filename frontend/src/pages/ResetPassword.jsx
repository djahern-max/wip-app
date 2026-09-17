import { useState } from "react";
import { api } from "../api.js";
import { styles } from "./Login.jsx";

// Reached from the one-time link a firm administrator hands over (F02: no e-mail).
export default function ResetPassword({ token }) {
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setError(null);
    if (password.length < 12) return setError("Use at least 12 characters.");
    if (password !== again) return setError("The two passwords differ.");
    try {
      await api("POST", "/api/auth/password/reset", { token, new_password: password });
      setDone(true);
    } catch (err) {
      setError(err.detail || "This link is invalid or has expired. Ask your firm administrator for a new one.");
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
