import { useEffect, useState } from "react";
import { api } from "../../api.js";

const LOAD_FAILED = "The accounts could not be loaded. Refresh the page.";

// The account map (F04). Unmapped accounts are always counted at the top. The one
// primary action is "Confirm all suggestions", with a confirmation step. The table
// scrolls inside its own container with the account column held in place
// (.table-wrap, first column sticky) so the page never scrolls sideways.
export default function Accounts({ me, canManage }) {
  const [data, setData] = useState(null);
  const [filter, setFilter] = useState("all");
  const [lists, setLists] = useState({ divisions: [], categories: [] });
  const [editing, setEditing] = useState(null); // account id
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  function load(f = filter) {
    return api("GET", `/api/config/accounts?filter=${f}`)
      .then(setData)
      .catch(() => setError(LOAD_FAILED));
  }

  useEffect(() => {
    api("GET", `/api/config/accounts?filter=${filter}`)
      .then(setData)
      .catch(() => setError(LOAD_FAILED));
  }, [me.active_tenant_id, filter]);

  useEffect(() => {
    Promise.all([api("GET", "/api/config/divisions"), api("GET", "/api/config/cost-categories")])
      .then(([divisions, categories]) => setLists({ divisions, categories }))
      .catch(() => setError(LOAD_FAILED));
  }, [me.active_tenant_id]);

  async function act(fn, done) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const r = await fn();
      if (done) done(r);
      setData(r);
    } catch (err) {
      setError(err.detail || "The change could not be saved. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function confirmAll() {
    setBusy(true);
    setError(null);
    try {
      const r = await api("POST", "/api/config/accounts/confirm-all");
      setNotice(
        r.confirmed === 0
          ? "There were no suggestions to confirm."
          : `${r.confirmed} suggestions confirmed. ${r.unmapped_count} accounts remain unmapped.`,
      );
      setConfirming(false);
      await load();
    } catch (err) {
      setError(err.detail || "The suggestions could not be confirmed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <p className="hint">{error || "Loading…"}</p>;
  const suggested = data.suggested_count;

  return (
    <div>
      <p className="counts">
        <strong>{data.unmapped_count}</strong> of <strong>{data.total_active}</strong> active accounts are
        unmapped ({data.suggested_count} suggested, {data.confirmed_count} confirmed).
      </p>
      <div className="toolbar">
        <label>
          Show{" "}
          <select value={filter} onChange={(e) => setFilter(e.target.value)} disabled={busy}>
            <option value="all">All accounts</option>
            <option value="unmapped">Unmapped</option>
            <option value="suggested">Suggested</option>
            <option value="confirmed">Confirmed</option>
          </select>
        </label>
        {canManage && !confirming && (
          <button
            type="button"
            className="button-primary"
            disabled={busy || suggested === 0}
            onClick={() => setConfirming(true)}
          >
            Confirm all suggestions ({suggested})
          </button>
        )}
        {canManage && (
          <button
            type="button"
            className="link-button"
            disabled={busy}
            onClick={() => act(() => api("POST", "/api/config/accounts/suggest"))}
          >
            Re-run suggestions
          </button>
        )}
      </div>
      {confirming && (
        <div className="confirm-step">
          This confirms {suggested} suggested mappings as they stand. Confirmed mappings are what
          every report will use.{" "}
          <button type="button" className="button" disabled={busy} onClick={confirmAll}>
            Yes, confirm {suggested} suggestions
          </button>{" "}
          <button type="button" className="link-button" onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </div>
      )}
      {notice && <p className="hint">{notice}</p>}
      {error && <p className="error">{error}</p>}
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Account</th>
              <th>Type</th>
              <th>Division</th>
              <th>Cost category</th>
              <th>In job cost</th>
              <th className="num">Cost code</th>
              <th>Status</th>
              {canManage && <th></th>}
            </tr>
          </thead>
          <tbody>
            {data.accounts.length === 0 && (
              <tr>
                <td colSpan={canManage ? 8 : 7}>No accounts to show.</td>
              </tr>
            )}
            {data.accounts.map((a) =>
              editing === a.id ? (
                <EditRow
                  key={a.id}
                  account={a}
                  lists={lists}
                  busy={busy}
                  onCancel={() => setEditing(null)}
                  onSave={(body) =>
                    act(
                      () => api("PUT", `/api/config/accounts/${a.id}/map`, body),
                      () => setEditing(null),
                    )
                  }
                />
              ) : (
                <tr key={a.id}>
                  <td className="wrap-anywhere">
                    <span className="grid-code">{a.account_no}</span> {a.name}
                  </td>
                  <td>{a.ledger_type}</td>
                  <td>{a.map && a.map.division_code ? a.map.division_code : ""}</td>
                  <td>{a.map && a.map.cost_category_name ? a.map.cost_category_name : ""}</td>
                  <td>{a.map ? (a.map.in_job_cost ? "Yes" : "No") : ""}</td>
                  <td className="num">{a.cost_code || ""}</td>
                  <td>
                    {a.map_status_label}
                    {a.map && a.map.suggested_by_rule && a.map.status === "suggested" && (
                      <div className="hint">rule: {a.map.suggested_by_rule}</div>
                    )}
                    {a.map && a.map.status === "confirmed" && a.map.confirmed_by_email && (
                      <div className="hint">by {a.map.confirmed_by_email}</div>
                    )}
                  </td>
                  {canManage && (
                    <td>
                      <button type="button" className="link-button" disabled={busy} onClick={() => setEditing(a.id)}>
                        Edit
                      </button>
                      {a.map && a.map.status === "suggested" && (
                        <>
                          {" · "}
                          <button
                            type="button"
                            className="link-button"
                            disabled={busy}
                            onClick={() => act(() => api("POST", `/api/config/accounts/${a.id}/confirm`))}
                          >
                            Confirm
                          </button>
                        </>
                      )}
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

function EditRow({ account, lists, busy, onCancel, onSave }) {
  const m = account.map || {};
  const [division, setDivision] = useState(m.division_id || "");
  const [category, setCategory] = useState(m.cost_category_id || "");
  const [inJobCost, setInJobCost] = useState(Boolean(m.in_job_cost));
  const body = (confirm) => ({
    division_id: division || null,
    cost_category_id: category || null,
    in_job_cost: inJobCost,
    confirm,
  });
  return (
    <tr>
      <td className="wrap-anywhere">
        <span className="grid-code">{account.account_no}</span> {account.name}
      </td>
      <td>{account.ledger_type}</td>
      <td>
        <label className="label">
          Division
          <select className="input" value={division} onChange={(e) => setDivision(e.target.value)} disabled={busy}>
            <option value="">none</option>
            {lists.divisions
              .filter((d) => d.active)
              .map((d) => (
                <option key={d.id} value={d.id}>
                  {d.code} · {d.name}
                </option>
              ))}
          </select>
        </label>
      </td>
      <td>
        <label className="label">
          Cost category
          <select className="input" value={category} onChange={(e) => setCategory(e.target.value)} disabled={busy}>
            <option value="">none</option>
            {lists.categories
              .filter((c) => c.active)
              .map((c) => (
                <option key={c.id} value={c.id}>
                  {c.slot} · {c.name}
                </option>
              ))}
          </select>
        </label>
      </td>
      <td>
        <label className="label">
          <input type="checkbox" checked={inJobCost} onChange={(e) => setInJobCost(e.target.checked)} disabled={busy} />{" "}
          In job cost
        </label>
      </td>
      <td className="num"></td>
      <td>{account.map_status_label}</td>
      <td>
        <button type="button" className="button" disabled={busy} onClick={() => onSave(body(false))}>
          Save
        </button>{" "}
        <button type="button" className="button" disabled={busy} onClick={() => onSave(body(true))}>
          Save and confirm
        </button>{" "}
        <button type="button" className="link-button" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </td>
    </tr>
  );
}
