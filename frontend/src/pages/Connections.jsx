import { useEffect, useState } from "react";
import { api } from "../api.js";
import { addMoney, formatMoney } from "../money.js";

// Connections (F05): the QuickBooks connection of the active company and what the
// platform holds for it. The mount effect only reads (GET); connect, reconnect,
// disconnect, "Sync now" and a fresh backfill are event handlers.
//
// Everything a person reads comes from the API's display fields (status_label,
// message, result_message, *_label, attention labels); the machine values (status,
// error_detail, result, outcome) are compared, never shown (D-22). Money arrives as
// strings with cents and is rendered by formatMoney; the page never parses a number.
// Connecting leaves this page for QuickBooks; the browser comes back to
// /connections?result=…, which Shell hands over as `result`.
//
// One primary action: "Connect to QuickBooks" until a company is connected, then
// "Sync now". Reconnect, Disconnect and a fresh backfill are secondary.
export default function Connections({ me, result }) {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);

  const loadFailed = "The connection status could not be loaded. Refresh the page.";

  function load(withResult) {
    const query = withResult ? `?result=${encodeURIComponent(withResult)}` : "";
    return api("GET", `/api/qbo/status${query}`)
      .then(setStatus)
      .catch(() => setError(loadFailed));
  }

  useEffect(() => {
    load(result);
  }, [me.active_tenant_id, result]);

  async function connect() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api("POST", "/api/qbo/connect");
      window.location.assign(r.authorization_url); // on to QuickBooks; busy stays on
    } catch (err) {
      setError(
        typeof err.detail === "string" && err.detail
          ? err.detail
          : "QuickBooks could not be opened. Check your connection and try again.",
      );
      setBusy(false);
    }
  }

  async function disconnect() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setStatus(await api("POST", "/api/qbo/disconnect"));
      setConfirming(false);
    } catch {
      setError("QuickBooks was not disconnected. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  const syncNow = () =>
    request("/api/qbo/sync", "The sync was not queued. Check your connection and try again.");

  async function request(path, failure) {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const r = await api("POST", path);
      setNotice(r.message);
      await load(null);
    } catch (err) {
      setError(typeof err.detail === "string" && err.detail ? err.detail : failure);
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    return (
      <div>
        <h2>Connections</h2>
        {error ? <p className="error">{error}</p> : <p className="hint">Loading…</p>}
      </div>
    );
  }

  const connected = status.status === "connected";
  const needsReconnect = status.status === "needs_reconnect";
  const resultFailed = status.result && status.result !== "connected";
  const held = status.held;

  return (
    <div>
      <h2>Connections</h2>
      <section className="card card-wide">
        <h3>QuickBooks Online</h3>
        <p>
          <strong>{status.status_label}</strong>
          {status.company_name ? ` · ${status.company_name}` : ""}
          {status.environment === "sandbox" ? " · sandbox company" : ""}
        </p>
        {status.message && <p className={needsReconnect ? "error" : "hint"}>{status.message}</p>}
        {status.result_message && (
          <p className={resultFailed ? "error" : "hint"}>{status.result_message}</p>
        )}
        {status.last_success_at && (
          <p className="hint">Last successful sync: {new Date(status.last_success_at).toLocaleString()}</p>
        )}
        {status.connected_company && (
          <p className="hint">
            {status.last_webhook_at
              ? `Last webhook received: ${new Date(status.last_webhook_at).toLocaleString()}; ${status.webhooks_24h} in the last 24 hours.`
              : "No webhook received yet. Changes arrive with the change poll until one does."}
          </p>
        )}
        {held && held.last_sync && (
          <p className="hint">
            Last run: {held.last_sync.kind_label}, {held.last_sync.outcome_label.toLowerCase()}
            {held.last_sync.finished_at ? `, ${new Date(held.last_sync.finished_at).toLocaleString()}` : ""}
            {held.last_sync.message ? `. ${held.last_sync.message}` : ""}
          </p>
        )}
        {!status.can_manage && !connected && (
          <p className="hint">A firm admin connects QuickBooks for this company.</p>
        )}
        {status.can_manage && (
          <div className="toolbar">
            {!needsReconnect && !confirming && (
              <button
                type="button"
                className="button-primary"
                onClick={connected ? syncNow : connect}
                disabled={busy}
              >
                {busy ? "Working…" : connected ? "Sync now" : "Connect to QuickBooks"}
              </button>
            )}
            {connected && held && held.backfill_needed && !confirming && (
              <button
                type="button"
                className="button"
                onClick={() => request("/api/qbo/backfill", "The backfill was not started. Check your connection and try again.")}
                disabled={busy}
              >
                Start fresh backfill
              </button>
            )}
            {(connected || needsReconnect) && !confirming && (
              <>
                <button type="button" className="button" onClick={connect} disabled={busy}>
                  {busy ? "Opening QuickBooks…" : "Reconnect"}
                </button>
                <button type="button" className="button" onClick={() => setConfirming(true)} disabled={busy}>
                  Disconnect
                </button>
              </>
            )}
            {confirming && (
              <div className="confirm-step">
                <p>
                  Disconnect QuickBooks for this company? Syncing stops. Everything already synced stays.
                </p>
                <button type="button" className="button" onClick={disconnect} disabled={busy}>
                  {busy ? "Disconnecting…" : "Yes, disconnect"}
                </button>{" "}
                <button type="button" className="button" onClick={() => setConfirming(false)} disabled={busy}>
                  Cancel
                </button>
              </div>
            )}
          </div>
        )}
        {notice && <p className="hint">{notice}</p>}
        {error && <p className="error">{error}</p>}
      </section>

      {held && (
        <>
          <h3>Needs attention</h3>
          {held.attention.length === 0 ? (
            <p className="hint">Nothing needs attention.</p>
          ) : (
            <ul>
              {held.attention.map((a) => (
                <li key={a.code}>
                  <strong>{a.label}</strong>
                  {a.detail ? `: ${a.detail}` : ""}
                </li>
              ))}
            </ul>
          )}
          {held.backfill && held.backfill.outcome === null && (
            <p className="hint">A backfill is running. Refresh to follow it.</p>
          )}

          <h3>Month totals</h3>
          <p className="hint">
            By calendar month of the document date. Payments are Payment documents only; sales receipts are
            billed and collected in one document and stand in their own column.
          </p>
          <MonthTotals rows={held.month_totals} />

          <h3>Records held</h3>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Record</th>
                  <th className="num">Current</th>
                  <th className="num">Could not be read</th>
                </tr>
              </thead>
              <tbody>
                {held.entities.map((e) => (
                  <tr key={e.entity}>
                    <td>{e.entity}</td>
                    <td className="num">{e.current}</td>
                    <td className="num">{e.skipped === null ? "not read in this feature" : e.skipped}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

const COLUMNS = [
  ["invoices", "Invoices"],
  ["credit_memos", "Credit memos"],
  ["sales_receipts", "Sales receipts"],
  ["payments", "Payments"],
];

function MonthTotals({ rows }) {
  const totals = Object.fromEntries(
    COLUMNS.map(([key]) => [key, rows.reduce((sum, r) => addMoney(sum, r[key]), "0.00")]),
  );
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Month</th>
            {COLUMNS.map(([key, label]) => (
              <th key={key} className="num">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={COLUMNS.length + 1}>No documents held yet.</td>
            </tr>
          )}
          {rows.map((r) => (
            <tr key={r.month}>
              <td>{r.month}</td>
              {COLUMNS.map(([key]) => (
                <td key={key} className="num">
                  {formatMoney(r[key])}
                </td>
              ))}
            </tr>
          ))}
          {rows.length > 0 && (
            <tr className="totals">
              <td>Total</td>
              {COLUMNS.map(([key]) => (
                <td key={key} className="num">
                  {formatMoney(totals[key])}
                </td>
              ))}
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
