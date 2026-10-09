import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { emptyMessage, inactiveCount, visibleRows } from "../../inactive.js";
import ShowInactive from "./ShowInactive.jsx";

const LOAD_FAILED = "The divisions could not be loaded. Refresh the page.";

// Layout (F09.4): one sentence, the add form as one card with its fields on a line, the
// list in a table no wider than it needs; a row being edited is tinted; an inactive row
// is muted and still says "Inactive".
export default function Divisions({ me, canManage }) {
  const [rows, setRows] = useState(null);
  const [showInactive, setShowInactive] = useState(false); // F06.1: page state only
  const [form, setForm] = useState({ code: "", name: "", code_digit: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("GET", "/api/config/divisions").then(setRows).catch(() => setError(LOAD_FAILED));
  }, [me.active_tenant_id]);

  async function run(fn) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setRows(await api("GET", "/api/config/divisions"));
    } catch (err) {
      setError(err.detail || "The change could not be saved. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function add(e) {
    e.preventDefault();
    await run(() =>
      api("POST", "/api/config/divisions", {
        code: form.code,
        name: form.name,
        code_digit: form.code_digit || null,
        sort_order: rows ? rows.length : 0,
      }),
    );
    setForm({ code: "", name: "", code_digit: "" });
  }

  if (!rows) return <p className="hint">{error || "Loading…"}</p>;
  const shown = visibleRows(rows, showInactive);
  return (
    <div className="config-narrow">
      <p className="lead">
        A division is a line of business. Its code digit is the first character of a cost code; a division
        without one has no cost codes.
      </p>
      {canManage && (
        <form onSubmit={add} className="form-card">
          <label className="field">
            <span className="field-label">Code</span>
            <input className="control control-short" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} required disabled={busy} />
          </label>
          <label className="field field-grow">
            <span className="field-label">Name</span>
            <input className="control control-wide" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required disabled={busy} />
          </label>
          <label className="field">
            <span className="field-label">Code digit</span>
            <input className="control control-short" value={form.code_digit} maxLength={1} onChange={(e) => setForm({ ...form, code_digit: e.target.value })} disabled={busy} />
          </label>
          <button type="submit" className="button-primary" disabled={busy || !form.code || !form.name}>
            Add division
          </button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
      <ShowInactive
        count={inactiveCount(rows)}
        showing={showInactive}
        onToggle={() => setShowInactive((v) => !v)}
      />
      {shown.length === 0 ? (
        <div className="empty">
          <strong>{emptyMessage(rows, "divisions")}</strong>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Code</th>
                <th>Name</th>
                <th className="num">Code digit</th>
                <th>Status</th>
                {canManage && <th></th>}
              </tr>
            </thead>
            <tbody>
              {shown.map((d) => (
                <DivisionRow key={d.id} d={d} canManage={canManage} busy={busy} run={run} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function DivisionRow({ d, canManage, busy, run }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(d.name);
  const [digit, setDigit] = useState(d.code_digit || "");
  if (editing) {
    return (
      <tr className="row-editing">
        <td>
          <strong>{d.code}</strong>
        </td>
        <td>
          <input className="control control-wide" aria-label="Name" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
        </td>
        <td className="num">
          <input className="control control-short" aria-label="Code digit" value={digit} maxLength={1} onChange={(e) => setDigit(e.target.value)} disabled={busy} />
        </td>
        <td>{d.active ? "Active" : "Inactive"}</td>
        <td>
          <span className="actions actions-row">
            <button
              type="button"
              className="button"
              disabled={busy}
              onClick={() =>
                run(() =>
                  api("PUT", `/api/config/divisions/${d.id}`, {
                    name,
                    code_digit: digit || null,
                    clear_digit: digit === "",
                  }),
                ).then(() => setEditing(false))
              }
            >
              Save
            </button>
            <button type="button" className="link-button" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </span>
        </td>
      </tr>
    );
  }
  return (
    <tr className={d.active ? undefined : "row-off"}>
      <td>
        <strong>{d.code}</strong>
      </td>
      <td>{d.name}</td>
      <td className="num">{d.code_digit || <span className="muted">none</span>}</td>
      <td>{d.active ? "Active" : "Inactive"}</td>
      {canManage && (
        <td>
          <span className="actions">
            <button type="button" className="link-button" disabled={busy} onClick={() => setEditing(true)}>
              Edit
            </button>
            {d.active && (
              <button
                type="button"
                className="link-button"
                disabled={busy}
                onClick={() => run(() => api("POST", `/api/config/divisions/${d.id}/deactivate`))}
              >
                Deactivate
              </button>
            )}
          </span>
        </td>
      )}
    </tr>
  );
}
