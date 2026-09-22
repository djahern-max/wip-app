import { useCallback, useEffect, useState } from "react";
import { api, getMe } from "./api.js";
import Login from "./pages/Login.jsx";
import Activate from "./pages/Activate.jsx";
import Shell from "./pages/Shell.jsx";
import TotpEnrol from "./pages/TotpEnrol.jsx";
import TotpVerify from "./pages/TotpVerify.jsx";

// Unauthenticated users see only the login page (or the activation-link page).
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

  if (window.location.pathname === "/activate") {
    // The token is in the fragment (never sent to a server); the page posts it in the
    // request body. It stays in the address until the link has been used, so a reload
    // before "Save password" still has it (F05.1 small fix: clearing it on first read
    // left a reloaded page with nothing, and only a new link could recover).
    const token = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("token") || "";
    return <Activate token={token} />;
  }
  if (me === undefined) return <p className="main">Loading…</p>;
  if (me === null) return <Login onLoggedIn={refresh} />;
  if (me.totp === "enrol_required") return <TotpEnrol me={me} onEnrolled={refresh} onLogout={logout} />;
  if (me.totp === "verify_required") return <TotpVerify me={me} onVerified={refresh} onLogout={logout} />;
  return <Shell me={me} onChanged={refresh} onLogout={logout} />;
}
