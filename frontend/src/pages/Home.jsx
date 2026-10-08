import { useEffect, useState } from "react";
import { api } from "../api.js";
import { doneWord, jobsSentence, linkLabel, markerText, progressWords } from "../home.js";

// Home (F07.3): what to do next, computed on read by GET /api/home. The set-up
// checklist (done or not, one sentence, one link to the action that completes it) is
// sent only to roles that can view tenant configuration; the jobs part to every role,
// with links only to pages the role can open. The API marks at most one line primary;
// the page draws it as the one primary action. Mount effects only read (GET).

export default function Home({ me, companyName, onOpen }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setData(null);
    setError(null);
    api("GET", "/api/home")
      .then(setData)
      .catch(() => setError("Home could not be loaded. Refresh the page."));
  }, [me.active_tenant_id]);

  function action(link, primary) {
    if (!link) return null;
    return (
      <button
        type="button"
        className={primary ? "button button-primary" : "link-button"}
        onClick={() => onOpen(link)}
      >
        {linkLabel(link)}
      </button>
    );
  }

  return (
    <div>
      <h1>{companyName}</h1>
      {error && <p className="error">{error}</p>}
      {!data ? (
        <p className="hint">{error ? "" : "Loading…"}</p>
      ) : (
        <>
          {data.setup && (
            <>
              <div className="section-head">
                <h2>Set-up</h2>
                <span className="muted">{progressWords(data.setup)}</span>
              </div>
              <div className="progress" role="img" aria-label={progressWords(data.setup)}>
                {data.setup.map((line) => (
                  <span key={line.code} className={line.done ? "progress-step progress-done" : "progress-step"} />
                ))}
              </div>
              <ol className="checklist">
                {data.setup.map((line, n) => (
                  <li key={line.code} className="step">
                    <span
                      className={
                        line.done
                          ? "step-marker step-marker-done"
                          : line.primary
                            ? "step-marker step-marker-next"
                            : "step-marker"
                      }
                      aria-hidden="true"
                    >
                      {markerText(line, n)}
                    </span>
                    <div className="step-body">
                      <div className="step-title">{line.label}</div>
                      <div className="step-message">
                        {line.message}
                        {line.note && <span> {line.note}</span>}
                      </div>
                    </div>
                    <span className={line.done ? "step-status step-status-done" : "step-status"}>
                      {doneWord(line.done)}
                    </span>
                    <div className="step-action">{action(line.link, line.primary)}</div>
                  </li>
                ))}
              </ol>
            </>
          )}

          <h2>Jobs</h2>
          {data.review && (
            <p>
              {data.review.message} {action(data.review.link, false)}
            </p>
          )}
          <p>{jobsSentence(data.jobs)}</p>
          {data.jobs.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Status</th>
                    <th>Needs next</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {data.jobs.map((j) => (
                    <tr key={j.id}>
                      <td>
                        <button type="button" className="link-button" onClick={() => onOpen({ page: "jobs", job_id: j.id })}>
                          {j.name}
                        </button>
                      </td>
                      <td>{j.status_label}</td>
                      <td>{j.message}</td>
                      <td>{action(j.link, false)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {data.tracked_without_job.length > 0 && (
            <>
              <h3>Tracked QuickBooks rows with no job</h3>
              <ul className="issues">
                {data.tracked_without_job.map((i, n) => (
                  <li key={n}>
                    {i.message} {action(i.link, false)}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </div>
  );
}
