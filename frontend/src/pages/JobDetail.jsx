import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";
import {
  amountOr,
  approvalActions,
  approvalReady,
  canApproveChangeOrders,
  canEnterBillingRequest,
  changeOrderWords,
  factsSentence,
  figureOr,
  firmLineWords,
  headLine,
  invoiceRows,
  jobFacts,
  pendingLabel,
  requestLabel,
  unapprovedWords,
  workAreaRows,
} from "../jobs.js";
import { isFirmRole } from "../roles.js";
import { Dismissed } from "./JobAttention.jsx";
import PayApplications from "./PayApplications.jsx";

// The job page for a client (F09.6; D-50): the reader is a client role (client_pm,
// client_admin, client_viewer); a firm user sees the same page with one line more. The
// job's name and its head line; where the job stands (seven facts, the same fields the
// Contract and Billing headings held); what needs attention; one table of the kept work
// areas of the original estimate and of the estimates attached as change orders, with the
// F07.4 approval control in the row (D-42; client_pm and firm_admin); the invoices by
// document with the work areas their lines are tied to (D-45), the deposit first; the pay
// applications (F08.1 Part 2, F08.3); the approval history. The one primary action is the
// billing request, opened in the Pay applications section. No set-up control is drawn
// here: attaching estimates, confirming work areas, linking QuickBooks rows and assigning
// invoice lines are the firm's work, on the set-up view (JobSetup.jsx), which a firm role
// opens from the line under the head. Every figure is the field it was, formatted by the
// same function; nothing is computed here. Mount effects only read (GET).

