import { useEffect, useState } from "react";
import { api } from "../api.js";
import { PRODUCT_NAME, SUPPORT_EMAIL } from "../product.js";
import { openWords, pickerWords } from "../exceptions.js";
import { roleWords } from "../roles.js";
import Logo from "../Logo.jsx";
import Config from "./Config.jsx";
import Connections from "./Connections.jsx";
import Customers from "./Customers.jsx";
import Estimates from "./Estimates.jsx";
import Exceptions from "./Exceptions.jsx";
import Home from "./Home.jsx";
import Imports from "./Imports.jsx";
import Jobs from "./Jobs.jsx";

// Roles with can_manage_imports and can_view_tenant_config (app/core/authz.py). The
// server enforces them; this only decides whether to show the links. Estimates (F06)
// are read by every role, so that link needs only an active company.
const IMPORT_ROLES = ["firm_admin", "firm_staff", "client_admin"];
const CONFIG_ROLES = ["firm_admin", "firm_staff", "client_admin"];
const CONNECTION_ROLES = ["firm_admin", "firm_staff", "client_admin"]; // can_view_connections
// F07: every role reads jobs; can_manage_jobs and can_view_customer_duplicates are these.
const JOB_MANAGER_ROLES = ["firm_admin", "firm_staff", "client_admin"];

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
  // F07: what another screen asked to open (a job, the review queue, an estimate).
  const [jobTarget, setJobTarget] = useState(null);
  const [estimateTarget, setEstimateTarget] = useState(null);
  const [configTarget, setConfigTarget] = useState(null); // F07.3: the Configuration section Home asked for
  const [menuOpen, setMenuOpen] = useState(false); // F09.1: phone width only; the nav is always shown on a laptop

  // Leaving a screen forgets the message QuickBooks came back with.
  function go(next) {
    setReturned(null);
    setJobTarget(null);
    setEstimateTarget(null);
    setConfigTarget(null);
    setMenuOpen(false);
    setView(next);
  }

  // F07.3: a Home line sends the person to the page (and section, job or queue) that
  // completes it. The API only sends links the role can open.
  function openLink(link) {
    if (!link) return;
    if (link.page === "config") {
      go("config");
      setConfigTarget(link.section || "accounts");
    } else if (link.page === "jobs" && link.review) {
      openReview(null, null);
    } else if (link.page === "jobs" && link.job_id) {
      openJob(link.job_id);
    } else {
      go(link.page);
    }
  }

  function openJob(jobId) {
    go("jobs");
    setJobTarget({ view: "detail", jobId });
  }

  function openReview(estimateId, externalId) {
    // F07.1: the queue opens on the estimate the person was looking at.
    go("jobs");
    setJobTarget({ view: "review", estimateId: estimateId || null, externalId: externalId || null });
  }

  function openEstimate(estimateId) {
    go("estimates");
    setEstimateTarget(estimateId);
  }

  useEffect(() => {
    api("GET", "/api/session/tenants")
      .then(setTenants)
      .catch(() => setError("The list of companies could not be loaded. Reload the page."));
    fetch("/api/health").then((r) => r.json()).then(setHealth).catch(() => setHealth(null));
  }, [me.active_tenant_id, view]); // F09: the open count follows the person from page to page

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
  const canManageJobs = Boolean(active) && JOB_MANAGER_ROLES.includes(me.role);

  return (
    <div>
      <header className="header">
        <span className="header-brand">
          <Logo size={24} />
          <strong>{PRODUCT_NAME}</strong>
        </span>
        <label className="header-company">
          <span className="header-company-label">Company</span>
          <select
            className="header-select"
            value={me.active_tenant_id || ""}
            onChange={(e) => switchTenant(e.target.value)}
            disabled={tenants.length === 0}
          >
            {!me.active_tenant_id && <option value="">Choose…</option>}
            {tenants.map((t) => (
              <option key={t.tenant_id} value={t.tenant_id}>
                {t.name}
                {me.firm_role && t.open_exceptions ? ` · ${pickerWords(t.open_exceptions)}` : ""}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="button menu-button"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen(!menuOpen)}
        >
          Menu
        </button>
        {active && (
          <nav className={menuOpen ? "header-nav header-open" : "header-nav"} aria-label="Main">
            <button
              type="button"
              className="nav-link"
              aria-current={view === "home" ? "page" : undefined}
              onClick={() => go("home")}
            >
              Home
            </button>
            <button
              type="button"
              className="nav-link"
              aria-current={view === "estimates" ? "page" : undefined}
              onClick={() => go("estimates")}
            >
              Estimates
            </button>
            <button
              type="button"
              className="nav-link"
              aria-current={view === "jobs" ? "page" : undefined}
              onClick={() => go("jobs")}
            >
              Jobs
            </button>
            <button
              type="button"
              className="nav-link"
              aria-current={view === "exceptions" ? "page" : undefined}
              onClick={() => go("exceptions")}
            >
              Exceptions{active.open_exceptions ? ` (${openWords(active.open_exceptions)})` : ""}
            </button>
            {canManageJobs && (
              <button
                type="button"
                className="nav-link"
                aria-current={view === "customers" ? "page" : undefined}
                onClick={() => go("customers")}
              >
                Customers
              </button>
            )}
            {canImport && (
              <button
                type="button"
                className="nav-link"
                aria-current={view === "imports" ? "page" : undefined}
                onClick={() => go("imports")}
              >
                Imports
              </button>
            )}
            {canConfig && (
              <button
                type="button"
                className="nav-link"
                aria-current={view === "config" ? "page" : undefined}
                onClick={() => go("config")}
              >
                Configuration
              </button>
            )}
            {canConnections && (
              <button
                type="button"
                className="nav-link"
                aria-current={view === "connections" ? "page" : undefined}
                onClick={() => go("connections")}
              >
                Connections
              </button>
            )}
          </nav>
        )}
        <div className={menuOpen ? "header-account header-open" : "header-account"}>
          <span className="header-user">
            <span>{me.user.email}</span>
            <span className="header-role">{roleWords(active ? active.role : me.firm_role)}</span>
          </span>
          <button type="button" className="button" onClick={onLogout}>Sign out</button>
        </div>
      </header>
      <main className="main">
        {error && <p className="error">{error}</p>}
        {active && view === "estimates" ? (
          <Estimates
            me={me}
            canUpload={canImport}
            onUpload={() => go("imports")}
            target={estimateTarget}
            onOpenJob={openJob}
            onReview={openReview}
          />
        ) : active && view === "jobs" ? (
          <Jobs me={me} canManage={canManageJobs} target={jobTarget} onOpenEstimate={openEstimate} />
        ) : active && view === "exceptions" ? (
          <Exceptions me={me} onOpenJob={openJob} onOpenEstimate={openEstimate} onOpenCustomers={() => go("customers")} />
        ) : active && view === "customers" && canManageJobs ? (
          <Customers me={me} onOpenJob={openJob} />
        ) : active && view === "imports" && canImport ? (
          <Imports me={me} />
        ) : active && view === "config" && canConfig ? (
          <Config me={me} target={configTarget} />
        ) : active && view === "connections" && canConnections ? (
          <Connections me={me} result={returned ? returned.result : ""} />
        ) : active ? (
          <Home me={me} companyName={active.name} onOpen={openLink} />
        ) : (
          <p>Choose a company to start.</p>
        )}
      </main>
      <footer className="footer">
        <span>
          Support: <a href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</a>
        </span>
        {health && (
          <span className="muted">
            API {health.status} · database {health.db}
          </span>
        )}
      </footer>
    </div>
  );
}
