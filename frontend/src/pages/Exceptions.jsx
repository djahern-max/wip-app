import { useEffect, useState } from "react";
import { api } from "../api.js";
import { blockEnter } from "../jobs.js";
import { dayOf, eventLine, filterQuery, noteReady, pendingLabel, rowActions } from "../exceptions.js";

// Exceptions (F09; D-22, D-46): one table of the company's exceptions, open first, then by
// severity and age, with the sentence, what it is about, severity in words, first raised,
// assigned to and status. Filters: status, severity, assigned to me. A row opens to its
// history and, for the roles that manage jobs (can_manage from the API), the Assign,
// Dismiss and Reopen controls; everyone adds notes. Mount effects only read (GET); every
// write is a button, and Enter in the note field submits nothing (blockEnter). Buttons show
// a busy label while a request is in flight. The table scrolls inside .table-wrap with the
// first column held; the page never scrolls sideways.

const STATUSES = [
  ["", "All statuses"],
  ["open", "Open"],
  ["dismissed", "Dismissed"],
  ["resolved", "Resolved"],
];
const SEVERITIES = [
  ["", "All severities"],
  ["block_close", "Blocks period close"],
  ["warn", "Needs attention"],
  ["info", "For information"],
];

export default function Exceptions({ me, onOpenJob, onOpenEstimate, onOpenCustomers }) {
  const [data, setData] = useState(null);
  const [filters, setFilters] = useState({ status: "", severity: "", mine: false });
  const [openId, setOpenId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [note, setNote] = useState("");
  const [assignee, setAssignee] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null);
  const [error, setError] = useState(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api("GET", `/api/exceptions${filterQuery(filters)}`)
      .then(setData)
      .catch(() => setError("The exceptions could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, filters, reload]);

  useEffect(() => {
    if (!openId) {
      setDetail(null);
      return;
    }
    api("GET", `/api/exceptions/${openId}`)
      .then((d) => {
        setDetail(d);
        setAssignee(d.assigned_to_id || "");
      })
      .catch(() => setError("The exception could not be loaded. Refresh the page."));
  }, [me.active_tenant_id, openId, reload]);

  function open(id) {
    setError(null);
    setNote("");
    setOpenId(openId === id ? null : id);
  }

  function openSubject(subject) {
    if (subject.type === "job") onOpenJob(subject.id);
    else if (subject.type === "estimate") onOpenEstimate(subject.id);
    else onOpenCustomers();
  }

  async function act(action, id, body) {
    setError(null);
    setBusy(true);
    setPending({ action, id });
    try {
      const updated = await api("POST", `/api/exceptions/${id}/${action}`, body);
      setDetail(updated);
      setAssignee(updated.assigned_to_id || "");
      if (action !== "assign") setNote("");
      setReload((n) => n + 1);
    } catch (e) {
      setError(e.detail || `The request did not go through (${action}). Try again.`);
    } finally {
      setBusy(false);
      setPending(null);
    }
  }

  const canManage = Boolean(data && data.can_manage);

  return (
    <div>
      <h2>Exceptions</h2>
      {error && <p className="error">{error}</p>}
      {data && (
        <p className="hint">
          {data.tenant_name}.{" "}
          {data.as_of
            ? `As of the last run on ${dayOf(data.as_of)}.`
            : "No run has happened yet; the queue fills after the next sync or import."}{" "}
          Open: {data.counts.block_close} block the period close, {data.counts.warn} need attention,{" "}
          {data.counts.info} for information. A sentence stays open until its cause is gone, or a person
          dismisses it with a note (D-46); one that blocks the period close cannot be dismissed.
        </p>
      )}
      <div className="actions">
        <label className="small">
          Status{" "}
          <select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}>
            {STATUSES.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="small">
          Severity{" "}
          <select value={filters.severity} onChange={(e) => setFilters({ ...filters, severity: e.target.value })}>
            {SEVERITIES.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="small">
          <input
            type="checkbox"
            checked={filters.mine}
            onChange={(e) => setFilters({ ...filters, mine: e.target.checked })}
          />{" "}
          Assigned to me
        </label>
      </div>
      {!data ? (
        <p className="hint">{error ? "" : "Loading…"}</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Sentence</th>
                <th>About</th>
                <th>Severity</th>
                <th>First raised</th>
                <th>Assigned to</th>
                <th>Status</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {data.exceptions.length === 0 && (
                <tr>
                  <td colSpan={7}>No exception matches these filters.</td>
                </tr>
              )}
              {data.exceptions.map((e) => (
                <ExceptionRow
                  key={e.id}
                  e={e}
                  isOpen={openId === e.id}
                  detail={openId === e.id ? detail : null}
                  members={data.members}
                  canManage={canManage}
                  busy={busy}
                  pending={pending}
                  note={note}
                  setNote={setNote}
                  assignee={assignee}
                  setAssignee={setAssignee}
                  onOpen={() => open(e.id)}
                  onSubject={() => openSubject(e.subject)}
                  onAct={act}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ExceptionRow({
  e,
  isOpen,
  detail,
  members,
  canManage,
  busy,
  pending,
  note,
  setNote,
  assignee,
  setAssignee,
  onOpen,
  onSubject,
  onAct,
}) {
  const actions = rowActions(e, canManage);
  return (
    <>
      <tr>
        <td>{e.message}</td>
        <td>
          <button type="button" className="link-button" onClick={onSubject}>
            {e.subject.label || e.subject.type}
          </button>
        </td>
        <td>{e.severity_label}</td>
        <td>{dayOf(e.first_raised_at)}</td>
        <td>{e.assigned_to || "Nobody"}</td>
        <td>{e.status_label}</td>
        <td>
          <button type="button" className="link-button" onClick={onOpen}>
            {isOpen ? "Close" : "Open"}
          </button>
        </td>
      </tr>
      {isOpen && (
        <tr>
          <td colSpan={7}>
            {!detail ? (
              <p className="hint">Loading…</p>
            ) : (
              <div className="request-form">
                <h4>History</h4>
                <ul className="issues">
                  {detail.events.map((ev) => (
                    <li key={ev.id}>{eventLine(ev)}</li>
                  ))}
                </ul>
                <form onSubmit={(ev) => ev.preventDefault()} onKeyDown={blockEnter}>
                  <label className="label">
                    Note{" "}
                    <input
                      className="input"
                      type="text"
                      value={note}
                      maxLength={2000}
                      onChange={(ev) => setNote(ev.target.value)}
                      disabled={busy}
                    />
                  </label>{" "}
                  <button
                    type="button"
                    className="button"
                    disabled={busy || !noteReady(note)}
                    onClick={() => onAct("notes", e.id, { text: note })}
                  >
                    {pendingLabel("note", pending, e.id) || "Add note"}
                  </button>
                  {actions.some((a) => a.action === "dismiss") && (
                    <>
                      {" "}
                      <button
                        type="button"
                        className="button"
                        disabled={busy || !noteReady(note)}
                        onClick={() => onAct("dismiss", e.id, { note })}
                      >
                        {pendingLabel("dismiss", pending, e.id) || "Dismiss with this note"}
                      </button>
                    </>
                  )}
                  {actions.some((a) => a.action === "reopen") && (
                    <>
                      {" "}
                      <button type="button" className="button" disabled={busy} onClick={() => onAct("reopen", e.id)}>
                        {pendingLabel("reopen", pending, e.id) || "Reopen"}
                      </button>
                    </>
                  )}
                  {actions.some((a) => a.action === "assign") && (
                    <p>
                      <label className="label">
                        Assign to{" "}
                        <select value={assignee} onChange={(ev) => setAssignee(ev.target.value)} disabled={busy}>
                          <option value="">Nobody</option>
                          {members.map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.name}
                            </option>
                          ))}
                        </select>
                      </label>{" "}
                      <button
                        type="button"
                        className="button"
                        disabled={busy || (assignee || "") === (detail.assigned_to_id || "")}
                        onClick={() => onAct("assign", e.id, { user_id: assignee || null })}
                      >
                        {pendingLabel("assign", pending, e.id) || "Assign"}
                      </button>
                    </p>
                  )}
                  {e.status === "open" && !e.may_dismiss && (
                    <p className="hint">This exception blocks the period close and cannot be dismissed (D-46).</p>
                  )}
                </form>
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
