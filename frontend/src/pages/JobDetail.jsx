import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";
import {
  amountOr,
  appliedWords,
  approvalActions,
  approvalReady,
  canApproveChangeOrders,
  daysWords,
  figureOr,
  lineAction,
  offersInProgress,
  pendingLabel,
  pickedWorkArea,
  reasonText,
  suggestedPairs,
} from "../jobs.js";
import { KindActions } from "../kindActions.js";
import Attention from "./JobAttention.jsx";
import PayApplications from "./PayApplications.jsx";

// Job detail (F07): the job's header (edited in place by the roles that manage jobs),
// its estimates with their roles, the work areas of its original estimate with their
// kind (suggested until a person confirms it, D-01), the computed contract figures, and
// its QuickBooks links. Suggestions for a link say why; the search lists any active,
// unlinked QuickBooks row by name, and a link is always made by the row's id. Every
// action writes one audit row. Mount effects only read (GET). F07.1: "Confirm all as
// suggested" confirms every unconfirmed kept work area at its suggestion in one press
// (one audit row each); the Action column says what a click will do; Sold on can be
// corrected. F07.4 (D-42): a confirmed change order reads approved or not; client_pm and
// firm_admin approve it (a small form in the row: the date the customer agreed, who, a
// reference, a note) or withdraw an approval with a reason; the approval history is below
// the table; the figures follow on read. F08.1 (D-45): the work-area tables show billed
// to date and left to bill per work area from the tied invoice lines; the "Invoice lines"
// section lists every line with how it is tied ("#n", or a person's assignment, by id);
// the suggestion by name is shown and never applied until a person presses.

