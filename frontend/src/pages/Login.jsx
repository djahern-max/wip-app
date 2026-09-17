import { useState } from "react";
import { api } from "../api.js";
import { PRODUCT_NAME } from "../product.js";

export default function Login({ onLoggedIn }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("POST", "/api/auth/login", { email, password });
      setPassword("");
      onLoggedIn();
    } catch (err) {
      setError(err.status === 429 ? "Too many failed attempts. Try again in 15 minutes." : "Invalid e-mail or password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={styles.page}>
      <form onSubmit={submit} style={styles.card}>
        <h1 style={{ marginTop: 0 }}>{PRODUCT_NAME}</h1>
        <p>Sign in</p>
        <label style={styles.label}>
          E-mail
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus style={styles.input} />
        </label>
        <label style={styles.label}>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required style={styles.input} />
        </label>
        {error && <p style={styles.error}>{error}</p>}
        <button type="submit" disabled={busy} style={styles.button}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p style={styles.hint}>No self-service signup. Ask your firm administrator for an account or a reset link.</p>
      </form>
    </main>
  );
}

export const styles = {
  page: { fontFamily: "system-ui, sans-serif", display: "flex", justifyContent: "center", padding: "4rem 1rem" },
  card: { width: "100%", maxWidth: 380, border: "1px solid #ddd", borderRadius: 8, padding: "1.5rem", background: "#fff" },
  label: { display: "block", margin: "0.75rem 0", fontSize: 14 },
  input: { display: "block", width: "100%", boxSizing: "border-box", padding: "0.5rem", marginTop: 4, fontSize: 16 },
  button: { padding: "0.6rem 1rem", fontSize: 16, cursor: "pointer" },
  linkButton: { background: "none", border: "none", color: "#06c", cursor: "pointer", padding: 0, fontSize: 14 },
  error: { color: "crimson" },
  hint: { color: "#666", fontSize: 13 },
  code: { fontFamily: "ui-monospace, monospace", fontSize: 15, letterSpacing: 1 },
};
