import { useState } from "react";
import { api } from "../api.js";
import { styles } from "./Login.jsx";

export default function TotpVerify({ me, onVerified, onLogout }) {
  const [code, setCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("POST", useRecovery ? "/api/auth/totp/recover" : "/api/auth/totp/verify", { code });
      setCode("");
      onVerified();
    } catch (err) {
      setError(err.status === 429 ? "Too many failed attempts. Try again in 15 minutes." : "That code was not accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={styles.page}>
      <form onSubmit={submit} style={styles.card}>
        <h1 style={{ marginTop: 0 }}>Two-factor check</h1>
        <p style={styles.hint}>Signed in as {me.user.email}</p>
        <label style={styles.label}>
          {useRecovery ? "Recovery code" : "Code from your authenticator app"}
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode={useRecovery ? "text" : "numeric"}
            autoComplete="one-time-code"
            autoFocus
            required
            style={{ ...styles.input, ...styles.code }}
          />
        </label>
        {error && <p style={styles.error}>{error}</p>}
        <button type="submit" disabled={busy} style={styles.button}>Verify</button>
        <p>
          <button type="button" style={styles.linkButton} onClick={() => setUseRecovery(!useRecovery)}>
            {useRecovery ? "Use my authenticator app instead" : "Use a recovery code instead"}
          </button>
        </p>
        <p>
          <button type="button" style={styles.linkButton} onClick={onLogout}>Sign out</button>
        </p>
      </form>
    </main>
  );
}
