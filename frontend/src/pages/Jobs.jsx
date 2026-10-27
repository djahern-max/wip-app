import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";
import { isFirmRole } from "../roles.js";
import {
  amountOr,
  boardSentence,
  daysWords,
  figureOr,
  filterQuery,
  jobsListedWords,
  qboCell,
  tieOutDone,
  tieOutText,
} from "../jobs.js";
import Attention from "./JobAttention.jsx";
import JobDetail from "./JobDetail.jsx";
import JobReview from "./JobReview.jsx";
import JobSetup from "./JobSetup.jsx";
import UnapprovedChangeOrders from "./UnapprovedChangeOrders.jsx";

// Jobs (F07): every job with its computed contract figures, the review queue for sold
// estimates, and one job's detail. The job is the unit everything after F07 reports on.
// F08: the list is the Sold Jobs Board (BLUEPRINT §9 report 1): the billing figures per
// job computed on read from QuickBooks (D-02, D-39), a totals row, the not-on-a-job row
// (D-35, D-37), the tie-out status, and the two exports (plain links to GETs).
// Matching is by id: names only ever produce labelled suggestions, and a person makes
// every attach and link. Read by every role; the actions are for the roles that upload
// (canManage). Mount effects only read (GET).
//
// Money arrives as strings with cents and is rendered by formatMoney (through
// amountOr); a figure the API leaves out is said in words, never a dash. Tables use
// the container-scroll pattern (.table-wrap, first column held) so the page never
// scrolls sideways on a phone.
// F09.5 (design; D-22, D-47, D-49): the page head with the one primary action, the
// tie-out line in the status colour when every month ties, labelled filters, the board
// in its box with the totals and not-on-a-job rows as rows and their explanation under
// the table, the no-job list as a panel, the pool form as one card. Layout, style and
// display words only; every figure, request and action is as before.
// F09.6 (D-50): the job page is the client's; its set-up blocks are a view of their own,
// "setup", drawn for firm roles only.

