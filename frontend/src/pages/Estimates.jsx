import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";
import { Dismissed } from "./JobAttention.jsx";

// Estimates (F06): every estimate loaded from the estimate template, and one
// estimate's detail (its latest version's work areas with their cost, cost by cost
// category with the D-04 basis marked, the version list, and what needs attention).
// Read-only for every role; the one primary action, "Upload estimates", opens Imports
// for the roles that may upload. Mount effects only read (GET).
//
// Everything a person reads comes from the API's display fields (status_label,
// kept_label, in_basis_label, the attention sentences); the machine values
// (status_norm, the EST_* codes, in_basis) are compared, never shown (D-22). Money
// arrives as strings with cents and is rendered by formatMoney; the page never adds.
//
// Tables use the container-scroll pattern (.table-wrap, first column held) so the
// page never scrolls sideways on a phone.
//
// Labor burden (F06.1, D-05, D-34) is computed by the API when the estimate is read and
// never stored: the detail shows estimated cost as estimated and with burden side by
// side, and EAC in the WIP basis is the burdened figure with the as-estimated one beside
// it. A missing rate or date reads "Not computed" in words, never a dash.
const STATUSES = [
  ["", "All statuses"],
  ["pending", "Pending"],
  ["sold", "Sold"],
  ["lost", "Lost"],
  ["unknown", "Unknown status"],
];

