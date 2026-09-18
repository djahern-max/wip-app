import { useEffect, useState } from "react";
import { api } from "../../api.js";

const LOAD_FAILED = "The divisions could not be loaded. Refresh the page.";

export default function Divisions({ me, canManage }) {
  const [rows, setRows] = useState(null);
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
  return (
    <div>
      <p className="hint">
        A division is a line of business. Its code digit is the first character of a cost code (D-23);
        a division without one has no cost codes.
      </p>
      {canManage && (
        <form onSubmit={add} className="inline-form">
          <label className="label">
            Code
            <input className="input" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} required disabled={busy} />
          </label>
          <label className="label">
            Name
            <input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required disabled={busy} />
          </label>
          <label className="label">
            Code digit
            <input className="input" value={form.code_digit} maxLength={1} onChange={(e) => setForm({ ...form, code_digit: e.target.value })} disabled={busy} />
          </label>
          <button type="submit" className="button-primary" disabled={busy || !form.code || !form.name}>
            Add division
          </button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
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
            {rows.length === 0 && (
              <tr>
                <td colSpan={canManage ? 5 : 4}>No divisions yet.</td>
              </tr>
            )}
            {rows.map((d) => (
              <DivisionRow key={d.id} d={d} canManage={canManage} busy={busy} run={run} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DivisionRow({ d, canManage, busy, run }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(d.name);
  const [digit, setDigit] = useState(d.code_digit || "");
  if (editing) {
    return (
      <tr>
        <td>{d.code}</td>
        <td>
          <label className="label">
            Name
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
          </label>
        </td>
        <td className="num">
          <label className="label">
            Code digit
            <input className="input" value={digit} maxLength={1} onChange={(e) => setDigit(e.target.value)} disabled={busy} />
          </label>
        </td>
        <td>{d.active ? "Active" : "Inactive"}</td>
        <td>
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
          </button>{" "}
          <button type="button" className="link-button" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </td>
      </tr>
    );
  }
  return (
    <tr>
      <td>{d.code}</td>
      <td>{d.name}</td>
      <td className="num">{d.code_digit || ""}</td>
      <td>{d.active ? "Active" : "Inactive"}</td>
      {canManage && (
        <td>
          <button type="button" className="link-button" disabled={busy} onClick={() => setEditing(true)}>
            Edit
          </button>
          {d.active && (
            <>
              {" · "}
              <button
                type="button"
                className="link-button"
                disabled={busy}
                onClick={() => run(() => api("POST", `/api/config/divisions/${d.id}/deactivate`))}
              >
                Deactivate
              </button>
            </>
          )}
        </td>
      )}
    </tr>
  );
}