export default function Jobs({ me, canManage, target, onOpenEstimate }) {
  const [view, setView] = useState("list");
  const [jobId, setJobId] = useState(null);
  const [reviewStart, setReviewStart] = useState(null); // F07.1: {estimateId, externalId}
  const [data, setData] = useState(null);
  const [tie, setTie] = useState(null); // F08.2: the tie-out answers after the rows
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
    api("GET", `/api/jobs${filterQuery(filters)}`)
      .then(setData)
      .catch(() => setError("The jobs could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, view, filters, reload]);

  // The tie-out is over every job whatever the filter, and is its own request, so
  // the board's rows show first and the line reads "checking…" until it answers.
  useEffect(() => {
    if (view !== "list") return;
    setTie(null);
    api("GET", "/api/jobs/tie-out")
      .then(setTie)
      .catch(() => setTie(false));
  }, [me.active_tenant_id, view, reload]);

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
  if (view === "unapproved") {
    // F07.4 (D-42): every unapproved change order on an open job, with its amount and age.
    return <UnapprovedChangeOrders me={me} onBack={backToList} onOpenJob={openJob} />;
  }
  if (view === "setup" && jobId && isFirmRole(me.role)) {
    // F09.6 (D-50): the firm's set-up view of the job; the client's page never links here.
    return (
      <JobSetup
        me={me}
        jobId={jobId}
        canManage={canManage}
        onBack={() => setView("detail")}
        onOpenEstimate={onOpenEstimate}
      />
    );
  }
  if (view === "detail" && jobId) {
    return <JobDetail me={me} jobId={jobId} onBack={backToList} onOpenSetup={() => setView("setup")} />;
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Jobs</h1>
          {data && <p className="muted">{boardSentence(data)}</p>}
        </div>
        <div className="actions actions-row">
          <button type="button" className="link-button" onClick={() => setView("unapproved")} disabled={!data}>
            Unapproved change orders ({data ? data.unapproved_change_order_count : "…"})
          </button>
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
      </div>
      {error && <p className="error">{error}</p>}
      {data && (
        <p className="done-line tie-line">
          {tieOutDone(tie) && (
            <span className="step-marker step-marker-done" aria-hidden="true">
              ✓
            </span>
          )}
          <span className={tieOutDone(tie) ? "tie-words tie-words-done" : "tie-words"}>
            {tieOutText(tie)}
          </span>
        </p>
      )}
      {data && (
        <div className="filters">
          <label className="field">
            <span className="field-label">Status</span>
            <select
              className="select"
              value={filters.status}
              onChange={(e) => setFilters({ ...filters, status: e.target.value })}
            >
              <option value="">All statuses</option>
              {data.statuses.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Division</span>
            <select
              className="select"
              value={filters.division_id}
              onChange={(e) => setFilters({ ...filters, division_id: e.target.value })}
            >
              <option value="">All divisions</option>
              {data.divisions.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.code}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Revenue method</span>
            <select
              className="select"
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
          <label className="check">
            <input
              type="checkbox"
              checked={filters.no_link}
              onChange={(e) => setFilters({ ...filters, no_link: e.target.checked })}
            />{" "}
            No QuickBooks link
          </label>
          <p className="upload-note hint small">
            A QuickBooks project is created when the first money moves on a job. A Sold job with no link is
            backlog; from In progress on, a job needs its link.
          </p>
        </div>
      )}
      {!data ? (
        <p className="hint">Loading…</p>
      ) : (
        <>
          <div className="section-head section-head-wide">
            <div>
              <h2>Sold Jobs Board</h2>
              <p className="hint small">
                A deposit is the invoice numbered estimate_DEP on a deposit item; fuel surcharge lines are outside
                billed to date.
              </p>
              {data.policy_note && <p className="hint small">{data.policy_note}</p>}
            </div>
            <div className="actions actions-row">
              <a className="button" href={`/api/jobs/export.xlsx${filterQuery(filters)}`} download>
                Export XLSX
              </a>
              <a className="button" href={`/api/jobs/export.pdf${filterQuery(filters)}`} download>
                Export PDF
              </a>
            </div>
          </div>
          {data.jobs.length === 0 ? (
            <div className="empty">
              <strong>No jobs yet.</strong>
              <span className="muted">Review the sold estimates to make them jobs.</span>
            </div>
          ) : (
            <>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Job</th>
                      <th>Customer</th>
                      <th>Division</th>
                      <th>Revenue method</th>
                      <th>Status</th>
                      <th>Estimator</th>
                      <th className="num">Revised contract</th>
                      <th className="num">Unapproved change orders</th>
                      <th className="num">EAC in the WIP basis</th>
                      <th className="num">Deposit invoiced</th>
                      <th className="num">Deposit received</th>
                      <th className="num">Billed to date</th>
                      <th className="num">Fuel surcharge billed</th>
                      <th className="num">Collected to date</th>
                      <th className="num">Other credits applied</th>
                      <th className="num">Open A/R</th>
                      <th className="num">Remaining to bill</th>
                      <th className="num">Days since last activity</th>
                      <th>QuickBooks</th>
                      <th>Attention</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.jobs.map((j) => (
                      <tr key={j.id}>
                        <td>
                          <button type="button" className="link-button" onClick={() => openJob(j.id)}>
                            {j.name}
                          </button>
                          {j.estimate_number && <div className="cell-sub">{j.estimate_number}</div>}
                        </td>
                        <td>{j.customer_name || <span className="muted">Not linked</span>}</td>
                        <td>{j.division_code || <span className="muted">Not set</span>}</td>
                        <td>{j.revenue_method_label}</td>
                        <td>{j.status_label}</td>
                        <td>{j.estimator || <span className="muted">Not on the estimate</span>}</td>
                        <td className="num">
                          {amountOr(j.revised_contract, "None")}
                          {j.revised_contract_note && <div className="cell-sub">{j.revised_contract_note}</div>}
                        </td>
                        <td className="num">{amountOr(j.unapproved_change_orders, "None")}</td>
                        <td className="num">{amountOr(j.eac_in_basis, "Not computed")}</td>
                        <td className="num">{figureOr(j.billing.deposit_invoiced, null, "Not decided")}</td>
                        <td className="num">{figureOr(j.billing.deposit_received, null, "Not decided")}</td>
                        <td className="num">{figureOr(j.billing.billed_to_date, null, "Not decided")}</td>
                        <td className="num">{figureOr(j.billing.fuel_surcharge_billed, null, "Not decided")}</td>
                        <td className="num">{formatMoney(j.billing.collected_to_date)}</td>
                        <td className="num">{formatMoney(j.billing.other_credits_applied)}</td>
                        <td className="num">{formatMoney(j.billing.open_ar)}</td>
                        <td className="num">
                          {figureOr(j.billing.remaining_to_bill, j.billing.remaining_to_bill_note, "Not decided")}
                        </td>
                        <td className="num">{daysWords(j.billing.days_since_activity)}</td>
                        <td className="cell-text">{qboCell(j)}</td>
                        <td className="cell-text">
                          <Attention items={j.attention} />
                        </td>
                      </tr>
                    ))}
                    <tr className="totals">
                      <td>
                        Total
                        <div className="cell-sub">{jobsListedWords(data.total)}</div>
                      </td>
                      <td colSpan={8}></td>
                      <td className="num">{figureOr(data.totals.deposit_invoiced, null, "Not decided")}</td>
                      <td className="num">{figureOr(data.totals.deposit_received, null, "Not decided")}</td>
                      <td className="num">{figureOr(data.totals.billed_to_date, null, "Not decided")}</td>
                      <td className="num">{figureOr(data.totals.fuel_surcharge_billed, null, "Not decided")}</td>
                      <td className="num">{formatMoney(data.totals.collected_to_date)}</td>
                      <td className="num">{formatMoney(data.totals.other_credits_applied)}</td>
                      <td className="num">{formatMoney(data.totals.open_ar)}</td>
                      <td className="num">{figureOr(data.totals.remaining_to_bill, null, "None")}</td>
                      <td colSpan={3}></td>
                    </tr>
                    <tr className="aside">
                      <td>
                        Not on a job
                        <div className="cell-sub">
                          Unapplied payments {formatMoney(data.not_on_a_job.unapplied_payments)}
                        </div>
                      </td>
                      <td colSpan={8}></td>
                      <td className="num">{figureOr(data.not_on_a_job.deposit_invoiced, null, "Not decided")}</td>
                      <td className="num">{figureOr(data.not_on_a_job.deposit_received, null, "Not decided")}</td>
                      <td className="num">{figureOr(data.not_on_a_job.billed_to_date, null, "Not decided")}</td>
                      <td className="num">{figureOr(data.not_on_a_job.fuel_surcharge_billed, null, "Not decided")}</td>
                      <td className="num">{formatMoney(data.not_on_a_job.collected_to_date)}</td>
                      <td className="num">{formatMoney(data.not_on_a_job.other_credits_applied)}</td>
                      <td className="num">{formatMoney(data.not_on_a_job.open_ar)}</td>
                      <td className="num"></td>
                      <td colSpan={3}></td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p className="hint small table-note">
                Not on a job: documents and payments on QuickBooks rows with no job (untracked rows, tracked rows with
                no job, and the parent customer of a construction job). The table scrolls sideways inside its own box;
                the job stays in place.
              </p>
            </>
          )}
        </>
      )}

      {data && data.ledger_items.length > 0 && (
        <>
          <h2>QuickBooks projects with no job</h2>
          <ul className="issues panel">
            {data.ledger_items.map((i, n) => (
              <li key={n}>{i.message}</li>
            ))}
          </ul>
        </>
      )}

      {canManage && data && (
        <>
          <div className="section-head">
            <div>
              <h2>New pool or program job</h2>
              <p className="hint">
                Made by hand, with no estimate and no contract; never on the WIP schedule. A pool holds shared
                supplies until month-end allocation. A program is one maintenance or snow season, recognised as
                billed.
              </p>
            </div>
          </div>
          <form onSubmit={createPool} className="form-card">
            <label className="field">
              <span className="field-label">Kind</span>
              <select
                className="select"
                value={pool.kind}
                onChange={(e) => setPool({ ...pool, kind: e.target.value })}
                disabled={busy}
              >
                <option value="pool">Pool (shared supplies)</option>
                <option value="program">Program (maintenance or snow season)</option>
              </select>
            </label>
            <label className="field field-grow">
              <span className="field-label">Name</span>
              <input
                className="control"
                value={pool.name}
                onChange={(e) => setPool({ ...pool, name: e.target.value })}
                placeholder={pool.kind === "program" ? "Snow 2026-27" : "Pool - Hydroseed"}
                required
                disabled={busy}
              />
            </label>
            <label className="field">
              <span className="field-label">Division</span>
              <select
                className="select"
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

