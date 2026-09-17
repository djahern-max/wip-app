import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { api } from "../api.js";
import { styles } from "./Login.jsx";

// Firm roles must enrol before anything else works. The secret is shown exactly
// once (QR plus manual key); recovery codes are shown exactly once after confirmation.
export default function TotpEnrol({ me, onEnrolled, onLogout }) {
  const [setup, setSetup] = useState(null); // { secret, otpauth_uri }
  const [qr, setQr] = useState(null);
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api("POST", "/api/auth/totp/enrol")
      .then(async (s) => {
        if (cancelled) return;
        setSetup(s);
        setQr(await QRCode.toDataURL(s.otpauth_uri, { margin: 1, width: 200 }));
      })
      .catch((err) => setError(err.detail || "Could not start enrolment."));
    return () => {
      cancelled = true;
    };
  }, []);

  async function confirm(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api("POST", "/api/auth/totp/enrol/confirm", { code });
      setRecoveryCodes(r.recovery_codes);
    } catch (err) {
      setError(err.status === 429 ? "Too many failed attempts. Try again in 15 minutes." : "That code was not accepted. Check the time on your device and try again.");
    } finally {
      setBusy(false);
    }
  }

  if (recoveryCodes) {
    return (
      <main style={styles.page}>
        <div style={styles.card}>
          <h1 style={{ marginTop: 0 }}>Recovery codes</h1>
          <p>Each code works once, if you lose your authenticator. They are shown only now. Store them somewhere safe.</p>
          <ol style={styles.code}>
            {recoveryCodes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ol>
          <button type="button" style={styles.button} onClick={onEnrolled}>I have saved these codes</button>
        </div>
      </main>
    );
  }

  return (
    <main style={styles.page}>
      <form onSubmit={confirm} style={styles.card}>
        <h1 style={{ marginTop: 0 }}>Set up two-factor</h1>
        <p style={styles.hint}>{me.user.email}. Your role requires an authenticator app.</p>
        {!setup && !error && <p>Preparing…</p>}
        {setup && (
          <>
            {qr && <img src={qr} alt="Scan with your authenticator app" width={200} height={200} />}
            <p style={styles.hint}>Or enter this key manually:</p>
            <p style={styles.code}>{setup.secret.match(/.{1,4}/g).join(" ")}</p>
            <label style={styles.label}>
              Code from the app
              <input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                style={{ ...styles.input, ...styles.code }}
              />
            </label>
          </>
        )}
        {error && <p style={styles.error}>{error}</p>}
        {setup && (
          <button type="submit" disabled={busy} style={styles.button}>Confirm</button>
        )}
        <p>
          <button type="button" style={styles.linkButton} onClick={onLogout}>Sign out</button>
        </p>
      </form>
    </main>
  );
}
