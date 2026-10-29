import { useEffect, useState } from "react";
import { api } from "../api.js";
import { addMoney, formatMoney } from "../money.js";

// Cost (F10; D-22, D-47, D-49, D-50, D-51): a firm screen, read by the roles that read
// Connections. The reader knows accounting. Everything is GET /api/cost, computed when read
// from the ledger lines, the confirmed account mapping and the WIP basis; nothing is stored.
// The tie-out by month and account (BLUEPRINT §8.5 check 2): the five parts and the ledger
// total, which they equal by construction, so the tick means "every payload read"; under it
// the unassigned list by document (what to fix in QuickBooks or Ramp, never here) and labor in
// the ledger by month and division (§8.5 check 3's ledger side; F11 brings the other side).
// One primary action: Export (XLSX, the F08 writer). Money arrives as strings with cents and is
// rendered by formatMoney; the page never parses a number. Mount effects only read (GET).

const TIE_COLUMNS = [
  ["on_jobs", "On jobs"],
  ["memo_on_jobs", "Owned equipment and fuel on jobs"],
  ["unassigned", "Unassigned"],
  ["labor_in_ledger", "Labor in the ledger"],
  ["outside_job_cost", "Outside job cost"],
  ["total", "Ledger total"],
];

export default function Cost({ me, onOpenCustomers }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setData(null);
    setError(null);
    api("GET", "/api/cost")
      .then(setData)
      .catch(() => setError("The cost page could not be loaded. Refresh the page."));
  }, [me.active_tenant_id]);

  if (!data) {
    return (
      <div>
        <h1>Cost</h1>
        {error ? <p className="error">{error}</p> : <p className="hint">Loading…</p>}
      </div>
    );
  }

  const ties = data.balanced && data.months > 0;
  const totals = Object.fromEntries(
    TIE_COLUMNS.map(([key]) => [key, data.tie_out.reduce((sum, r) => addMoney(sum, r[key]), "0.00")]),
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Cost</h1>
          <p className="muted">
            {data.tenant_name}. Figures to date as of {data.as_of}, from QuickBooks
            {data.history_start ? `, from the history start ${data.history_start}` : ""}.
          </p>
        </div>
        <div className="actions actions-row">
          <a className="button-primary" href="/api/cost/export.xlsx" download>
            Export
          </a>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      <p className="done-line tie-line">
        {ties && (
          <span className="step-marker step-marker-done" aria-hidden="true">
            ✓
          </span>
        )}
        <span className={ties ? "tie-words tie-words-done" : "tie-words"}>Tie-out: {data.status}</span>
      </p>
      <p className="hint small">
        Cost lines: {data.lines_held} held. A line&apos;s job is the customer or project on the line in QuickBooks; the
        platform never decides one. Fix a line in QuickBooks or in Ramp, then wait for the next change poll.
      </p>
      {data.policy_note && <p className="hint small">{data.policy_note}</p>}

      <h2>Tie-out by month and account</h2>
      <p className="hint small">
        On jobs, owned equipment and fuel on jobs, unassigned, labor in the ledger and outside job cost add up to
        every account&apos;s ledger total. Compare the ledger total to the QuickBooks Profit and Loss by account for
        the month.
      </p>
      {data.tie_out.length === 0 ? (
        <div className="empty">
          <strong>No cost lines held yet.</strong>
          <span className="muted">Bills, expenses, vendor credits, journal entries and deposits arrive with the sync.</span>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Account</th>
                <th>Month</th>
                {TIE_COLUMNS.map(([key, label]) => (
                  <th key={key} className="num">
                    {label}
                  </th>
                ))}
                <th>Ties</th>
              </tr>
            </thead>
            <tbody>
              {data.tie_out.map((r) => (
                <tr key={`${r.month}-${r.account_key}`}>
                  <td>{r.account_label}</td>
                  <td>{r.month}</td>
                  {TIE_COLUMNS.map(([key]) => (
                    <td key={key} className="num">
                      {formatMoney(r[key])}
                    </td>
                  ))}
                  <td>{r.balanced ? "Ties" : `Does not tie by ${formatMoney(r.difference)}`}</td>
                </tr>
              ))}
              <tr className="totals">
                <td>Total</td>
                <td></td>
                {TIE_COLUMNS.map(([key]) => (
                  <td key={key} className="num">
                    {formatMoney(totals[key])}
                  </td>
                ))}
                <td></td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      <h2>Unassigned</h2>
      <p className="hint small">
        Job-cost lines with no job: coded to a customer or project with no job, or to no customer. Link the row to
        its job on Customers, or correct the line in QuickBooks.
      </p>
      {data.unassigned.length === 0 ? (
        <div className="empty">
          <strong>Nothing unassigned.</strong>
          <span className="muted">Every job-cost line has a job.</span>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Document</th>
                <th>Date</th>
                <th>Vendor</th>
                <th>Account</th>
                <th className="num">Amount</th>
                <th>Customer row</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {data.unassigned.map((u) => (
                <tr key={`${u.txn_type}-${u.txn_external_id}-${u.customer_id || "none"}`}>
                  <td>
                    {u.txn_type_label} {u.document}
                    {u.lines > 1 && <div className="cell-sub">{u.lines} lines</div>}
                  </td>
                  <td>{u.txn_date}</td>
                  <td>{u.vendor || <span className="muted">No vendor</span>}</td>
                  <td className="cell-text">{u.accounts}</td>
                  <td className="num">{formatMoney(u.amount)}</td>
                  <td className="cell-text">{u.customer_label}</td>
                  <td>
                    {u.customer_tracked ? (
                      <button type="button" className="link-button" onClick={onOpenCustomers}>
                        Open the customer
                      </button>
                    ) : (
                      <span className="muted">{u.customer_id ? "Not tracked" : ""}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2>Labor in the ledger</h2>
      <p className="hint small">
        Payroll posted to the Labor and Labor Burden accounts, by month and division. It is never job cost and never
        unassigned: labor reaches jobs from hours and rates.
      </p>
      {data.labor.length === 0 ? (
        <div className="empty">
          <strong>No labor posted yet.</strong>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Month</th>
                <th>Division</th>
                <th className="num">Amount</th>
              </tr>
            </thead>
            <tbody>
              {data.labor.map((r) => (
                <tr key={`${r.month}-${r.division_code || "none"}`}>
                  <td>{r.month}</td>
                  <td>{r.division_name || r.division_code || <span className="muted">No division</span>}</td>
                  <td className="num">{formatMoney(r.amount)}</td>
                </tr>
              ))}
              <tr className="totals">
                <td>Total</td>
                <td></td>
                <td className="num">{formatMoney(data.labor.reduce((sum, r) => addMoney(sum, r.amount), "0.00"))}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
