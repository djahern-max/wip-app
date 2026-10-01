import { useEffect, useState } from "react";
import { api } from "../api.js";
import { amountOr, qboCell } from "../jobs.js";
import Attention from "./JobAttention.jsx";
import JobDetail from "./JobDetail.jsx";
import JobReview from "./JobReview.jsx";

// Jobs (F07): every job with its computed contract figures, the review queue for sold
// estimates, and one job's detail. The job is the unit everything after F07 reports on.
// Matching is by id: names only ever produce labelled suggestions, and a person makes
// every attach and link. Read by every role; the actions are for the roles that upload
// (canManage). Mount effects only read (GET).
//
// Money arrives as strings with cents and is rendered by formatMoney (through
// amountOr); a figure the API leaves out is said in words, never a dash. Tables use
// the container-scroll pattern (.table-wrap, first column held) so the page never
// scrolls sideways on a phone.

export default function Jobs({ me, canManage, target, onOpenEstimate }) {
  const [view, setView] = useState("list");
  const [jobId, setJobId] = useState(null);
  const [reviewStart, setReviewStart] = useState(null); // F07.1: {estimateId, externalId}
  const [data, setData] = useState(null);
  const [filters, setFilters] = useState({ status: "", division_id: "", revenue_method: "", no_link: false });
  const [pool, setPool] = useState({ kind: "pool", name: "", division_id: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [reload, setReload] = useState(0);

  // Another screen (the estimate detail) may open a job or the queue.
  useEffect(() => {
    if (!target) return;
    if (target.view === "detail" && target.jobId) {
      setJobId(target.jobId);
      setView("detail");
    } else if (target.view === "review") {
      setReviewStart(target.estimateId ? { estimateId: target.estimateId, externalId: target.externalId } : null);
      setView("review");
    }
  }, [target]);

  useEffect(() => {
    setView("list"); // another company: its own jobs
    setJobId(null);
  }, [me.active_tenant_id]);

  useEffect(() => {
    if (view !== "list") return;
    const q = new URLSearchParams();
    if (filters.status) q.set("status", filters.status);
    if (filters.division_id) q.set("division_id", filters.division_id);
    if (filters.revenue_method) q.set("revenue_method", filters.revenue_method);
    if (filters.no_link) q.set("no_link", "true");
    const qs = q.toString();
    api("GET", `/api/jobs${qs ? `?${qs}` : ""}`)
      .then(setData)
      .catch(() => setError("The jobs could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, view, filters, reload]);

  function openJob(id) {
    setJobId(id);
    setView("detail");
  }

  function backToList() {
    setView("list");
    setReload((n) => n + 1);
  }

  async function createPool(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const path = pool.kind === "program" ? "/api/jobs/program" : "/api/jobs/pool";
      const job = await api("POST", path, { name: pool.name, division_id: pool.division_id || null });
      setPool({ kind: pool.kind, name: "", division_id: "" });
      openJob(job.id);
    } catch (err) {
      setError(err.detail || "The job could not be created. Try again.");
    } finally {
      setBusy(false);
    }
  }

  if (view === "review") {
    return (
      <JobReview
        me={me}
        canManage={canManage}
        onBack={backToList}
        onOpenJob={openJob}
        startEstimateId={reviewStart ? reviewStart.estimateId : null}
        startExternalId={reviewStart ? reviewStart.externalId : null}
      />
    );
  }
  if (view === "detail" && jobId) {
    return (
      <JobDetail me={me} jobId={jobId} canManage={canManage} onBack={backToList} onOpenEstimate={onOpenEstimate} />
    );
  }

  return (
    <div>
      <h2>Jobs</h2>
      {error && <p className="error">{error}</p>}
      <div className="toolbar">
        <button
          type="button"
          className="button-primary"
          onClick={() => {
            setReviewStart(null);
            setView("review");
          }}
          disabled={!data}
        >
          Review sold estimates ({data ? data.to_review : "…"})
        </button>
      </div>
      {data && (
        <div className="toolbar">
          <label>
            Status{" "}
            <select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}>
              <option value="">All statuses</option>
              {data.statuses.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Division{" "}
            <select value={filters.division_id} onChange={(e) => setFilters({ ...filters, division_id: e.target.value })}>
              <option value="">All divisions</option>
              {data.divisions.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.code}
                </option>
              ))}
            </select>
          </label>
          <label>
            Revenue method{" "}
            <select
              value={filters.revenue_method}
              onChange={(e) => setFilters({ ...filters, revenue_method: e.target.value })}
            >
              <option value="">All revenue methods</option>
              {data.revenue_methods.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={filters.no_link}
              onChange={(e) => setFilters({ ...filters, no_link: e.target.checked })}
            />{" "}
            No QuickBooks link
          </label>
        </div>
      )}
      <p className="hint">
        A QuickBooks project is created when the first money moves on a job (D-35). A Sold job with no link is
        backlog; from In progress on, a job needs its link.
      </p>
      {!data ? (
        <p className="hint">Loading…</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Job</th>
                <th>Customer</th>
                <th>Division</th>
                <th>Revenue method</th>
                <th>Status</th>
                <th className="num">Revised contract</th>
                <th className="num">Unapproved change orders</th>
                <th className="num">EAC in the WIP basis</th>
                <th>QuickBooks</th>
                <th>Attention</th>
              </tr>
            </thead>
            <tbody>
              {data.jobs.length === 0 && (
                <tr>
                  <td colSpan={10}>
                    No jobs yet. Review the sold estimates to make them jobs.
                  </td>
                </tr>
              )}
              {data.jobs.map((j) => (
                <tr key={j.id}>
                  <td>
                    <button type="button" className="link-button" onClick={() => openJob(j.id)}>
                      {j.name}
                    </button>
                  </td>
                  <td>{j.customer_name || "Not linked"}</td>
                  <td>{j.division_code || "Not set"}</td>
                  <td>{j.revenue_method_label}</td>
                  <td>{j.status_label}</td>
                  <td className="num">
                    {amountOr(j.revised_contract, "None")}
                    {j.revised_contract_note && <div className="hint">{j.revised_contract_note}</div>}
                  </td>
                  <td className="num">{amountOr(j.unapproved_change_orders, "None")}</td>
                  <td className="num">{amountOr(j.eac_in_basis, "Not computed")}</td>
                  <td>{qboCell(j)}</td>
                  <td>
                    <Attention items={j.attention} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && data.ledger_items.length > 0 && (
        <>
          <h3>QuickBooks projects with no job</h3>
          <ul className="issues">
            {data.ledger_items.map((i, n) => (
              <li key={n}>{i.message}</li>
            ))}
          </ul>
        </>
      )}

      {canManage && data && (
        <>
          <h3>New pool or program job</h3>
          <p className="hint">
            Made by hand, with no estimate and no contract; never on the WIP schedule. A pool holds shared supplies
            until month-end allocation (D-30). A program is one maintenance or snow season, recognised as billed
            (D-35).
          </p>
          <form onSubmit={createPool} className="inline-form">
            <label className="label">
              Kind
              <select
                className="input"
                value={pool.kind}
                onChange={(e) => setPool({ ...pool, kind: e.target.value })}
                disabled={busy}
              >
                <option value="pool">Pool (shared supplies)</option>
                <option value="program">Program (maintenance or snow season)</option>
              </select>
            </label>
            <label className="label">
              Name
              <input
                className="input"
                value={pool.name}
                onChange={(e) => setPool({ ...pool, name: e.target.value })}
                placeholder={pool.kind === "program" ? "Snow 2026-27" : "Pool - Hydroseed"}
                required
                disabled={busy}
              />
            </label>
            <label className="label">
              Division
              <select
                className="input"
                value={pool.division_id}
                onChange={(e) => setPool({ ...pool, division_id: e.target.value })}
                required
                disabled={busy}
              >
                <option value="">Choose a division</option>
                {data.divisions.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.code}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" className="button" disabled={busy || !pool.name.trim() || !pool.division_id}>
              {pool.kind === "program" ? "Create program job" : "Create pool job"}
            </button>
          </form>
        </>
      )}
    </div>
  );
}

