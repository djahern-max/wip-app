import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { formatMoney } from "../../money.js";

const LOAD_FAILED = "The policy settings could not be loaded. Refresh the page.";
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

// Accounting-policy keys (F04): each shows its value or "Not decided", who decided,
// when, and the reference. No key has a default. Only a firm_admin sets one.
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
                <tr key={p.key}>
                  <td>
                    {p.label}
                    <div className="hint">{p.description}</div>
                  </td>
                  <td className={p.kind === "money" ? "num" : ""}>{p.decided ? showValue(p, categories) : "Not decided"}</td>
                  <td>{p.decided_by_email || ""}</td>
                  <td>{p.decided_at ? new Date(p.decided_at).toLocaleDateString() : ""}</td>
                  <td className="wrap-anywhere">{p.decision_ref || ""}</td>
                  {canSetPolicy && (
                    <td>
                      <button type="button" className="link-button" disabled={busy} onClick={() => setEditing(p.key)}>
                        {p.decided ? "Change" : "Decide"}
                      </button>
                    </td>
                  )}
                </tr>
              ),
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function showValue(p, categories) {
  if (p.kind === "money") return formatMoney(p.value);
  if (p.kind === "month") return MONTHS[p.value - 1] || String(p.value);
  if (p.kind === "category_slots") {
    const names = categories.filter((c) => p.value.includes(c.slot)).map((c) => c.name);
    return names.length ? names.join(", ") : p.value.join(", ");
  }
  return String(p.value);
}

function EditPolicy({ p, categories, busy, onCancel, onSave }) {
  const [text, setText] = useState(p.decided && p.kind !== "category_slots" ? String(p.value) : "");
  const [slots, setSlots] = useState(p.decided && p.kind === "category_slots" ? p.value : []);
  const [ref, setRef] = useState("");

  function value() {
    if (p.kind === "month") return Number.isNaN(parseInt(text, 10)) ? text : parseInt(text, 10);
    if (p.kind === "category_slots") return slots;
    return text; // money stays a string; the server parses it as Decimal
  }

  return (
    <tr>
      <td>
        {p.label}
        <div className="hint">{p.description}</div>
      </td>
      <td>
        {p.kind === "month" && (
          <label className="label">
            Month
            <select className="input" value={text} onChange={(e) => setText(e.target.value)} disabled={busy}>
              <option value="">Choose…</option>
              {MONTHS.map((m, i) => (
                <option key={m} value={i + 1}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        )}
        {p.kind === "category_slots" && (
          <fieldset className="label">
            <legend>Cost categories in the WIP basis</legend>
            {categories.filter((c) => c.active).map((c) => (
              <label key={c.id} className="small">
                <input
                  type="checkbox"
                  checked={slots.includes(c.slot)}
                  disabled={busy}
                  onChange={(e) => setSlots(e.target.checked ? [...slots, c.slot] : slots.filter((s) => s !== c.slot))}
                />{" "}
                {c.slot} {c.name}
                <br />
              </label>
            ))}
          </fieldset>
        )}
        {(p.kind === "money" || p.kind === "text" || p.kind === "timezone") && (
          <label className="label">
            {p.kind === "money" ? "Amount" : p.kind === "timezone" ? "Time zone (e.g. America/New_York)" : "Value"}
            <input className="input" inputMode={p.kind === "money" ? "decimal" : "text"} value={text} onChange={(e) => setText(e.target.value)} disabled={busy} />
          </label>
        )}
      </td>
      <td colSpan={3}>
        <label className="label">
          Decision reference (who decided, where it is written down)
          <input className="input" value={ref} onChange={(e) => setRef(e.target.value)} required disabled={busy} />
        </label>
      </td>
      <td>
        <button type="button" className="button-primary" disabled={busy || !ref.trim()} onClick={() => onSave(p.key, value(), ref)}>
          Record decision
        </button>{" "}
        <button type="button" className="link-button" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </td>
    </tr>
  );
}
