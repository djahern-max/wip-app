import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { formatRate } from "../../rates.js";

const LOAD_FAILED = "The burden rates could not be loaded. Refresh the page.";

export default function BurdenRates({ me, canManage }) {
  const [rows, setRows] = useState(null);
  const [divisions, setDivisions] = useState([]);
  const [form, setForm] = useState({ division_id: "", effective_from: "", effective_to: "", rate: "", basis_note: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("GET", "/api/config/burden-rates").then(setRows).catch(() => setError(LOAD_FAILED));
    api("GET", "/api/config/divisions").then(setDivisions).catch(() => setError(LOAD_FAILED));
  }, [me.active_tenant_id]);

  async function run(fn) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setRows(await api("GET", "/api/config/burden-rates"));
      return true;
    } catch (err) {
      setError(err.detail || "The change could not be saved. Try again.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function add(e) {
    e.preventDefault();
    const ok = await run(() =>
      api("POST", "/api/config/burden-rates", {
        division_id: form.division_id || null,
        effective_from: form.effective_from,
        effective_to: form.effective_to || null,
        rate: form.rate, // a string: "0.3250"; never a number
        basis_note: form.basis_note || null,
      }),
    );
    if (ok) setForm({ division_id: "", effective_from: "", effective_to: "", rate: "", basis_note: "" });
  }

  if (!rows) return <p className="hint">{error || "Loading…"}</p>;
  return (
    <div>
      <p className="hint">
        A burden rate is a fraction of labor cost (0.3250 = 32.50%), in force from a date, for one division or
        for the whole company. Periods for the same division may not overlap.
      </p>
      {canManage && (
        <form onSubmit={add} className="inline-form">
          <label className="label">
            Division
            <select className="input" value={form.division_id} onChange={(e) => setForm({ ...form, division_id: e.target.value })} disabled={busy}>
              <option value="">Whole company</option>
              {divisions.filter((d) => d.active).map((d) => (
                <option key={d.id} value={d.id}>
                  {d.code}
                </option>
              ))}
            </select>
          </label>
          <label className="label">
            From
            <input className="input" type="date" value={form.effective_from} onChange={(e) => setForm({ ...form, effective_from: e.target.value })} required disabled={busy} />
          </label>
          <label className="label">
            To (exclusive)
            <input className="input" type="date" value={form.effective_to} onChange={(e) => setForm({ ...form, effective_to: e.target.value })} disabled={busy} />
          </label>
          <label className="label">
            Rate (fraction)
            <input className="input" inputMode="decimal" placeholder="0.3250" value={form.rate} onChange={(e) => setForm({ ...form, rate: e.target.value })} required disabled={busy} />
          </label>
          <label className="label">
            Basis
            <input className="input" value={form.basis_note} onChange={(e) => setForm({ ...form, basis_note: e.target.value })} disabled={busy} />
          </label>
          <button type="submit" className="button-primary" disabled={busy || !form.effective_from || !form.rate}>
            Add burden rate
          </button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Division</th>
              <th>From</th>
              <th>To</th>
              <th className="num">Rate</th>
              <th>Basis</th>
              <th>Status</th>
              {canManage && <th></th>}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={canManage ? 7 : 6}>No burden rates yet.</td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.division_code || "Whole company"}</td>
                <td>{r.effective_from}</td>
                <td>{r.effective_to || "open"}</td>
                <td className="num">{formatRate(r.rate)}</td>
                <td>{r.basis_note || ""}</td>
                <td>{r.active ? "Active" : "Inactive"}</td>
                {canManage && (
                  <td>
                    {r.active && (
                      <button
                        type="button"
                        className="link-button"
                        disabled={busy}
                        onClick={() => run(() => api("POST", `/api/config/burden-rates/${r.id}/deactivate`))}
                      >
                        Deactivate
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
