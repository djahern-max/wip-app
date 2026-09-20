import { useEffect, useState } from "react";
import { api } from "../api.js";
import { PRODUCT_NAME } from "../product.js";
import Config from "./Config.jsx";
import Connections from "./Connections.jsx";
import Imports from "./Imports.jsx";

// Roles with can_manage_imports and can_view_tenant_config (app/core/authz.py). The
// server enforces them; this only decides whether to show the links.
const IMPORT_ROLES = ["firm_admin", "firm_staff", "client_admin"];
const CONFIG_ROLES = ["firm_admin", "firm_staff", "client_admin"];
const CONNECTION_ROLES = ["firm_admin", "firm_staff", "client_admin"]; // can_view_connections

// QuickBooks sends the browser back to /connections?result=… (F05). Read it once,
// open that screen, and clear the address so a refresh does not repeat the message.
function returnedFromQuickBooks() {
  if (window.location.pathname !== "/connections") return null;
  const result = new URLSearchParams(window.location.search).get("result") || "";
  window.history.replaceState(null, "", "/");
  return { result };
}

// Signed-in frame: product name, tenant switcher, user, sign-out. The active
// tenant lives in the server-side session; the switcher only asks to change it.
export default function Shell({ me, onChanged, onLogout }) {
  const [tenants, setTenants] = useState([]);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  const [returned, setReturned] = useState(returnedFromQuickBooks);
  const [view, setView] = useState(returned ? "connections" : "home");

  // Leaving a screen forgets the message QuickBooks came back with.
  function go(next) {
    setReturned(null);
    setView(next);
  }

  useEffect(() => {
    api("GET", "/api/session/tenants")
      .then(setTenants)
      .catch(() => setError("The list of companies could not be loaded. Reload the page."));
    fetch("/api/health").then((r) => r.json()).then(setHealth).catch(() => setHealth(null));
  }, [me.active_tenant_id]);

  async function switchTenant(tenantId) {
    setError(null);
    try {
      await api("POST", "/api/session/tenant", { tenant_id: tenantId });
      onChanged();
    } catch {
      setError("The company could not be switched. Try again; if it keeps failing, sign out and back in.");
    }
  }

  const active = tenants.find((t) => t.tenant_id === me.active_tenant_id);
  const canImport = Boolean(active) && IMPORT_ROLES.includes(me.role);
  const canConfig = Boolean(active) && CONFIG_ROLES.includes(me.role);
  const canConnections = Boolean(active) && CONNECTION_ROLES.includes(me.role);

  return (
    <div>
      <header className="header">
        <strong>{PRODUCT_NAME}</strong>
        <label className="small">
          Company{" "}
          <select
            value={me.active_tenant_id || ""}
            onChange={(e) => switchTenant(e.target.value)}
            disabled={tenants.length === 0}
          >
            {!me.active_tenant_id && <option value="">Choose…</option>}
            {tenants.map((t) => (
              <option key={t.tenant_id} value={t.tenant_id}>
                {t.name} ({t.role})
              </option>
            ))}
          </select>
        </label>
        {(canImport || canConfig || canConnections) && (
          <nav className="header-nav">
            <button type="button" className="link-button" onClick={() => go("home")}>
              Home
            </button>
            {canImport && (
              <button type="button" className="link-button" onClick={() => go("imports")}>
                Imports
              </button>
            )}
            {canConfig && (
              <button type="button" className="link-button" onClick={() => go("config")}>
                Configuration
              </button>
            )}
            {canConnections && (
              <button type="button" className="link-button" onClick={() => go("connections")}>
                Connections
              </button>
            )}
          </nav>
        )}
        <span className="header-user">
          {me.user.email}
          {me.firm_role ? ` · ${me.firm_role}` : ""}
        </span>
        <button type="button" className="button" onClick={onLogout}>Sign out</button>
      </header>
      <main className="main">
        {error && <p className="error">{error}</p>}
        {active && view === "imports" && canImport ? (
          <Imports me={me} />
        ) : active && view === "config" && canConfig ? (
          <Config me={me} />
        ) : active && view === "connections" && canConnections ? (
          <Connections me={me} result={returned ? returned.result : ""} />
        ) : active ? (
          <>
            <h1>{active.name}</h1>
            <p>
              Your role here: <strong>{active.role}</strong>. Job cost &amp; WIP reporting arrives with the next features.
            </p>
          </>
        ) : (
          <p>Choose a company to start.</p>
        )}
        {health && (
          <p className="hint">
            API {health.status} · database {health.db}
          </p>
        )}
      </main>
    </div>
  );
}
