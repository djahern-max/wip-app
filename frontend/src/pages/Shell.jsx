import { useEffect, useState } from "react";
import { api } from "../api.js";
import { PRODUCT_NAME } from "../product.js";
import Imports from "./Imports.jsx";
import { styles } from "./Login.jsx";

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
    api("GET", "/api/session/tenants").then(setTenants).catch((e) => setError(String(e.message)));
    fetch("/api/health").then((r) => r.json()).then(setHealth).catch(() => setHealth(null));
  }, [me.active_tenant_id]);

  async function switchTenant(tenantId) {
    setError(null);
    try {
      await api("POST", "/api/session/tenant", { tenant_id: tenantId });
      onChanged();
    } catch (e) {
      setError(e.detail || "Could not switch tenant.");
    }
  }

  const active = tenants.find((t) => t.tenant_id === me.active_tenant_id);
  const canImport = Boolean(active) && IMPORT_ROLES.includes(me.role);

  return (
    <div style={{ fontFamily: "system-ui, sans-serif" }}>
      <header style={header}>
        <strong>{PRODUCT_NAME}</strong>
        <label style={{ fontSize: 14 }}>
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
          <nav style={{ display: "flex", gap: "1rem", fontSize: 14 }}>
            <button type="button" style={styles.linkButton} onClick={() => setView("home")}>
              Home
            </button>
            <button type="button" style={styles.linkButton} onClick={() => setView("imports")}>
              Imports
            </button>
          </nav>
        )}
        <span style={{ marginLeft: "auto", fontSize: 14 }}>
          {me.user.email}
          {me.firm_role ? ` · ${me.firm_role}` : ""}
        </span>
        <button type="button" style={styles.button} onClick={onLogout}>Sign out</button>
      </header>
      <main style={{ padding: "1rem", maxWidth: "100%", boxSizing: "border-box" }}>
        {error && <p style={styles.error}>{error}</p>}
        {active && view === "imports" && canImport ? (
          <Imports me={me} />
        ) : active ? (
          <>
            <h1 style={{ marginTop: 0 }}>{active.name}</h1>
            <p>
              Your role here: <strong>{active.role}</strong>. Job cost &amp; WIP reporting arrives with the next features.
            </p>
          </>
        ) : (
          <p>Choose a company to start.</p>
        )}
        {health && (
          <p style={styles.hint}>
            API {health.status} · database {health.db}
          </p>
        )}
      </main>
    </div>
  );
}

const header = {
  display: "flex",
  flexWrap: "wrap", // phone width: the switcher, nav and user wrap instead of overflowing
  alignItems: "center",
  gap: "0.75rem 1.5rem",
  padding: "0.75rem 1rem",
  borderBottom: "1px solid #ddd",
  background: "#fafafa",
};
