import { useEffect, useState } from "react";
import { api } from "../../api.js";

const LOAD_FAILED = "The cost categories could not be loaded. Refresh the page.";

// The D-23 list, seeded per company. Categories are renamed or deactivated, never
// deleted; adding one needs a decision, so there is no add form.
export default function CostCategories({ me, canManage }) {
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("GET", "/api/config/cost-categories").then(setRows).catch(() => setError(LOAD_FAILED));
  }, [me.active_tenant_id]);

  async function run(fn) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setRows(await api("GET", "/api/config/cost-categories"));
    } catch (err) {
      setError(err.detail || "The change could not be saved. Try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!rows) return <p className="hint">{error || "Loading…"}</p>;
  return (
    <div>
      <p className="hint">
        The slot is the last two digits of a cost code. The list is fixed by decision D-23; a category
        can be renamed or deactivated here.
      </p>
      {error && <p className="error">{error}</p>}
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th className="num">Slot</th>
              <th>Name</th>
              <th>Status</th>
              {canManage && <th></th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <CategoryRow key={c.id} c={c} canManage={canManage} busy={busy} run={run} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CategoryRow({ c, canManage, busy, run }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(c.name);
  return (
    <tr>
      <td className="num">{c.slot}</td>
      <td>
        {editing ? (
          <label className="label">
            Name
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
          </label>
        ) : (
          c.name
        )}
      </td>
      <td>{c.active ? "Active" : "Inactive"}</td>
      {canManage && (
        <td>
          {editing ? (
            <>
              <button
                type="button"
                className="button"
                disabled={busy}
                onClick={() => run(() => api("PUT", `/api/config/cost-categories/${c.id}`, { name })).then(() => setEditing(false))}
              >
                Save
              </button>{" "}
              <button type="button" className="link-button" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button type="button" className="link-button" disabled={busy} onClick={() => setEditing(true)}>
                Rename
              </button>
              {c.active && (
                <>
                  {" · "}
                  <button
                    type="button"
                    className="link-button"
                    disabled={busy}
                    onClick={() => run(() => api("POST", `/api/config/cost-categories/${c.id}/deactivate`))}
                  >
                    Deactivate
                  </button>
                </>
              )}
            </>
          )}
        </td>
      )}
    </tr>
  );
}
