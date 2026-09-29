import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";
import { amountOr, reasonText } from "../jobs.js";
import Attention from "./JobAttention.jsx";

// Job detail (F07): the job's header (edited in place by the roles that manage jobs),
// its estimates with their roles, the work areas of its original estimate with their
// kind (suggested until a person confirms it, D-01), the computed contract figures, and
// its QuickBooks links. Suggestions for a link say why; the search lists any active,
// unlinked QuickBooks row by name, and a link is always made by the row's id. Every
// action writes one audit row. Mount effects only read (GET).

export default function JobDetail({ me, jobId, canManage, onBack, onOpenEstimate }) {
  const [job, setJob] = useState(null);
  const [edit, setEdit] = useState(null);
  const [suggestions, setSuggestions] = useState(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

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
    for (const key of ["name", "division_id", "revenue_method", "status", "notes"]) {
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

  async function link(externalId) {
    const ok = await run(() =>
      api("POST", `/api/jobs/${jobId}/aliases`, { system: "qbo_customer", external_id: externalId }),
    );
    if (ok) setResults(null);
  }

  if (!job) {
    return (
      <div>
        <p>
          <button type="button" className="link-button" onClick={onBack}>
            ← All jobs
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
      edit.notes !== (job.notes || ""));

  return (
    <div>
      <p>
        <button type="button" className="link-button" onClick={onBack}>
          ← All jobs
        </button>
      </p>
      <h2>{job.name}</h2>
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
          <button type="submit" className="button-primary" disabled={busy || !changed || !edit.name.trim()}>
            Save changes
          </button>
        </form>
      )}

      <h3>Contract</h3>
      <dl className="kv">
        <dt>Revised contract</dt>
        <dd className="num">
          {amountOr(job.revised_contract, "None")}
          {job.revised_contract_note && <div className="hint">{job.revised_contract_note}</div>}
        </dd>
        <dt>Unapproved change orders</dt>
        <dd className="num">{amountOr(job.unapproved_change_orders, "None")}</dd>
        <dt>EAC in the WIP basis</dt>
        <dd className="num">
          {amountOr(job.eac_in_basis, "Not computed")}
          {job.eac_note && <div className="hint">{job.eac_note}</div>}
        </dd>
      </dl>
      <p className="hint">
        Revised contract counts the kept work areas of the original estimate confirmed as original. Change orders are
        shown unapproved, outside the contract, until they are signed off (D-01).
      </p>

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
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Order</th>
                    <th>Name</th>
                    <th>Kept</th>
                    <th className="num">Price</th>
                    <th>Kind</th>
                    {canManage && <th>Confirm as</th>}
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
                          {w.kept &&
                            ["original", "change_order"]
                              .filter((k) => k !== w.kind)
                              .map((k) => (
                                <button
                                  key={k}
                                  type="button"
                                  className="link-button"
                                  disabled={busy}
                                  onClick={() =>
                                    run(() => api("POST", `/api/jobs/${jobId}/work-areas/${w.id}/kind`, { kind: k }))
                                  }
                                >
                                  {k === "original" ? "Original" : "Change order"}{" "}
                                </button>
                              ))}
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <h3>QuickBooks</h3>
      {qboLinks.length === 0 ? (
        <p>Not linked.</p>
      ) : (
        <ul>
          {qboLinks.map((a) => (
            <li key={a.id}>
              {a.display_name || a.external_id} ({a.kind_label || "QuickBooks"}, id {a.external_id})
              {a.linked_by ? `, linked by ${a.linked_by}` : ""}{" "}
              {canManage && (
                <button
                  type="button"
                  className="link-button"
                  disabled={busy}
                  onClick={() => run(() => api("DELETE", `/api/jobs/${jobId}/aliases/${a.id}`))}
                >
                  Unlink
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <h4>Suggested QuickBooks rows</h4>
      {!suggestions ? (
        <p className="hint">Loading…</p>
      ) : suggestions.length === 0 ? (
        <p className="hint">Nothing is suggested. Search for the project below.</p>
      ) : (
        <QboRows rows={suggestions} canManage={canManage} busy={busy} onLink={link} />
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
            <QboRows rows={results} canManage={canManage} busy={busy} onLink={link} />
          ))}
        </>
      )}

      <h3>Attention</h3>
      {job.attention.length === 0 ? (
        <p className="hint">Nothing needs attention on this job.</p>
      ) : (
        <Attention items={job.attention} />
      )}
    </div>
  );
}

function QboRows({ rows, canManage, busy, onLink }) {
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
                    Link
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
