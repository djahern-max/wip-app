import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";
import {
  amountOr,
  figureOr,
  lineAction,
  offersInProgress,
  pendingLabel,
  pickedWorkArea,
  reasonText,
  suggestedPairs,
} from "../jobs.js";
import { KindActions } from "../kindActions.js";

// The firm's set-up view of a job (F09.6, D-50): the blocks lifted out of JobDetail.jsx as
// they were, for firm roles only (Jobs.jsx draws this view for no other role; the API's
// permissions are unchanged). The header fields and their edit form (F07, F07.1), the
// estimates with Detach (F07), the work areas of the original estimate with their kind
// confirm controls and "Confirm all as suggested" (F07.1, D-01), the invoice lines with
// their assignment (F08.1, D-45), and the QuickBooks links, suggestions and search (F07,
// D-35). The same components, requests, audit rows and permission checks; nothing here is
// designed yet (F09.7 does that). Mount effects only read (GET).

export default function JobSetup({ me, jobId, canManage, onBack, onOpenEstimate }) {
  const [job, setJob] = useState(null);
  const [edit, setEdit] = useState(null);
  const [suggestions, setSuggestions] = useState(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null); // F08.2: {id, action} of the pressed Link or Unlink
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null); // F07.1: what "Confirm all as suggested" did
  const [setInProgress, setSetInProgress] = useState(true); // D-35: offered for a sold job
  const [picks, setPicks] = useState({}); // F08.1: billing line id → the work area picked in its row
  const [lineNotice, setLineNotice] = useState(null); // what "Confirm all as suggested" did on the lines

  useEffect(() => {
    api("GET", `/api/jobs/${jobId}`)
      .then(show)
      .catch(() => setError("The job could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, jobId]);

  useEffect(() => {
    if (!job) return;
    api("GET", `/api/jobs/${jobId}/qbo-candidates`)
      .then((d) => setSuggestions(d.rows))
      .catch(() => setSuggestions([]));
  }, [me.active_tenant_id, jobId, job && job.aliases.length]);

  function show(j) {
    setJob(j);
    setEdit({
      name: j.name,
      division_id: j.division_id || "",
      revenue_method: j.revenue_method,
      status: j.status,
      notes: j.notes || "",
      sold_on: j.sold_on,
    });
  }

  async function run(fn) {
    setBusy(true);
    setError(null);
    try {
      show(await fn());
      return true;
    } catch (err) {
      setError(err.detail || "The change could not be saved. Try again.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  function save(e) {
    e.preventDefault();
    const changes = {};
    for (const key of ["name", "division_id", "revenue_method", "status", "notes", "sold_on"]) {
      const before = key === "division_id" ? job.division_id || "" : key === "notes" ? job.notes || "" : job[key];
      if (edit[key] !== before) changes[key] = edit[key] === "" && key === "division_id" ? null : edit[key];
    }
    run(() => api("PATCH", `/api/jobs/${jobId}`, changes));
  }

  async function search(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const d = await api("GET", `/api/jobs/${jobId}/qbo-search?q=${encodeURIComponent(query)}`);
      setResults(d.rows);
    } catch {
      setError("The QuickBooks search could not be run. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function confirmAll() {
    setNotice(null);
    const ok = await run(async () => {
      const r = await api("POST", `/api/jobs/${jobId}/work-areas/kinds/confirm-suggested`);
      setNotice(r.message);
      return r;
    });
    if (!ok) setNotice(null);
  }

  async function link(externalId) {
    setPending({ id: externalId, action: "link" });
    try {
      const ok = await run(() =>
        api("POST", `/api/jobs/${jobId}/aliases`, {
          system: "qbo_customer",
          external_id: externalId,
          set_in_progress: offersInProgress(job) && setInProgress,
        }),
      );
      if (ok) setResults(null);
    } finally {
      setPending(null);
    }
  }

  async function unlink(aliasId) {
    setPending({ id: aliasId, action: "unlink" });
    try {
      await run(() => api("DELETE", `/api/jobs/${jobId}/aliases/${aliasId}`));
    } finally {
      setPending(null);
    }
  }

  async function assignLine(ln) {
    const areaId = pickedWorkArea(ln, picks);
    if (!areaId) return;
    setPending({ id: ln.billing_line_id, action: "assign" });
    try {
      await run(() =>
        api("PUT", `/api/jobs/${jobId}/invoice-lines/${ln.billing_line_id}/work-area`, { estimate_work_area_id: areaId }),
      );
    } finally {
      setPending(null);
    }
  }

  async function clearLine(ln) {
    setPending({ id: ln.billing_line_id, action: "clear" });
    try {
      await run(() => api("DELETE", `/api/jobs/${jobId}/invoice-lines/${ln.billing_line_id}/work-area`));
    } finally {
      setPending(null);
    }
  }

  async function assignSuggested() {
    const pairs = suggestedPairs(job.invoice_lines.lines);
    setPending({ id: "all", action: "assign_all" });
    setLineNotice(null);
    try {
      await run(async () => {
        const d = await api("POST", `/api/jobs/${jobId}/invoice-lines/assign-suggested`, { assignments: pairs });
        setLineNotice(d.message);
        return d;
      });
    } finally {
      setPending(null);
    }
  }

  function workAreaCell(ln) {
    if (ln.work_area_label) return ln.work_area_label;
    if (!ln.offered || !canManage) return ln.suggested_label ? `${ln.suggested_label} (suggested)` : "";
    const doc = ln.doc_number || `QuickBooks id ${ln.external_id}`;
    return (
      <select
        className="input"
        aria-label={`Work area for line ${ln.line_no} of ${doc}`}
        value={pickedWorkArea(ln, picks)}
        disabled={busy}
        onChange={(e) => setPicks({ ...picks, [ln.billing_line_id]: e.target.value })}
      >
        <option value="">Choose a work area</option>
        {job.invoice_lines.work_areas.map((w) => (
          <option key={w.id} value={w.id}>
            {w.label}
            {w.id === ln.suggested_work_area_id ? " (suggested)" : ""}
          </option>
        ))}
      </select>
    );
  }

  function lineActionCell(ln) {
    const a = lineAction(ln);
    if (!a) return null;
    const ready = a.action === "clear" || Boolean(pickedWorkArea(ln, picks));
    return (
      <button
        type="button"
        className="link-button"
        disabled={busy || !ready}
        onClick={() => (a.action === "clear" ? clearLine(ln) : assignLine(ln))}
      >
        {pendingLabel(a.action, pending, ln.billing_line_id) || a.label}
      </button>
    );
  }

  if (!job) {
    return (
      <div>
        <p>
          <button type="button" className="link-button" onClick={onBack}>
            ← Back to the job
          </button>
        </p>
        <p className="hint">{error || "Loading…"}</p>
      </div>
    );
  }

  const qboLinks = job.aliases.filter((a) => a.system === "qbo_customer");
  const changed =
    edit &&
    (edit.name !== job.name ||
      edit.division_id !== (job.division_id || "") ||
      edit.revenue_method !== job.revenue_method ||
      edit.status !== job.status ||
      edit.notes !== (job.notes || "") ||
      edit.sold_on !== job.sold_on);

  return (
    <div>
      <p>
        <button type="button" className="link-button" onClick={onBack}>
          ← Back to the job
        </button>
      </p>
      <h2>Set-up: {job.name}</h2>
      {error && <p className="error">{error}</p>}
      <dl className="kv">
        <dt>Customer</dt>
        <dd>{job.customer_name || "Not linked to QuickBooks"}</dd>
        <dt>Division</dt>
        <dd>{job.division_code || "Not set"}</dd>
        <dt>Revenue method</dt>
        <dd>{job.revenue_method_label}</dd>
        <dt>Status</dt>
        <dd>{job.status_label}</dd>
        <dt>Sold on</dt>
        <dd>
          {job.sold_on}
          {job.sold_on_set_when_created ? " (set when created)" : ""}
        </dd>
        <dt>Created</dt>
        <dd>
          {new Date(job.created_at).toLocaleString()}
          {job.created_by ? ` by ${job.created_by}` : ""}
        </dd>
      </dl>

      {canManage && edit && (
        <form onSubmit={save} className="inline-form">
          <label className="label">
            Job name
            <input className="input" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} required disabled={busy} />
          </label>
          <label className="label">
            Division
            <select className="input" value={edit.division_id} onChange={(e) => setEdit({ ...edit, division_id: e.target.value })} disabled={busy}>
              {!job.division_id && <option value="">Choose a division</option>}
              {job.divisions.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.code}
                </option>
              ))}
            </select>
          </label>
          <label className="label">
            Revenue method
            <select className="input" value={edit.revenue_method} onChange={(e) => setEdit({ ...edit, revenue_method: e.target.value })} disabled={busy}>
              {job.revenue_methods.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          <label className="label">
            Status
            <select className="input" value={edit.status} onChange={(e) => setEdit({ ...edit, status: e.target.value })} disabled={busy}>
              {job.statuses.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label className="label">
            Notes
            <input className="input" value={edit.notes} onChange={(e) => setEdit({ ...edit, notes: e.target.value })} disabled={busy} />
          </label>
          <label className="label">
            Sold on
            <input
              className="input"
              type="date"
              value={edit.sold_on}
              onChange={(e) => setEdit({ ...edit, sold_on: e.target.value })}
              required
              disabled={busy}
            />
          </label>
          <button type="submit" className="button-primary" disabled={busy || !changed || !edit.name.trim() || !edit.sold_on}>
            Save changes
          </button>
        </form>
      )}

      <h3>Estimates</h3>
      {job.estimates.length === 0 ? (
        <p className="hint">No estimate on this job{job.revenue_method === "pool" ? ": a pool has none (D-30)" : ""}.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Estimate ID</th>
                <th>Role</th>
                <th>Name</th>
                <th>Status</th>
                <th className="num">Price</th>
                <th className="num">EAC in the WIP basis</th>
                <th>Attached</th>
                <th>Note</th>
                {canManage && <th></th>}
              </tr>
            </thead>
            <tbody>
              {job.estimates.map((e) => (
                <tr key={e.estimate_id}>
                  <td>
                    <button type="button" className="link-button" onClick={() => onOpenEstimate(e.estimate_id)}>
                      {e.external_id}
                    </button>
                  </td>
                  <td>{e.role_label}</td>
                  <td>{e.name}</td>
                  <td>{e.status_label}</td>
                  <td className="num">{formatMoney(e.price)}</td>
                  <td className="num">
                    {e.role === "ignored" ? "Not counted" : amountOr(e.eac_in_basis, "Not computed")}
                  </td>
                  <td>
                    {new Date(e.attached_at).toLocaleDateString()}
                    {e.attached_by ? ` by ${e.attached_by}` : ""}
                  </td>
                  <td>{e.note || ""}</td>
                  {canManage && (
                    <td>
                      <button
                        type="button"
                        className="link-button"
                        disabled={busy}
                        onClick={() => run(() => api("DELETE", `/api/jobs/${jobId}/estimates/${e.estimate_id}`))}
                      >
                        Detach
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {job.original_external_id && (
        <>
          <h3>Work areas of {job.original_external_id} (latest version)</h3>
          {!job.work_areas_loaded ? (
            <p className="hint">No work areas loaded for this estimate: the revised contract is its price.</p>
          ) : (
            <>
            {canManage && job.to_confirm > 0 && (
              <p>
                <button type="button" className="button" disabled={busy} onClick={confirmAll}>
                  Confirm all as suggested ({job.to_confirm})
                </button>
              </p>
            )}
            {notice && <p className="hint">{notice}</p>}
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Order</th>
                    <th>Name</th>
                    <th>Kept</th>
                    <th className="num">Price</th>
                    <th>Kind</th>
                    {canManage && <th>Action</th>}
                  </tr>
                </thead>
                <tbody>
                  {job.work_areas.map((w) => (
                    <tr key={w.id}>
                      <td>#{w.order_no}</td>
                      <td>{w.name}</td>
                      <td>{w.kept_label}</td>
                      <td className="num">{formatMoney(w.price)}</td>
                      <td>{w.kind_label}</td>
                      {canManage && (
                        <td>
                          <KindActions
                            area={w}
                            busy={busy}
                            onConfirm={(k) => run(() => api("POST", `/api/jobs/${jobId}/work-areas/${w.id}/kind`, { kind: k }))}
                          />
                        </td>
                      )}
                    </tr>
                  ))}
                  <tr className="totals">
                    <td colSpan={3}>Total of the kept work areas</td>
                    <td className="num">{formatMoney(job.work_area_totals.price)}</td>
                    <td colSpan={canManage ? 2 : 1}></td>
                  </tr>
                </tbody>
              </table>
            </div>
            </>
          )}
        </>
      )}

      <h3>Invoice lines</h3>
      <p className="hint">
        One row per line of the invoices, credit memos and sales receipts on the QuickBooks rows of this job, and
        the work area each line is billed on: by its pay application, by the number at the start of its description
        (#n), or as a person assigned it (D-45). A suggestion by name is shown and never applied until it is
        confirmed. Billed to date is unchanged by any of this.
      </p>
      <p>
        Not assigned to a work area:{" "}
        <span className="num">{figureOr(job.invoice_lines.not_assigned_to_work_area, null, "Not decided")}</span>
      </p>
      {job.invoice_lines.policy_note && <p className="hint">{job.invoice_lines.policy_note}</p>}
      {canManage && job.invoice_lines.suggested > 0 && (
        <p>
          <button type="button" className="button" disabled={busy} onClick={assignSuggested}>
            {pendingLabel("assign_all", pending, "all") || `Confirm all as suggested (${job.invoice_lines.suggested})`}
          </button>
        </p>
      )}
      {lineNotice && <p className="hint">{lineNotice}</p>}
      {job.invoice_lines.lines.length === 0 ? (
        <p className="hint">No invoice, credit memo or sales receipt line on the QuickBooks rows of this job.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Document</th>
                <th>Date</th>
                <th>Kind</th>
                <th className="num">Line</th>
                <th>Description</th>
                <th className="num">Quantity</th>
                <th className="num">Rate</th>
                <th className="num">Amount</th>
                <th>Work area</th>
                <th>How it is tied</th>
                {canManage && <th>Action</th>}
              </tr>
            </thead>
            <tbody>
              {job.invoice_lines.lines.map((ln) => (
                <tr key={ln.billing_line_id}>
                  <td>
                    {ln.doc_number || `QuickBooks id ${ln.external_id}`}
                    {ln.state_label ? ` (${ln.state_label})` : ""}
                  </td>
                  <td>
                    {ln.txn_date}
                    {ln.service_date && <div className="hint">Service date {ln.service_date}</div>}
                  </td>
                  <td>{ln.kind_label}</td>
                  <td className="num">{ln.line_no}</td>
                  <td>{ln.description || ""}</td>
                  <td className="num">{ln.quantity || ""}</td>
                  <td className="num">{ln.rate ? formatMoney(ln.rate) : ""}</td>
                  <td className="num">{formatMoney(ln.amount)}</td>
                  <td>{workAreaCell(ln)}</td>
                  <td>
                    {ln.how_label}
                    {ln.assigned_at && <div className="hint">{new Date(ln.assigned_at).toLocaleDateString()}</div>}
                    {ln.not_offered && <div className="hint">{ln.not_offered}</div>}
                    {ln.note && <div className="hint">{ln.note}</div>}
                  </td>
                  {canManage && <td>{lineActionCell(ln)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>QuickBooks</h3>
      {qboLinks.length === 0 ? (
        <p>
          {job.status === "sold"
            ? "Not linked yet. Sold with no money moved: backlog. Create the QuickBooks project when the first money moves (a deposit, the first invoice or the first cost) and link it here (D-35)."
            : "Not linked."}
        </p>
      ) : (
        <ul>
          {qboLinks.map((a) => (
            <li key={a.id}>
              {a.display_name || a.external_id} ({a.kind_label || "QuickBooks"}, id {a.external_id})
              {a.linked_by ? `, linked by ${a.linked_by}` : ""}{" "}
              {canManage && (
                <button type="button" className="link-button" disabled={busy} onClick={() => unlink(a.id)}>
                  {pendingLabel("unlink", pending, a.id) || "Unlink"}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {canManage && offersInProgress(job) && (
        <p>
          <label>
            <input
              type="checkbox"
              checked={setInProgress}
              onChange={(e) => setSetInProgress(e.target.checked)}
              disabled={busy}
            />{" "}
            When linking, set the job In progress: the first money has moved (D-35)
          </label>
        </p>
      )}
      <h4>Suggested QuickBooks rows</h4>
      {!suggestions ? (
        <p className="hint">Loading…</p>
      ) : suggestions.length === 0 ? (
        <p className="hint">Nothing is suggested. Search for the project below.</p>
      ) : (
        <QboRows rows={suggestions} canManage={canManage} busy={busy} pending={pending} onLink={link} />
      )}
      {canManage && (
        <>
          <form onSubmit={search} className="inline-form">
            <label className="label">
              Find a QuickBooks project or customer by name
              <input className="input" value={query} onChange={(e) => setQuery(e.target.value)} disabled={busy} />
            </label>
            <button type="submit" className="button" disabled={busy}>
              Search
            </button>
          </form>
          {results && (results.length === 0 ? (
            <p className="hint">No active, unlinked QuickBooks row has that in its name.</p>
          ) : (
            <QboRows rows={results} canManage={canManage} busy={busy} pending={pending} onLink={link} />
          ))}
        </>
      )}
    </div>
  );
}

function QboRows({ rows, canManage, busy, pending, onLink }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>QuickBooks name</th>
            <th>Kind</th>
            <th>Customer</th>
            <th>Why it is listed</th>
            {canManage && <th></th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.external_id}>
              <td>{r.display_name}</td>
              <td>{r.kind_label}</td>
              <td>{r.parent_name || ""}</td>
              <td>{r.reason ? reasonText(r.reason) : "Found by search"}</td>
              {canManage && (
                <td>
                  <button type="button" className="link-button" disabled={busy} onClick={() => onLink(r.external_id)}>
                    {pendingLabel("link", pending, r.external_id) || "Link"}
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