export default function JobDetail({ me, jobId, canManage, onBack, onOpenEstimate }) {
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
  // F07.4: the open approval or withdrawal form, {id, action, agreed_on, agreed_by, evidence_ref, note, reason}
  const [approval, setApproval] = useState(null);
  const [evidence, setEvidence] = useState(null); // the policy key's value, read once: "none" | "reference" | null
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

  // F07.4 (D-42): Approve opens the small form in the row; Withdraw approval asks for the reason.
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

  function approvalForm(w) {
    if (!approval || approval.id !== w.id) return null;
    const approving = approval.action === "approve";
    return (
      <tr key={`${w.id}-form`}>
        <td colSpan={canManage || canApprove ? 9 : 8}>
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

  function billedCells(w) {
    // F08.1: billed to date on the work area from its tied lines; left to bill is blank for
    // an omitted row and for a change order that is not approved (outside the contract).
    return [
      <td key="billed" className="num">
        {figureOr(w.billed_to_date, null, "Not decided")}
      </td>,
      <td key="left" className="num">
        {w.left_to_bill === null || w.left_to_bill === undefined ? "" : formatMoney(w.left_to_bill)}
      </td>,
    ];
  }

  function totalsRow(totals, trailing) {
    return (
      <tr className="totals">
        <td colSpan={3}>Total of the kept work areas</td>
        <td className="num">{formatMoney(totals.price)}</td>
        <td className="num">{figureOr(totals.billed_to_date, null, "Not decided")}</td>
        <td className="num">{figureOr(totals.left_to_bill, null, "Not decided")}</td>
        <td colSpan={trailing}></td>
      </tr>
    );
  }

  function workAreaRows(areas) {
    return areas.map((w) => [
      <tr key={w.id}>
        <td>#{w.order_no}</td>
        <td>{w.name}</td>
        <td>{w.kept_label}</td>
        <td className="num">{formatMoney(w.price)}</td>
        {billedCells(w)}
        <td>{w.kind_label}</td>
        <td>
          {w.approval_label || ""}
          {w.approval_note && <div className="hint">{w.approval_note}</div>}
        </td>
        {(canManage || canApprove) && (
          <td>
            {canManage && w.estimate_role === "original" && (
              <KindActions
                area={w}
                busy={busy}
                onConfirm={(k) => run(() => api("POST", `/api/jobs/${jobId}/work-areas/${w.id}/kind`, { kind: k }))}
              />
            )}
            {approvalCell(w)}
          </td>
        )}
      </tr>,
      approvalForm(w),
    ]);
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
      edit.notes !== (job.notes || "") ||
      edit.sold_on !== job.sold_on);

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

      <h3>Contract</h3>
      <dl className="kv">
        <dt>Original contract</dt>
        <dd className="num">
          {amountOr(job.original_contract, "None")}
          {job.revised_contract_note && <div className="hint">{job.revised_contract_note}</div>}
        </dd>
        <dt>Approved change orders</dt>
        <dd className="num">{amountOr(job.approved_change_orders, "None")}</dd>
        <dt>Revised contract</dt>
        <dd className="num">{amountOr(job.revised_contract, "None")}</dd>
        <dt>Unapproved change orders</dt>
        <dd className="num">
          {amountOr(job.unapproved_change_orders, "None")}
          {job.unapproved_change_order_count > 0 && (
            <div className="hint">
              {job.unapproved_change_order_count} change {job.unapproved_change_order_count === 1 ? "order" : "orders"}
            </div>
          )}
        </dd>
        <dt>EAC in the WIP basis</dt>
        <dd className="num">
          {amountOr(job.eac_in_basis, "Not computed")}
          {job.eac_note && <div className="hint">{job.eac_note}</div>}
        </dd>
      </dl>
      <p className="hint">
        Original contract counts the kept work areas of the original estimate confirmed as original. A change order
        joins the revised contract when the project manager records that the customer agreed, at its price that day
        (D-42); until then it is shown unapproved, outside the contract. EAC counts the estimated cost of every kept
        work area, approved or not (D-44).
      </p>

      <h3>Billing</h3>
      <p className="hint">
        {job.tenant_name}. Figures to date as of {job.as_of}, computed from QuickBooks when read; nothing is stored. A
        deposit is the invoice numbered estimate_DEP on a deposit item (D-02); fuel surcharge lines and sales tax are
        outside billed to date (D-39).
      </p>
      {job.policy_note && <p className="hint">{job.policy_note}</p>}
      <dl className="kv">
        <dt>Deposit invoiced</dt>
        <dd className="num">{figureOr(job.billing.deposit_invoiced, null, "Not decided")}</dd>
        <dt>Deposit received</dt>
        <dd className="num">{figureOr(job.billing.deposit_received, null, "Not decided")}</dd>
        <dt>Billed to date</dt>
        <dd className="num">{figureOr(job.billing.billed_to_date, null, "Not decided")}</dd>
        <dt>Fuel surcharge billed</dt>
        <dd className="num">{figureOr(job.billing.fuel_surcharge_billed, null, "Not decided")}</dd>
        <dt>Collected to date</dt>
        <dd className="num">{formatMoney(job.billing.collected_to_date)}</dd>
        <dt>Other credits applied</dt>
        <dd className="num">{formatMoney(job.billing.other_credits_applied)}</dd>
        <dt>Open A/R</dt>
        <dd className="num">{formatMoney(job.billing.open_ar)}</dd>
        <dt>Unapplied payments</dt>
        <dd className="num">{formatMoney(job.billing.unapplied_payments)}</dd>
        <dt>Remaining to bill</dt>
        <dd className="num">
          {figureOr(job.billing.remaining_to_bill, job.billing.remaining_to_bill_note, "Not decided")}
        </dd>
        <dt>Last billing date</dt>
        <dd>{job.billing.last_billing_date || "None"}</dd>
        <dt>Last payment date</dt>
        <dd>{job.billing.last_payment_date || "None"}</dd>
        <dt>Days since last activity</dt>
        <dd className="num">{daysWords(job.billing.days_since_activity)}</dd>
      </dl>
      {job.billing.deposit_note && <p className="hint">{job.billing.deposit_note}</p>}

      <h4>Billing history</h4>
      {job.billing_history.length === 0 ? (
        <p className="hint">No invoice, credit memo or sales receipt on the QuickBooks rows of this job.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Document</th>
                <th>Date</th>
                <th>Kind</th>
                <th className="num">Total</th>
                <th className="num">Sales tax</th>
                <th className="num">Fuel surcharge</th>
                <th className="num">Counted in billed to date</th>
                <th className="num">Balance</th>
                <th>Deposit</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {job.billing_history.map((d) => (
                <tr key={d.billing_id}>
                  <td>{d.doc_number || `QuickBooks id ${d.external_id}`}</td>
                  <td>{d.txn_date}</td>
                  <td>{d.kind_label}</td>
                  <td className="num">{formatMoney(d.total)}</td>
                  <td className="num">{formatMoney(d.sales_tax)}</td>
                  <td className="num">{figureOr(d.fuel_surcharge, null, "Not decided")}</td>
                  <td className="num">{d.counted ? figureOr(d.billed, null, "Not decided") : "Not counted"}</td>
                  <td className="num">{formatMoney(d.balance)}</td>
                  <td>{d.is_deposit ? "The deposit" : ""}</td>
                  <td>{d.state_label}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4>Payment history</h4>
      {job.payment_history.length === 0 ? (
        <p className="hint">No payment on the QuickBooks rows or documents of this job.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Payment</th>
                <th>Date</th>
                <th>Kind</th>
                <th className="num">Amount</th>
                <th>Applied to</th>
                <th className="num">Unapplied</th>
                <th className="num">Other credits applied</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {job.payment_history.map((p) => (
                <tr key={p.payment_id}>
                  <td>
                    QuickBooks id {p.external_id}
                    {!p.on_this_job && <div className="hint">On another customer row; applied here</div>}
                  </td>
                  <td>{p.txn_date}</td>
                  <td>{p.kind_label}</td>
                  <td className="num">{formatMoney(p.total)}</td>
                  <td>{appliedWords(p.applied)}</td>
                  <td className="num">{figureOr(p.unapplied, null, "Not on this row")}</td>
                  <td className="num">{figureOr(p.other_credit, null, "Not on this row")}</td>
                  <td>{p.state_label}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
                    <th className="num">Billed to date</th>
                    <th className="num">Left to bill</th>
                    <th>Kind</th>
                    <th>Approval</th>
                    {(canManage || canApprove) && <th>Action</th>}
                  </tr>
                </thead>
                <tbody>
                  {workAreaRows(job.work_areas)}
                  {totalsRow(job.work_area_totals, canManage || canApprove ? 3 : 2)}
                </tbody>
              </table>
            </div>
            </>
          )}
        </>
      )}

      {job.change_order_work_areas.length > 0 && (
        <>
          <h3>Work areas of the estimates attached as change orders (latest versions)</h3>
          <p className="hint">
            Every kept work area here is a change order by the role of its estimate (D-03); it joins the revised
            contract when it is approved (D-42).
          </p>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Name</th>
                  <th>Kept</th>
                  <th className="num">Price</th>
                  <th className="num">Billed to date</th>
                  <th className="num">Left to bill</th>
                  <th>Estimate</th>
                  <th>Approval</th>
                  {(canManage || canApprove) && <th>Action</th>}
                </tr>
              </thead>
              <tbody>
                {job.change_order_work_areas.map((w) => [
                  <tr key={w.id}>
                    <td>#{w.order_no}</td>
                    <td>{w.name}</td>
                    <td>{w.kept_label}</td>
                    <td className="num">{formatMoney(w.price)}</td>
                    {billedCells(w)}
                    <td>{w.estimate_external_id}</td>
                    <td>
                      {w.approval_label || ""}
                      {w.approval_note && <div className="hint">{w.approval_note}</div>}
                    </td>
                    {(canManage || canApprove) && <td>{approvalCell(w)}</td>}
                  </tr>,
                  approvalForm(w),
                ])}
                {totalsRow(job.change_order_totals, canManage || canApprove ? 3 : 2)}
              </tbody>
            </table>
          </div>
        </>
      )}

      {job.approval_history.length > 0 && (
        <>
          <h3>Approval history</h3>
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

      <PayApplications jobId={jobId} me={me} />

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

      <h3>Attention</h3>
      {job.attention.length === 0 ? (
        <p className="hint">Nothing needs attention on this job.</p>
      ) : (
        <Attention items={job.attention} />
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
