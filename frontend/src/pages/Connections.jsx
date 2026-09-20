import { useEffect, useState } from "react";
import { api } from "../api.js";

// Connections (F05): the QuickBooks connection of the active company. The mount
// effect only reads (GET); connect, reconnect and disconnect are event handlers.
//
// Everything a person reads comes from the API's display fields (status_label,
// message, result_message); the machine values (status, error_detail, result) are
// compared, never shown (D-22). Connecting leaves this page for QuickBooks; the
// browser comes back to /connections?result=…, which Shell hands over as `result`.
export default function Connections({ me, result }) {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    const query = result ? `?result=${encodeURIComponent(result)}` : "";
    api("GET", `/api/qbo/status${query}`)
      .then(setStatus)
      .catch(() => setError("The connection status could not be loaded. Refresh the page."));
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
        {!status.can_manage && !connected && (
          <p className="hint">A firm admin connects QuickBooks for this company.</p>
        )}
        {status.can_manage && (
          <div className="toolbar">
            {!connected && !needsReconnect && (
              <button type="button" className="button-primary" onClick={connect} disabled={busy}>
                {busy ? "Opening QuickBooks…" : "Connect to QuickBooks"}
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
        {error && <p className="error">{error}</p>}
      </section>
    </div>
  );
}
