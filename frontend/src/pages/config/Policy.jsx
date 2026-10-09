import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { decidedWords } from "../../config.js";
import { EditPolicy, PolicyRow } from "../../policyRow.js";

const LOAD_FAILED = "The policy settings could not be loaded. Refresh the page.";

// Accounting-policy keys (F04, F04.1): each shows its value or "Not decided", who decided,
// when, and the reference. No key has a default. Only a firm_admin sets one. A key whose
// feature has not arrived shows what it waits for in place of "Decide".
//
// Layout (F09.4; D-47, point 3): the keys are a list a person works through, one
// decision per row with its status in words and its one action, drawn with the
// checklist's pieces from Home; deciding a key opens its form in place.
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
      <div className="section-head">
        <p className="muted">
          These are the accounting decisions for this company. A key that has not been decided stays "Not decided";
          nothing is assumed in its place.
        </p>
        <span className="muted">{decidedWords(rows)}</span>
      </div>
      {error && <p className="error">{error}</p>}
      <ul className="checklist checklist-spaced">
        {rows.map((p) =>
          editing === p.key ? (
            <EditPolicy key={p.key} p={p} categories={categories} busy={busy} onCancel={() => setEditing(null)} onSave={save} />
          ) : (
            <PolicyRow key={p.key} p={p} categories={categories} canSetPolicy={canSetPolicy} busy={busy} onEdit={setEditing} />
          ),
        )}
      </ul>
    </div>
  );
}
