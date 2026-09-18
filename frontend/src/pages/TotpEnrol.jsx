import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { api } from "../api.js";

// Firm roles must enrol before anything else works. The secret is shown exactly
// once (QR plus manual key); recovery codes are shown exactly once after confirmation.
export default function TotpEnrol({ me, onEnrolled, onLogout }) {
  const [setup, setSetup] = useState(null); // { secret, otpauth_uri }
  const [qr, setQr] = useState(null);
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  // One enrol request per mounted page. React StrictMode runs mount effects twice in
  // dev (mount, cleanup, mount); the ref survives that, so the second run reuses the
  // first run's request instead of sending another. The server is idempotent as well.
  const enrolRequest = useRef(null);

  useEffect(() => {
    let cancelled = false;
    if (!enrolRequest.current) enrolRequest.current = api("POST", "/api/auth/totp/enrol");
    enrolRequest.current
      .then(async (s) => {
        if (cancelled) return;
        setSetup(s);
        const dataUrl = await QRCode.toDataURL(s.otpauth_uri, { margin: 1, width: 200 });
        if (!cancelled) setQr(dataUrl);
      })
      .catch((err) => {
        if (!cancelled) setError("Two-factor setup could not start. Reload the page; if it happens again, ask your firm administrator for a new link.");
      });
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
      <main className="page">
        <div className="card">
          <h1>Recovery codes</h1>
          <p>Each code works once, if you lose your authenticator. They are shown only now. Store them somewhere safe.</p>
          <ol className="code">
            {recoveryCodes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ol>
          <button type="button" className="button-primary" onClick={onEnrolled}>I have saved these codes</button>
        </div>
      </main>
    );
  }

  return (
    <main className="page">
      <form onSubmit={confirm} className="card">
        <h1>Set up two-factor</h1>
        <p className="hint">{me.user.email}. Your role requires an authenticator app.</p>
        {!setup && !error && <p>Preparing…</p>}
        {setup && (
          <>
            {qr && <img src={qr} alt="Scan with your authenticator app" width={200} height={200} />}
            <p className="hint">Or enter this key manually:</p>
            <p className="code">{setup.secret.match(/.{1,4}/g).join(" ")}</p>
            <label className="label">
              Code from the app
              <input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                className="input code"
              />
            </label>
          </>
        )}
        {error && <p className="error">{error}</p>}
        {setup && (
          <button type="submit" disabled={busy} className="button-primary">Confirm code</button>
        )}
        <p>
          <button type="button" className="link-button" onClick={onLogout}>Sign out</button>
        </p>
      </form>
    </main>
  );
}