export default function JobDetail({ me, jobId, onBack, onOpenSetup }) {
  const [job, setJob] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null); // {id, action} of the pressed control
  const [error, setError] = useState(null);
  // F07.4: the open approval or withdrawal form, {id, action, agreed_on, agreed_by, evidence_ref, note, reason}
  const [approval, setApproval] = useState(null);
  const [evidence, setEvidence] = useState(null); // the policy key's value, read once: "none" | "reference" | null
  const [payData, setPayData] = useState(null); // what the Pay applications section loaded: the head's label
  const [openRequest, setOpenRequest] = useState(0); // each press of the head's action opens the request form

  useEffect(() => {
    api("GET", `/api/jobs/${jobId}`)
      .then(setJob)
      .catch(() => setError("The job could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, jobId]);

  useEffect(() => {
    // F07.4: whether an approval must carry a reference (the server refuses either way).
    if (!canApproveChangeOrders(me.role)) return;
    api("GET", "/api/config/policy")
      .then((rows) => {
        const key = rows.find((p) => p.key === "change_order_evidence");
        setEvidence(key && key.decided ? key.value : null);
      })
      .catch(() => setEvidence(null));
  }, [me.active_tenant_id, me.role]);

  async function run(fn) {
    setBusy(true);
    setError(null);
    try {
      setJob(await fn());
      return true;
    } catch (err) {
      setError(err.detail || "The change could not be saved. Try again.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  // F07.4 (D-42): Approve opens the small form in the row; Withdraw approval asks for the reason.
  function openApproval(areaId, action) {
    setApproval({ id: areaId, action, agreed_on: "", agreed_by: "", evidence_ref: "", note: "", reason: "" });
  }

  async function submitApproval(e) {
    e.preventDefault();
    const a = approval;
    setPending({ id: a.id, action: a.action });
    try {
      const ok = await run(() =>
        a.action === "approve"
          ? api("POST", `/api/jobs/${jobId}/work-areas/${a.id}/approval`, {
              agreed_on: a.agreed_on,
              agreed_by: a.agreed_by || null,
              evidence_ref: a.evidence_ref || null,
              note: a.note || null,
            })
          : api("POST", `/api/jobs/${jobId}/work-areas/${a.id}/approval/withdraw`, { reason: a.reason }),
      );
      if (ok) setApproval(null);
    } finally {
      setPending(null);
    }
  }

  const canApprove = canApproveChangeOrders(me.role);
  const referenceRequired = evidence === "reference";
  const columns = canApprove ? 8 : 7;

  function approvalForm(w) {
    if (!approval || approval.id !== w.id) return null;
    const approving = approval.action === "approve";
    return (
      <tr key={`${w.id}-form`}>
        <td colSpan={columns}>
          <form onSubmit={submitApproval}>
            {approving ? (
              <>
                <label className="label">
                  Date the customer agreed
                  <input
                    className="input"
                    type="date"
                    value={approval.agreed_on}
                    onChange={(e) => setApproval({ ...approval, agreed_on: e.target.value })}
                    required
                    disabled={busy}
                  />
                </label>
                <label className="label">
                  Who at the customer agreed (optional)
                  <input className="input" value={approval.agreed_by} onChange={(e) => setApproval({ ...approval, agreed_by: e.target.value })} disabled={busy} />
                </label>
                <label className="label">
                  {referenceRequired ? "Reference to the evidence (required by this company)" : "Reference to the evidence (optional)"}
                  <input className="input" value={approval.evidence_ref} onChange={(e) => setApproval({ ...approval, evidence_ref: e.target.value })} disabled={busy} />
                </label>
                <label className="label">
                  Note (optional)
                  <input className="input" value={approval.note} onChange={(e) => setApproval({ ...approval, note: e.target.value })} disabled={busy} />
                </label>
              </>
            ) : (
              <label className="label">
                Reason for withdrawing the approval
                <input className="input" value={approval.reason} onChange={(e) => setApproval({ ...approval, reason: e.target.value })} required disabled={busy} />
              </label>
            )}
            <button
              type="submit"
              className="button"
              disabled={busy || (approving ? !approvalReady(approval, referenceRequired) : !approval.reason.trim())}
            >
              {pendingLabel(approval.action, pending, w.id) || (approving ? "Record approval" : "Withdraw approval")}
            </button>{" "}
            <button type="button" className="link-button" disabled={busy} onClick={() => setApproval(null)}>
              Cancel
            </button>
          </form>
        </td>
      </tr>
    );
  }

  function approvalCell(w) {
    if (!canApprove) return null;
    const actions = approvalActions(w);
    if (actions.length === 0) return null;
    return (
      <span className="actions">
        {actions.map((a) => (
          <button
            key={a.action}
            type="button"
            className="link-button"
            disabled={busy}
            onClick={() => openApproval(w.id, a.action)}
          >
            {pendingLabel(a.action, pending, w.id) || a.label}
          </button>
        ))}
      </span>
    );
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

  // The head's one primary action: the billing request, for the roles that enter one, on a
  // fixed-price job (the section says why a time-and-materials job has none).
  const showRequest = canEnterBillingRequest(me.role) && job.revenue_method === "fixed_price";
  const facts = jobFacts(job);
  const areas = workAreaRows(job);
  const invoices = invoiceRows(job);

  return (
    <div>
      <p>
        <button type="button" className="link-button" onClick={onBack}>
          ← All jobs
        </button>
      </p>
      <div className="page-head">
        <div>
          <h1>{job.name}</h1>
          <p className="muted">{headLine(job)}</p>
        </div>
        {showRequest && (
          <div className="actions actions-row">
            <button type="button" className="button-primary" disabled={busy} onClick={() => setOpenRequest((n) => n + 1)}>
              {requestLabel(payData)}
            </button>
          </div>
        )}
      </div>
      {isFirmRole(me.role) && (
        <p className="firm-line">
          {firmLineWords(job)}{" "}
          <button type="button" className="link-button" onClick={onOpenSetup}>
            Open set-up
          </button>
        </p>
      )}
      {error && <p className="error">{error}</p>}

      <h3>Where the job stands</h3>
      <dl className="facts">
        {facts.map((f) => (
          <div key={f.label}>
            <dt>{f.label}</dt>
            <dd className={f.bold ? "fact-figure" : "fact-num"}>
              {f.value}
              {f.note && <div className="hint">{f.note}</div>}
            </dd>
          </div>
        ))}
      </dl>
      <p className="hint">{factsSentence(job)}</p>
      {job.policy_note && <p className="hint">{job.policy_note}</p>}
      {job.billing.deposit_note && <p className="hint">{job.billing.deposit_note}</p>}

      <section className="attn">
        <h3>Attention</h3>
        {job.attention.length === 0 ? (
          <p className="hint">Nothing needs attention on this job.</p>
        ) : (
          <ul className="issues">
            {job.attention.map((i, n) => (
              <li key={n}>{i.message}</li>
            ))}
          </ul>
        )}
        <Dismissed items={job.dismissed} />
      </section>

      <h3>Work areas</h3>
      {areas.length === 0 ? (
        <div className="empty">
          <p>No work areas on this job{job.revenue_method === "pool" ? ": a pool has none" : ""}.</p>
        </div>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table table-numbered">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Name</th>
                  <th className="num">Price</th>
                  <th className="num">Billed to date</th>
                  <th className="num">Left to bill</th>
                  <th>Kind</th>
                  <th>Change order</th>
                  {canApprove && <th>Action</th>}
                </tr>
              </thead>
              <tbody>
                {areas.map((w) => [
                  <tr key={w.id}>
                    <td>#{w.order_no}</td>
                    <td>
                      {w.name}
                      {w.estimate_role !== "original" && <div className="cell-sub">Estimate {w.estimate_external_id}</div>}
                    </td>
                    <td className="num">{formatMoney(w.price)}</td>
                    <td className="num">{figureOr(w.billed_to_date, null, "Not decided")}</td>
                    <td className="num">
                      {w.left_to_bill === null || w.left_to_bill === undefined ? "" : formatMoney(w.left_to_bill)}
                    </td>
                    <td>{w.kind_label}</td>
                    <td>
                      {changeOrderWords(w)}
                      {w.approval_note && <div className="hint">{w.approval_note}</div>}
                    </td>
                    {canApprove && <td>{approvalCell(w)}</td>}
                  </tr>,
                  approvalForm(w),
                ])}
                <tr className="totals">
                  <td colSpan={2}>Revised contract</td>
                  <td className="num">{amountOr(job.revised_contract, "None")}</td>
                  <td colSpan={columns - 3}></td>
                </tr>
                <tr className="aside">
                  <td colSpan={2}>{unapprovedWords(job.unapproved_change_order_count)}</td>
                  <td className="num">{amountOr(job.unapproved_change_orders, "None")}</td>
                  <td colSpan={columns - 3}></td>
                </tr>
              </tbody>
            </table>
          </div>
          <p className="hint table-note">
            A change order counts in the revised contract once approved and is listed outside it until then. Billing
            by work area is in the rows; billed to date for the whole job is in the facts above.
          </p>
        </>
      )}

      <h3>Invoices</h3>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Document</th>
              <th>Date</th>
              <th className="num">Amount</th>
              <th>Work areas</th>
            </tr>
          </thead>
          <tbody>
            {invoices.map((r) => (
              <tr key={r.key}>
                <td>
                  {r.document}
                  <div className="cell-sub">{r.sub}</div>
                </td>
                <td>{r.date}</td>
                <td className="num">{r.amount}</td>
                <td className="cell-text">{r.workAreas}</td>
              </tr>
            ))}
            <tr className="totals">
              <td colSpan={2}>Billed to date</td>
              <td className="num">{figureOr(job.billing.billed_to_date, null, "Not decided")}</td>
              <td></td>
            </tr>
          </tbody>
        </table>
      </div>

      <PayApplications jobId={jobId} me={me} openRequest={openRequest} onLoaded={setPayData} />

      <h3>Approval history</h3>
      {job.approval_history.length === 0 ? (
        <div className="empty">
          <p>No change-order approval or withdrawal recorded on this job.</p>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Work area</th>
                <th>What</th>
                <th className="num">Price</th>
                <th>Customer agreed on</th>
                <th>Who at the customer</th>
                <th>Reference</th>
                <th>Note or reason</th>
                <th>Recorded</th>
              </tr>
            </thead>
            <tbody>
              {job.approval_history.map((h) => (
                <tr key={h.id}>
                  <td>
                    #{h.order_no} {h.work_area_name}
                    {h.estimate_external_id ? ` (${h.estimate_external_id})` : ""}
                  </td>
                  <td>
                    {h.action_label}
                    {h.action === "approved" && !h.applies && (
                      <div className="hint">{h.ended ? `No longer applies: ${h.ended}` : "Withdrawn or replaced"}</div>
                    )}
                  </td>
                  <td className="num">{formatMoney(h.price)}</td>
                  <td>{h.agreed_on || ""}</td>
                  <td>{h.agreed_by || ""}</td>
                  <td className="wrap-anywhere">{h.evidence_ref || ""}</td>
                  <td>{h.note || h.reason || ""}</td>
                  <td>
                    {new Date(h.recorded_at).toLocaleString()}
                    {h.recorded_by ? ` by ${h.recorded_by}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
