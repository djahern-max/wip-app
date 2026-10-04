import { useEffect, useState } from "react";
import { api } from "../api.js";
import { jobCell, pageLabel, resultSentence, trackLabel, trackedSentence, untrackLabel } from "../customers.js";
import CustomerDuplicates from "./CustomerDuplicates.jsx";

// Customers (F07.2, D-37): the owner picks the QuickBooks customers and projects to work
// on. "Tracked" lists what is picked, each with its job or the sentence that it needs
// one; below it a search over the company's active QuickBooks rows by name, projects
// first, a page at a time, with a Track control per row. Track and untrack send the
// row's id and nothing else; the name is only searched. The duplicates list (F07) is
// behind a link. Roles: the ones that link a job. Mount effects only read (GET).

export default function Customers({ me, onOpenJob }) {
  const [view, setView] = useState("picker");
  const [tracked, setTracked] = useState(null);
  const [q, setQ] = useState("");
  const [asked, setAsked] = useState(""); // the text the shown results are for
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    setView("picker");
    setTracked(null);
    setQ("");
    setAsked("");
    setResult(null);
    setError(null);
    api("GET", "/api/customers/tracked")
      .then(setTracked)
      .catch(() => setError("The tracked list could not be loaded. Refresh the page."));
  }, [me.active_tenant_id]);

  async function search(e, page = 1) {
    if (e) e.preventDefault();
    const text = q.trim();
    setAsked(text);
    if (!text) {
      setResult(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setResult(await api("GET", `/api/customers?q=${encodeURIComponent(text)}&page=${page}`));
    } catch (err) {
      setError(err.detail || "The search could not be run. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function act(row, action) {
    setBusy(true);
    setError(null);
    try {
      setTracked(await api("POST", `/api/customers/${row.customer_id}/${action}`));
      if (asked) setResult(await api("GET", `/api/customers?q=${encodeURIComponent(asked)}&page=${result ? result.page : 1}`));
    } catch (err) {
      setError(err.detail || `The row could not be ${action === "track" ? "tracked" : "untracked"}. Try again.`);
    } finally {
      setBusy(false);
    }
  }

  function job(row) {
    const cell = jobCell(row);
    if (cell.jobId && onOpenJob) {
      return (
        <button type="button" className="link-button" onClick={() => onOpenJob(cell.jobId)}>
          {cell.text}
        </button>
      );
    }
    return cell.text;
  }

  return (
    <div>
      <ul className="subnav">
        <li>
          {view === "picker" ? (
            <span className="current">Customers and projects</span>
          ) : (
            <button type="button" className="link-button" onClick={() => setView("picker")}>
              Customers and projects
            </button>
          )}
        </li>
        <li>
          {view === "duplicates" ? (
            <span className="current">Possible duplicate customers</span>
          ) : (
            <button type="button" className="link-button" onClick={() => setView("duplicates")}>
              Possible duplicate customers
            </button>
          )}
        </li>
      </ul>
      {view === "duplicates" ? (
        <CustomerDuplicates me={me} />
      ) : (
        <>
          <h2>Tracked</h2>
          <p className="hint">
            The platform works on the QuickBooks customers and projects picked here, and on the rows linked to a job.
            Everything else stays in the copy the sync keeps, out of the way. Nothing here changes QuickBooks.
          </p>
          {error && <p className="error">{error}</p>}
          {!tracked ? (
            <p className="hint">{error ? "" : "Loading…"}</p>
          ) : (
            <>
              <p>{trackedSentence(tracked.rows)}</p>
              {tracked.rows.length > 0 && (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>Under</th>
                        <th>What it is</th>
                        <th>QuickBooks id</th>
                        <th>Job</th>
                        <th>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tracked.rows.map((r) => (
                        <tr key={r.customer_id}>
                          <td>{r.display_name}</td>
                          <td>{r.parent_name || ""}</td>
                          <td>{r.kind_label}</td>
                          <td>{r.external_id}</td>
                          <td>{job(r)}</td>
                          <td>
                            {r.job ? (
                              <span className="muted">{untrackLabel(r)}</span>
                            ) : (
                              <button type="button" className="button" disabled={busy} onClick={() => act(r, "untrack")}>
                                {untrackLabel(r)}
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}

          <h2>Find a customer or project</h2>
          <form className="inline-form" onSubmit={search}>
            <label className="label">
              Name
              <input
                className="input"
                type="search"
                value={q}
                maxLength={200}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Part of the QuickBooks name"
              />
            </label>
            <button type="submit" className="button button-primary" disabled={busy}>
              Search
            </button>
          </form>
          <p className="hint">{resultSentence(asked, result ? result.total : 0)}</p>
          {result && result.rows.length > 0 && (
            <>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Under</th>
                      <th>What it is</th>
                      <th>QuickBooks id</th>
                      <th className="num">Invoices and credits</th>
                      <th className="num">Payments</th>
                      <th>Job</th>
                      <th>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((r) => (
                      <tr key={r.customer_id}>
                        <td>{r.display_name}</td>
                        <td>{r.parent_name || ""}</td>
                        <td>{r.kind_label}</td>
                        <td>{r.external_id}</td>
                        <td className="num">{r.billing_count}</td>
                        <td className="num">{r.payment_count}</td>
                        <td>{job(r)}</td>
                        <td>
                          {r.tracked ? (
                            <span className="muted">{trackLabel(r)}</span>
                          ) : (
                            <button type="button" className="button" disabled={busy} onClick={() => act(r, "track")}>
                              {trackLabel(r)}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {result.pages > 1 && (
                <div className="toolbar">
                  <span>{pageLabel(result.page, result.pages)}</span>
                  <button type="button" className="button" disabled={busy || result.page <= 1} onClick={() => search(null, result.page - 1)}>
                    Previous page
                  </button>
                  <button
                    type="button"
                    className="button"
                    disabled={busy || result.page >= result.pages}
                    onClick={() => search(null, result.page + 1)}
                  >
                    Next page
                  </button>
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
