import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";
import { attachReady, nextIndex, reasonText } from "../jobs.js";
import Attention from "./JobAttention.jsx";

// Review sold estimates (F07, D-03): one sold estimate at a time. The default is a new
// job; the person may instead attach it to an existing job with a role. Suggested jobs
// come first with the reason each was suggested; any other job can be chosen from the
// list. Nothing is written until the person presses New job or Attach; Skip writes
// nothing. Mount effects only read (GET).

export default function JobReview({ me, canManage, onBack, onOpenJob }) {
  const [queue, setQueue] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [index, setIndex] = useState(0);
  const [form, setForm] = useState({ division_id: "", name: "" });
  const [attach, setAttach] = useState({ jobId: "", role: "change_order", note: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api("GET", "/api/jobs/review")
      .then(setQueue)
      .catch(() => setError("The sold estimates could not be loaded. Refresh the page."));
    api("GET", "/api/jobs")
      .then((d) => setJobs(d.jobs.filter((j) => j.revenue_method !== "pool")))
      .catch(() => setError("The jobs could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, reload]);

  const entry = queue && queue.entries.length > 0 ? queue.entries[index % queue.entries.length] : null;

  // A new estimate on screen: its own suggestion and name.
  useEffect(() => {
    if (!entry) return;
    setForm({ division_id: entry.division_suggestion_id || "", name: entry.name });
    setAttach({ jobId: "", role: "change_order", note: "" });
    setError(null);
  }, [entry && entry.estimate_id]);

  async function run(fn, after) {
    setBusy(true);
    setError(null);
    try {
      const job = await fn();
      after(job);
    } catch (err) {
      setError(err.detail || "The change could not be saved. Try again.");
    } finally {
      setBusy(false);
    }
  }

  function newJob(e) {
    e.preventDefault();
    run(
      () =>
        api("POST", "/api/jobs", {
          estimate_id: entry.estimate_id,
          division_id: form.division_id || null,
          name: form.name,
        }),
      () => setReload((n) => n + 1),
    );
  }

  function attachTo(e) {
    e.preventDefault();
    run(
      () =>
        api("POST", `/api/jobs/${attach.jobId}/estimates`, {
          estimate_id: entry.estimate_id,
          role: attach.role,
          note: attach.note || null,
        }),
      () => setReload((n) => n + 1),
    );
  }

  const suggested = entry ? entry.candidates : [];
  const others = jobs.filter((j) => !suggested.some((c) => c.job_id === j.id));

  return (
    <div>
      <p>
        <button type="button" className="link-button" onClick={onBack}>
          ← All jobs
        </button>
      </p>
      <h2>Review sold estimates</h2>
      {error && <p className="error">{error}</p>}
      {!queue ? (
        <p className="hint">Loading…</p>
      ) : !entry ? (
        <p>Every sold estimate is on a job. Nothing to review.</p>
      ) : (
        <>
          <p className="hint">
            Estimate {(index % queue.entries.length) + 1} of {queue.total}. One job per distinct scope the
            customer treats as its own contract; attach an estimate only when it adds to the scope of an existing job
            (D-03).
          </p>
          <dl className="kv">
            <dt>Estimate ID</dt>
            <dd>{entry.external_id}</dd>
            <dt>Estimator</dt>
            <dd>{entry.estimator || "Not given"}</dd>
            <dt>Client</dt>
            <dd>{entry.client_name || "Not given"}</dd>
            <dt>Jobsite</dt>
            <dd>{entry.jobsite || "Not given"}</dd>
            <dt>Name</dt>
            <dd>{entry.name}</dd>
            <dt>Price</dt>
            <dd className="num">{formatMoney(entry.price)}</dd>
            <dt>Estimate date</dt>
            <dd>{entry.estimate_date || "Not given"}</dd>
          </dl>
          <h3>Attention</h3>
          <Attention items={entry.attention} />

          <h3>Suggested jobs</h3>
          {suggested.length === 0 ? (
            <p className="hint">No existing job is suggested for this estimate.</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Why it is suggested</th>
                  </tr>
                </thead>
                <tbody>
                  {suggested.map((c) => (
                    <tr key={c.job_id}>
                      <td>
                        <button type="button" className="link-button" onClick={() => onOpenJob(c.job_id)}>
                          {c.job_name}
                        </button>
                      </td>
                      <td>{reasonText(c.reasons)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {canManage ? (
            <>
              <h3>Make it a new job</h3>
              <form onSubmit={newJob} className="inline-form">
                <label className="label">
                  Division
                  <select
                    className="input"
                    value={form.division_id}
                    onChange={(e) => setForm({ ...form, division_id: e.target.value })}
                    required
                    disabled={busy}
                  >
                    <option value="">Choose a division</option>
                    {queue.divisions.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.code}
                        {d.id === entry.division_suggestion_id ? " (suggested: most cost)" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="label">
                  Job name
                  <input
                    className="input"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    required
                    disabled={busy}
                  />
                </label>
                <button
                  type="submit"
                  className="button-primary"
                  disabled={busy || !form.division_id || !form.name.trim()}
                >
                  New job
                </button>
              </form>

              <h3>Or attach it to a job</h3>
              <form onSubmit={attachTo} className="inline-form">
                <label className="label">
                  Job
                  <select
                    className="input"
                    value={attach.jobId}
                    onChange={(e) => setAttach({ ...attach, jobId: e.target.value })}
                    disabled={busy}
                  >
                    <option value="">Choose a job</option>
                    {suggested.length > 0 && (
                      <optgroup label="Suggested">
                        {suggested.map((c) => (
                          <option key={c.job_id} value={c.job_id}>
                            {c.job_name}
                          </option>
                        ))}
                      </optgroup>
                    )}
                    {others.length > 0 && (
                      <optgroup label="Other jobs">
                        {others.map((j) => (
                          <option key={j.id} value={j.id}>
                            {j.name}
                          </option>
                        ))}
                      </optgroup>
                    )}
                  </select>
                </label>
                <label className="label">
                  Role
                  <select
                    className="input"
                    value={attach.role}
                    onChange={(e) => setAttach({ ...attach, role: e.target.value })}
                    disabled={busy}
                  >
                    {queue.roles.map((r) => (
                      <option key={r.value} value={r.value}>
                        {r.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="label">
                  Note{attach.role === "ignored" ? " (the reason, required)" : ""}
                  <input
                    className="input"
                    value={attach.note}
                    onChange={(e) => setAttach({ ...attach, note: e.target.value })}
                    disabled={busy}
                  />
                </label>
                <button type="submit" className="button" disabled={busy || !attachReady(attach)}>
                  Attach
                </button>
              </form>
            </>
          ) : (
            <p className="hint">Your role can read this list; a firm user or the company admin makes the jobs.</p>
          )}
          <p>
            <button
              type="button"
              className="link-button"
              disabled={busy}
              onClick={() => setIndex((i) => nextIndex(i % queue.entries.length, queue.entries.length))}
            >
              Skip to the next estimate
            </button>
          </p>
        </>
      )}
    </div>
  );
}
