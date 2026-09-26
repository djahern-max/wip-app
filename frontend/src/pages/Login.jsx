import { useState } from "react";
import { api } from "../api.js";
import { PRODUCT_NAME } from "../product.js";
import Logo from "../Logo.jsx";

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
    <main className="page">
      <form onSubmit={submit} className="card">
        <Logo size={48} />
        <h1>{PRODUCT_NAME}</h1>
        <p>Sign in</p>
        <label className="label">
          E-mail
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus className="input" />
        </label>
        <label className="label">
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required className="input" />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy} className="button-primary">
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="hint">No self-service signup. Ask your firm administrator for an account or a reset link.</p>
      </form>
    </main>
  );
}
