import { useCallback, useEffect, useState } from "react";
import { api, getMe } from "./api.js";
import Login from "./pages/Login.jsx";
import ResetPassword from "./pages/ResetPassword.jsx";
import Shell from "./pages/Shell.jsx";
import TotpEnrol from "./pages/TotpEnrol.jsx";
import TotpVerify from "./pages/TotpVerify.jsx";

// Unauthenticated users see only the login page (or the reset-link page).
// Everything the client knows about the session comes from /api/session/me.
export default function App() {
  const [me, setMe] = useState(undefined); // undefined: loading; null: signed out

  const refresh = useCallback(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null));
  }, []);

  useEffect(refresh, [refresh]);

  async function logout() {
    try {
      await api("POST", "/api/auth/logout");
    } finally {
      setMe(null);
    }
  }

  if (window.location.pathname === "/reset-password") {
    const token = new URLSearchParams(window.location.search).get("token") || "";
    return <ResetPassword token={token} />;
  }
  if (me === undefined) return <p style={{ fontFamily: "system-ui, sans-serif", padding: "2rem" }}>Loading…</p>;
  if (me === null) return <Login onLoggedIn={refresh} />;
  if (me.totp === "enrol_required") return <TotpEnrol me={me} onEnrolled={refresh} onLogout={logout} />;
  if (me.totp === "verify_required") return <TotpVerify me={me} onVerified={refresh} onLogout={logout} />;
  return <Shell me={me} onChanged={refresh} onLogout={logout} />;
}
