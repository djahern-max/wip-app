import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { EditPolicy, PolicyRow } from "../../policyRow.js";

const LOAD_FAILED = "The policy settings could not be loaded. Refresh the page.";

// Accounting-policy keys (F04, F04.1): each shows its value or "Not decided", who decided,
// when, and the reference. No key has a default. Only a firm_admin sets one. A key whose
// feature has not arrived shows what it waits for in place of "Decide".
export default function Policy({ me, canSetPolicy }) {
  const [rows, setRows] = useState(null);
  const [categories, setCategories] = useState([]);
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("GET", "/api/config/policy").then(setRows).catch(() => setError(LOAD_FAILED));
    api("GET", "/api/config/cost-categories").then(setCategories).catch(() => setError(LOAD_FAILED));
  }, [me.active_tenant_id]);

  async function save(key, value, decision_ref) {
    setBusy(true);
    setError(null);
    try {
      await api("PUT", `/api/config/policy/${key}`, { value, decision_ref });
      setRows(await api("GET", "/api/config/policy"));
      setEditing(null);
    } catch (err) {
      setError(err.detail || "The setting could not be saved. Try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!rows) return <p className="hint">{error || "Loading…"}</p>;
  return (
    <div>
      <p className="hint">
        These are the accounting decisions for this company. A key that has not been decided stays "Not decided";
        nothing is assumed in its place.
      </p>
      {error && <p className="error">{error}</p>}
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Setting</th>
              <th>Value</th>
              <th>Decided by</th>
              <th>When</th>
              <th>Reference</th>
              {canSetPolicy && <th></th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((p) =>
              editing === p.key ? (
                <EditPolicy key={p.key} p={p} categories={categories} busy={busy} onCancel={() => setEditing(null)} onSave={save} />
              ) : (
                <PolicyRow key={p.key} p={p} categories={categories} canSetPolicy={canSetPolicy} busy={busy} onEdit={setEditing} />
              ),
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
