import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";
import {
  applyToAll,
  canEnterBillingRequest,
  canIssuePayApplications,
  pendingLabel,
  requestBody,
  requestReady,
} from "../jobs.js";

// Pay applications (F08.1 Part 2; D-26, D-36, D-39): the list, the billing request form
// (one row per listed work area, cumulative percent complete, a job-level percent), the
// draft with its exceptions, Issue, Void (with a reason) and the PDF. Mount effects only
// read (GET). Money is shown as the API gives it; percents are sent as typed.

export default function PayApplications({ jobId, me }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null);
  const [form, setForm] = useState(null);
  const [applyAll, setApplyAll] = useState("");
  const [open, setOpen] = useState(null); // the application shown in full
  const [voidReason, setVoidReason] = useState(null); // {id, reason} while Void asks

  useEffect(() => {
    api("GET", `/api/jobs/${jobId}/pay-applications`)
      .then(setData)
      .catch(() => setError("The pay applications could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, jobId]);

  const canRequest = canEnterBillingRequest(me.role);
  const canIssue = canIssuePayApplications(me.role);

  async function reload(showId) {
    const d = await api("GET", `/api/jobs/${jobId}/pay-applications`);
    setData(d);
    if (showId) setOpen(showId);
    return d;
  }

  async function run(id, action, fn) {
    setBusy(true);
    setError(null);
    setPending({ id, action });
    try {
      const result = await fn();
      await reload(result && result.id);
      return result;
    } catch (err) {
      setError(err.detail || "The change could not be saved. Try again.");
      return null;
    } finally {
      setBusy(false);
      setPending(null);
    }
  }

  function startRequest() {
    const percents = {};
    for (const a of data.schedule) percents[a.id] = a.previous_percent;
    setForm({ application_date: new Date().toISOString().slice(0, 10), surcharge_applies: "", percents });
    setApplyAll("");
  }

  async function submit(e) {
    e.preventDefault();
    const saved = await run("draft", "draft", () => api("POST", `/api/jobs/${jobId}/pay-applications`, requestBody(form)));
    if (saved) setForm(null);
  }

  function issue(id) {
    return run(id, "issue", () => api("POST", `/api/jobs/${jobId}/pay-applications/${id}/issue`));
  }

  async function submitVoid(e) {
    e.preventDefault();
    const saved = await run(voidReason.id, "void", () =>
      api("POST", `/api/jobs/${jobId}/pay-applications/${voidReason.id}/void`, { reason: voidReason.reason }),
    );
    if (saved) setVoidReason(null);
  }

  if (!data) {
    return (
      <>
        <h3>Pay applications</h3>
        <p className="hint">{error || "Loading…"}</p>
      </>
    );
  }

  const shown = data.applications.find((a) => a.id === open) || null;

  return (
    <>
      <h3>Pay applications</h3>
      {error && <p className="error">{error}</p>}
      {!data.fixed_price ? (
        <p className="hint">{data.note || "Only a fixed-price job has pay applications (D-24)."}</p>
      ) : (
        <>
          <p className="hint">
            The customer receives the pay application: earned to date by work area and the amount due. The controller
            keys one invoice in QuickBooks from it, numbered as shown; the platform writes nothing to QuickBooks (D-36).
          </p>
          {data.applications.length === 0 ? (
            <p className="hint">No pay application yet on this job.</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Number</th>
                    <th>Application date</th>
                    <th>Status</th>
                    <th className="num">Amount due</th>
                    <th className="num">Total to invoice</th>
                    <th>Invoice</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {data.applications.map((a) => (
                    <tr key={a.id}>
                      <td>Pay application {a.number}</td>
                      <td>{a.application_date}</td>
                      <td>{a.status_words}</td>
                      <td className="num">{formatMoney(a.summary.amount_due)}</td>
                      <td className="num">{a.summary.total_to_invoice ? formatMoney(a.summary.total_to_invoice) : "None due"}</td>
                      <td>{invoiceWords(a)}</td>
                      <td>
                        <button type="button" className="link-button" disabled={busy} onClick={() => setOpen(a.id === open ? null : a.id)}>
                          {a.id === open ? "Hide" : "Show"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {canRequest && !form && (
            <p>
              <button type="button" className="button" disabled={busy} onClick={startRequest}>
                New billing request
              </button>
            </p>
          )}
          {form && (
            <form onSubmit={submit} className="inline-form">
              <h4>Billing request for pay application {data.next_number}</h4>
              <p className="hint">
                Cumulative percent complete to date per work area (D-26). The schedule lists every kept original work
                area and every change order approved on the application date (D-36).
              </p>
              <label className="label">
                Application date
                <input
                  className="input"
                  type="date"
                  value={form.application_date}
                  onChange={(e) => setForm({ ...form, application_date: e.target.value })}
                  disabled={busy}
                />
              </label>
              <fieldset>
                <legend>Fuel surcharge on this application (D-39)</legend>
                <label>
                  <input type="radio" name="surcharge" checked={form.surcharge_applies === "yes"} onChange={() => setForm({ ...form, surcharge_applies: "yes" })} disabled={busy} />{" "}
                  Applies{data.surcharge_percent ? ` (${data.surcharge_percent}%)` : ""}
                </label>{" "}
                <label>
                  <input type="radio" name="surcharge" checked={form.surcharge_applies === "no"} onChange={() => setForm({ ...form, surcharge_applies: "no" })} disabled={busy} />{" "}
                  Does not apply
                </label>
                {!data.surcharge_rate_decided && (
                  <p className="hint">The fuel surcharge rate is not decided (Configuration, Policy); issue is refused while it applies.</p>
                )}
              </fieldset>
              <label className="label">
                Apply one percent to every listed work area
                <input className="input" inputMode="decimal" value={applyAll} onChange={(e) => setApplyAll(e.target.value)} disabled={busy} />
              </label>
              <button
                type="button"
                className="link-button"
                disabled={busy || applyAll === ""}
                onClick={() => setForm({ ...form, percents: applyToAll(form.percents, applyAll) })}
              >
                Apply to every work area
              </button>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Work area</th>
                      <th className="num">Scheduled value</th>
                      <th className="num">Percent complete to date</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.schedule.map((a) => (
                      <tr key={a.id}>
                        <td>
                          {a.label} {a.name}
                        </td>
                        <td className="num">{formatMoney(a.price)}</td>
                        <td className="num">
                          <input
                            className="input"
                            inputMode="decimal"
                            aria-label={`Percent complete to date for ${a.label} ${a.name}`}
                            value={form.percents[a.id]}
                            onChange={(e) => setForm({ ...form, percents: { ...form.percents, [a.id]: e.target.value } })}
                            disabled={busy}
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <button type="submit" className="button" disabled={busy || !requestReady(form)}>
                {pendingLabel("draft", pending, "draft") || "Save draft"}
              </button>{" "}
              <button type="button" className="link-button" disabled={busy} onClick={() => setForm(null)}>
                Cancel
              </button>
            </form>
          )}
          {shown && (
            <Application
              app={shown}
              jobId={jobId}
              busy={busy}
              pending={pending}
              canIssue={canIssue}
              onIssue={() => issue(shown.id)}
              voidReason={voidReason}
              setVoidReason={setVoidReason}
              onVoid={submitVoid}
            />
          )}
        </>
      )}
    </>
  );
}

function invoiceWords(a) {
  if (a.invoice_held) return a.invoice_tied ? `${a.invoice_held}, ties` : `${a.invoice_held}, does not tie`;
  if (a.status !== "issued") return "";
  return a.invoice_number ? `${a.invoice_number} not yet in QuickBooks` : "No invoice is due";
}

function Application({ app, jobId, busy, pending, canIssue, onIssue, voidReason, setVoidReason, onVoid }) {
  const s = app.summary;
  return (
    <div>
      <h4>Pay application {app.number}</h4>
      <p>
        {app.application_date}. {app.status_words}
        {app.issued_by ? ` by ${app.issued_by}` : ""}
        {app.created_by && app.status === "draft" ? `, entered by ${app.created_by}` : ""}
      </p>
      {app.issues.length > 0 && (
        <ul className="issues">
          {app.issues.map((i, n) => (
            <li key={n}>{i.message}</li>
          ))}
        </ul>
      )}
      {app.notes.map((n) => (
        <p key={n} className="hint">
          {n}
        </p>
      ))}
      <h5>Schedule of values</h5>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Work area</th>
              <th className="num">Scheduled value</th>
              <th className="num">Percent complete to date</th>
              <th className="num">Earned to date</th>
              <th className="num">Earned on previous applications</th>
              <th className="num">Earned this application</th>
              <th className="num">Balance to finish</th>
            </tr>
          </thead>
          <tbody>
            {app.lines.map((ln) => (
              <tr key={ln.work_area_id}>
                <td>
                  {ln.label} {ln.name}
                </td>
                <td className="num">{formatMoney(ln.scheduled_value)}</td>
                <td className="num">{ln.percent_complete}%</td>
                <td className="num">{formatMoney(ln.earned_to_date)}</td>
                <td className="num">{formatMoney(ln.earned_previous)}</td>
                <td className="num">{formatMoney(ln.earned_this_application)}</td>
                <td className="num">{formatMoney(ln.balance_to_finish)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <dl className="kv">
        <dt>Total earned to date</dt>
        <dd className="num">{formatMoney(s.earned_to_date)}</dd>
        <dt>Less billed to date before this application</dt>
        <dd className="num">{formatMoney(s.billed_before)}</dd>
        <dt>Amount due this application</dt>
        <dd className="num">{formatMoney(s.amount_due)}</dd>
        {s.billed_ahead && (
          <>
            <dt>Billed ahead by</dt>
            <dd className="num">{formatMoney(s.billed_ahead)}</dd>
          </>
        )}
        {s.surcharge_label && (
          <>
            <dt>{s.surcharge_label}</dt>
            <dd className="num">{formatMoney(s.surcharge)}</dd>
          </>
        )}
        {s.total_to_invoice && (
          <>
            <dt>Total to invoice</dt>
            <dd className="num">{formatMoney(s.total_to_invoice)}</dd>
          </>
        )}
      </dl>
      {s.no_invoice_due ? (
        <p>No invoice is due.</p>
      ) : (
        <p>
          To key in QuickBooks: invoice <strong>{app.invoice_number}</strong>, description{" "}
          <strong>Pay application {app.number}</strong>, amount {formatMoney(s.amount_due)}
          {s.surcharge ? `, plus the fuel surcharge line ${formatMoney(s.surcharge)} on a fuel surcharge item` : ""}.
        </p>
      )}
      <p className="actions">
        <a className="link-button" href={`/api/jobs/${jobId}/pay-applications/${app.id}/pdf`} target="_blank" rel="noreferrer">
          PDF
        </a>{" "}
        {canIssue && app.status === "draft" && (
          <button type="button" className="button" disabled={busy} onClick={onIssue}>
            {pendingLabel("issue", pending, app.id) || "Issue"}
          </button>
        )}{" "}
        {canIssue && app.status !== "void" && !voidReason && (
          <button type="button" className="link-button" disabled={busy} onClick={() => setVoidReason({ id: app.id, reason: "" })}>
            Void
          </button>
        )}
      </p>
      {voidReason && voidReason.id === app.id && (
        <form onSubmit={onVoid} className="inline-form">
          <label className="label">
            Reason for voiding pay application {app.number}
            <input className="input" value={voidReason.reason} onChange={(e) => setVoidReason({ ...voidReason, reason: e.target.value })} disabled={busy} />
          </label>
          <button type="submit" className="button" disabled={busy || !voidReason.reason.trim()}>
            {pendingLabel("void", pending, app.id) || "Void application"}
          </button>{" "}
          <button type="button" className="link-button" disabled={busy} onClick={() => setVoidReason(null)}>
            Cancel
          </button>
        </form>
      )}
    </div>
  );
}
