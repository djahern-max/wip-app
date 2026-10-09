import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { allConfirmed, confirmedSentence, suggestedNote } from "../../config.js";
import { visibleRows } from "../../inactive.js";
import ShowInactive from "./ShowInactive.jsx";

const LOAD_FAILED = "The accounts could not be loaded. Refresh the page.";

// The account map (F04). Unmapped accounts are always counted at the top. The one
// primary action is "Confirm all suggestions", with a confirmation step. The table
// scrolls inside its own container with the account column held in place
// (.table-wrap, first column sticky) so the page never scrolls sideways.
//
// Layout (F09.4): one sentence and the actions; the API's counts as figures ("Unmapped"
// is every active account without a confirmed mapping, so the suggested ones are among
// them and are named under it); "Done." when none is left; the Show filter with its
// label above it; the ledger type under the account; a row being edited opens its form
// across the row. "Confirm all suggestions" is drawn only when there is a suggestion.
export default function Accounts({ me, canManage }) {
  const [data, setData] = useState(null);
  const [filter, setFilter] = useState("all");
  const [showInactive, setShowInactive] = useState(false); // F06.1: page state only
  const [lists, setLists] = useState({ divisions: [], categories: [] });
  const [editing, setEditing] = useState(null); // account id
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  function load(f = filter) {
    return api("GET", `/api/config/accounts?filter=${f}&include_inactive=true`)
      .then(setData)
      .catch(() => setError(LOAD_FAILED));
  }

  useEffect(() => {
    api("GET", `/api/config/accounts?filter=${filter}&include_inactive=true`)
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
  const shown = visibleRows(data.accounts, showInactive);

  return (
    <div>
      <div className="page-head">
        <p className="muted">
          Each ledger account is given a division and a cost category. Confirmed mappings are what every
          report will use.
        </p>
        {canManage && (
          <div className="actions actions-row">
            <button
              type="button"
              className="link-button"
              disabled={busy}
              onClick={() => act(() => api("POST", "/api/config/accounts/suggest"))}
            >
              Re-run suggestions
            </button>
            {!confirming && suggested > 0 && (
              <button type="button" className="button-primary" disabled={busy} onClick={() => setConfirming(true)}>
                Confirm all suggestions ({suggested})
              </button>
            )}
          </div>
        )}
      </div>
      {allConfirmed(data) && (
        <p className="done-line">
          <span className="step-marker step-marker-done" aria-hidden="true">
            ✓
          </span>
          <span>
            <span className="step-status step-status-done">Done.</span> {confirmedSentence(data.total_active)}
          </span>
        </p>
      )}
      <dl className="facts facts-row">
        <div>
          <dt>Active accounts</dt>
          <dd className="fact-figure">{data.total_active}</dd>
        </div>
        <div>
          <dt>Unmapped</dt>
          <dd className="fact-figure">{data.unmapped_count}</dd>
          <dd className="cell-sub">{suggestedNote(suggested)}</dd>
        </div>
        <div>
          <dt>Confirmed</dt>
          <dd className="fact-figure">{data.confirmed_count}</dd>
        </div>
      </dl>
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
      <div className="filters">
        <label className="field">
          <span className="field-label">Show</span>
          <select className="select" value={filter} onChange={(e) => setFilter(e.target.value)} disabled={busy}>
            <option value="all">All accounts</option>
            <option value="unmapped">Unmapped</option>
            <option value="suggested">Suggested</option>
            <option value="confirmed">Confirmed</option>
          </select>
        </label>
        <ShowInactive
          count={data.inactive_count}
          showing={showInactive}
          onToggle={() => setShowInactive((v) => !v)}
        />
      </div>
      {shown.length === 0 ? (
        <div className="empty">
          <strong>No accounts to show.</strong>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Account</th>
                <th>Division</th>
                <th>Cost category</th>
                <th>In job cost</th>
                <th className="num">Cost code</th>
                <th>Status</th>
                {canManage && <th></th>}
              </tr>
            </thead>
            <tbody>
              {shown.map((a) =>
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
                  <tr key={a.id} className={a.active ? undefined : "row-off"}>
                    <td>
                      <span className="grid-code">{a.account_no}</span> {a.name}
                      <div className="cell-sub">{a.ledger_type}</div>
                    </td>
                    <td>{a.map && a.map.division_code ? a.map.division_code : ""}</td>
                    <td>{a.map && a.map.cost_category_name ? a.map.cost_category_name : ""}</td>
                    <td>{a.map ? a.map.in_job_cost ? "Yes" : <span className="muted">No</span> : ""}</td>
                    <td className="num">{a.cost_code || ""}</td>
                    <td>
                      {a.map && a.map.status === "confirmed" ? (
                        statusWords(a)
                      ) : (
                        <strong>{statusWords(a)}</strong>
                      )}
                      {a.map && a.map.suggested_by_rule && a.map.status === "suggested" && (
                        <div className="cell-sub">rule: {a.map.suggested_by_rule}</div>
                      )}
                      {a.map && a.map.status === "confirmed" && a.map.confirmed_by_email && (
                        <div className="cell-sub">by {a.map.confirmed_by_email}</div>
                      )}
                    </td>
                    {canManage && (
                      <td>
                        <span className="actions">
                          <button type="button" className="link-button" disabled={busy} onClick={() => setEditing(a.id)}>
                            Edit
                          </button>
                          {a.map && a.map.status === "suggested" && (
                            <button
                              type="button"
                              className="link-button"
                              disabled={busy}
                              onClick={() => act(() => api("POST", `/api/config/accounts/${a.id}/confirm`))}
                            >
                              Confirm
                            </button>
                          )}
                        </span>
                      </td>
                    )}
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function statusWords(a) {
  return a.active ? a.map_status_label : `Inactive · ${a.map_status_label}`;
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
    <tr className="row-editing">
      <td>
        <span className="grid-code">{account.account_no}</span> {account.name}
        <div className="cell-sub">{account.ledger_type}</div>
        <div className="cell-sub">{account.map_status_label}</div>
      </td>
      <td colSpan={6} className="cell-wide">
        <div className="edit-form">
          <label className="field">
            <span className="field-label">Division</span>
            <select className="select" value={division} onChange={(e) => setDivision(e.target.value)} disabled={busy}>
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
          <label className="field">
            <span className="field-label">Cost category</span>
            <select className="select" value={category} onChange={(e) => setCategory(e.target.value)} disabled={busy}>
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
          <label className="check">
            <input type="checkbox" checked={inJobCost} onChange={(e) => setInJobCost(e.target.checked)} disabled={busy} />
            In job cost
          </label>
          <span className="actions actions-row">
            <button type="button" className="button" disabled={busy} onClick={() => onSave(body(false))}>
              Save
            </button>
            <button type="button" className="button" disabled={busy} onClick={() => onSave(body(true))}>
              Save and confirm
            </button>
            <button type="button" className="link-button" disabled={busy} onClick={onCancel}>
              Cancel
            </button>
          </span>
        </div>
      </td>
    </tr>
  );
}