export default function Estimates({ me, canUpload, onUpload, target, onOpenJob, onReview }) {
  const [data, setData] = useState(null);
  const [status, setStatus] = useState("");
  const [estimator, setEstimator] = useState("");
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  const loadFailed = "The estimates could not be loaded. Refresh the page.";

  useEffect(() => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    if (estimator) q.set("estimator", estimator);
    const qs = q.toString();
    api("GET", `/api/estimates${qs ? `?${qs}` : ""}`)
      .then(setData)
      .catch(() => setError(loadFailed));
  }, [me.active_tenant_id, status, estimator]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    api("GET", `/api/estimates/${selected}`)
      .then(setDetail)
      .catch(() => setError("The estimate could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, selected]);

  useEffect(() => {
    setSelected(null); // another company: its own list
  }, [me.active_tenant_id]);

  // F07: a job's detail may open one of its estimates.
  useEffect(() => {
    if (target) setSelected(target);
  }, [target]);

  if (selected) {
    return (
      <div>
        <p>
          <button type="button" className="link-button" onClick={() => setSelected(null)}>
            ← All estimates
          </button>
        </p>
        {error && <p className="error">{error}</p>}
        {detail ? <Detail d={detail} onOpenJob={onOpenJob} onReview={onReview} /> : <p className="hint">Loading…</p>}
      </div>
    );
  }

  return (
    <div>
      <h2>Estimates</h2>
      {error && <p className="error">{error}</p>}
      <div className="toolbar">
        <label>
          Status{" "}
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Estimator{" "}
          <select value={estimator} onChange={(e) => setEstimator(e.target.value)}>
            <option value="">All estimators</option>
            {(data ? data.estimators : []).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        {canUpload && (
          <button type="button" className="button-primary" onClick={onUpload}>
            Upload estimates
          </button>
        )}
      </div>
      {!data ? (
        <p className="hint">Loading…</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Estimate ID</th>
                <th>Estimator</th>
                <th>Client</th>
                <th>Jobsite</th>
                <th>Name</th>
                <th>Status</th>
                <th className="num">Price</th>
                <th className="num">Versions</th>
                <th>Attention</th>
              </tr>
            </thead>
            <tbody>
              {data.estimates.length === 0 && (
                <tr>
                  <td colSpan={9}>
                    No estimates loaded for this company yet. Upload the estimate template on the Imports page.
                  </td>
                </tr>
              )}
              {data.estimates.map((e) => (
                <tr key={e.id}>
                  <td>
                    <button type="button" className="link-button" onClick={() => setSelected(e.id)}>
                      {e.external_id}
                    </button>
                  </td>
                  <td>{e.estimator || ""}</td>
                  <td>{e.client_name || ""}</td>
                  <td>{e.jobsite || ""}</td>
                  <td>{e.name}</td>
                  <td>{e.status_label}</td>
                  <td className="num">{formatMoney(e.price)}</td>
                  <td className="num">{e.versions}</td>
                  <td>
                    <Attention items={e.attention} />
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

// The sentences, never the codes (D-22). "None" is a word, so an empty cell is never
// mistaken for a missing value.
function Attention({ items }) {
  if (!items || items.length === 0) return <span className="muted">None</span>;
  return (
    <ul className="issues">
      {items.map((i, n) => (
        <li key={n}>{i.message}</li>
      ))}
    </ul>
  );
}

function burdenMoney(value) {
  return value === null || value === undefined ? "Not computed" : formatMoney(value);
}

// "EX 19.59% from 2026-01-01", one per division; the percent comes from the API as a
// string, so nothing here does arithmetic.
function rateNotes(t) {
  return t.burden_by_division.map((r) =>
    r.rate === null
      ? `${r.division_code}: no burden rate on ${t.burden_date || "no date"}`
      : `${r.division_code} ${r.rate_percent}% from ${r.rate_effective_from}`,
  );
}

function burdenDateSentence(t) {
  if (t.burden_date_source === "estimate_date") return `Burden rates as of ${t.burden_date}, the estimate date.`;
  if (t.burden_date_source === "received")
    return `Burden rates as of ${t.burden_date}, the date this version was received (the estimate has no date).`;
  return "Burden rates: no date to read them at (the estimate has no date and the company's time zone is not set).";
}

function eacWithBurden(t) {
  if (!t.basis_decided) return "Not decided";
  if (t.eac_not_computed) return "Not computed";
  return formatMoney(t.eac_in_basis);
}

function Detail({ d, onOpenJob, onReview }) {
  const t = d.totals;
  return (
    <div>
      <h2>
        {d.external_id} · {d.name}
      </h2>
      <dl className="kv">
        <dt>Status</dt>
        <dd>{d.status_label}</dd>
        <dt>Estimator</dt>
        <dd>{d.estimator || "Not given"}</dd>
        <dt>Client</dt>
        <dd>{d.client_name || "Not given"}</dd>
        <dt>Jobsite</dt>
        <dd>{d.jobsite || "Not given"}</dd>
        <dt>Price</dt>
        <dd className="num">{formatMoney(d.price)}</dd>
        <dt>Estimate date</dt>
        <dd>{d.estimate_date || "Not given"}</dd>
        {(d.job || d.to_review) && (
          <>
            <dt>Job</dt>
            <dd>
              {d.job ? (
                <>
                  <button type="button" className="link-button" onClick={() => onOpenJob(d.job.id)}>
                    {d.job.name}
                  </button>{" "}
                  ({d.job.role_label})
                </>
              ) : (
                <button type="button" className="link-button" onClick={() => onReview(d.id, d.external_id)}>
                  Sold, not yet reviewed
                </button>
              )}
            </dd>
          </>
        )}
        <dt>Versions</dt>
        <dd>
          {d.versions}
          {d.baseline_version_no ? ` (baseline: version ${d.baseline_version_no})` : " (no baseline yet)"}
        </dd>
      </dl>

      <h3>Work areas (latest version)</h3>
      {d.work_areas.length === 0 ? (
        <p className="hint">No work areas loaded for this estimate yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Order</th>
                <th>Name</th>
                <th>Kept</th>
                <th>Change order?</th>
                <th className="num">Hrs</th>
                <th className="num">Cost</th>
                <th className="num">Burden</th>
                <th className="num">Price</th>
              </tr>
            </thead>
            <tbody>
              {d.work_areas.map((w) => (
                <tr key={w.order_no}>
                  <td>#{w.order_no}</td>
                  <td>{w.name}</td>
                  <td>{w.kept_label}</td>
                  <td>{w.change_order_suggested ? "Suggested" : "No"}</td>
                  <td className="num">{formatMoney(w.hours)}</td>
                  <td className="num">{formatMoney(w.cost)}</td>
                  <td className="num">{burdenMoney(w.burden)}</td>
                  <td className="num">{formatMoney(w.price)}</td>
                </tr>
              ))}
              <tr className="totals">
                <td>Kept original</td>
                <td colSpan={6}></td>
                <td className="num">{formatMoney(t.kept_original)}</td>
              </tr>
              <tr className="totals">
                <td>Kept change orders</td>
                <td colSpan={6}></td>
                <td className="num">{formatMoney(t.kept_change_orders)}</td>
              </tr>
              <tr className="totals">
                <td>Kept total</td>
                <td colSpan={3}></td>
                <td className="num">{formatMoney(t.kept_hours)}</td>
                <td className="num">{formatMoney(t.kept_cost)}</td>
                <td className="num">{burdenMoney(t.burden_total)}</td>
                <td className="num">{formatMoney(t.kept_total)}</td>
              </tr>
              <tr className="totals">
                <td>Omitted</td>
                <td colSpan={6}></td>
                <td className="num">{formatMoney(t.omitted)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      <h3>Estimated cost by cost category (kept work areas)</h3>
      <p className="hint">{burdenDateSentence(t)}</p>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Cost category</th>
              <th>Slot</th>
              <th className="num">Hrs</th>
              <th className="num">As estimated</th>
              <th className="num">With burden</th>
              <th>In WIP basis (D-04)</th>
            </tr>
          </thead>
          <tbody>
            {t.by_category.map((c) => (
              <tr key={c.slot || "unknown"}>
                <td>
                  {c.name}
                  {c.slot === "20" &&
                    rateNotes(t).map((note) => (
                      <div key={note} className="hint">
                        {note}
                      </div>
                    ))}
                </td>
                <td>{c.slot || ""}</td>
                <td className="num">{formatMoney(c.hours)}</td>
                <td className="num">{formatMoney(c.amount)}</td>
                <td className="num">{burdenMoney(c.amount_with_burden)}</td>
                <td>{c.in_basis_label}</td>
              </tr>
            ))}
            <tr className="totals">
              <td>Total estimated cost</td>
              <td></td>
              <td className="num">{formatMoney(t.kept_hours)}</td>
              <td className="num">{formatMoney(t.cost_total_as_estimated)}</td>
              <td className="num">{burdenMoney(t.cost_total_with_burden)}</td>
              <td></td>
            </tr>
            <tr className="totals">
              <td>EAC in the WIP basis</td>
              <td></td>
              <td></td>
              <td className="num">
                {t.basis_decided && <span className="hint">{formatMoney(t.eac_in_basis_as_estimated)}</span>}
              </td>
              <td className="num">{eacWithBurden(t)}</td>
              <td>
                {!t.basis_decided
                  ? "The WIP basis policy has not been set for this company."
                  : t.eac_not_computed
                    ? "Labor burden is in the basis and is not computed; see Attention."
                    : ""}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <h3>Attention</h3>
      {d.attention.length === 0 ? (
        <p className="hint">Nothing needs attention on this estimate.</p>
      ) : (
        <ul className="issues">
          {d.attention.map((i, n) => (
            <li key={n}>{i.message}</li>
          ))}
        </ul>
      )}
      <Dismissed items={d.dismissed} />

      <h3>Versions</h3>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Version</th>
              <th>Received</th>
              <th>File</th>
              <th>Status then</th>
              <th className="num">Kept total</th>
              <th>Baseline</th>
            </tr>
          </thead>
          <tbody>
            {d.versions_list.map((v) => (
              <tr key={v.id}>
                <td>{v.version_no}</td>
                <td>{new Date(v.received_at).toLocaleString()}</td>
                <td className="wrap-anywhere">{v.original_filename || ""}</td>
                <td>{v.status_label}</td>
                <td className="num">{v.has_work_areas ? formatMoney(v.kept_total) : "No work areas"}</td>
                <td>{v.is_baseline ? "Baseline" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
