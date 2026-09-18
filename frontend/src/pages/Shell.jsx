import { useEffect, useState } from "react";
import { api } from "../api.js";
import { PRODUCT_NAME } from "../product.js";
import Imports from "./Imports.jsx";

// Roles with can_manage_imports (app/core/authz.py). The server enforces it; this
// only decides whether to show the link.
const IMPORT_ROLES = ["firm_admin", "firm_staff", "client_admin"];

// Signed-in frame: product name, tenant switcher, user, sign-out. The active
// tenant lives in the server-side session; the switcher only asks to change it.
export default function Shell({ me, onChanged, onLogout }) {
  const [tenants, setTenants] = useState([]);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  const [view, setView] = useState("home");

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
        {canImport && (
          <nav className="header-nav">
            <button type="button" className="link-button" onClick={() => setView("home")}>
              Home
            </button>
            <button type="button" className="link-button" onClick={() => setView("imports")}>
              Imports
            </button>
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
