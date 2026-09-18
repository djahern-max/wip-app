import { useEffect, useState } from "react";
import { api, upload } from "../api.js";
import { useNarrow } from "../narrow.js";

// Imports (F03): choose a source and a file, upload, see the batches of the active
// company. Mount effects only read (GET); the upload is an event handler. The
// worker sets the status; "Refresh" re-reads the list (no polling).
//
// Everything a person reads comes from the API's display fields (source_label,
// status_label, message); the machine values (source_kind, status, error_detail)
// are for OPERATIONS and never shown (D-22).
//
// Layout: a table with .num columns on a laptop; below 640 px this admin screen
// stacks to cards. Reports (F08+) must NOT do that: they use the container-scroll
// pattern (.table-wrap, first column held in place) from styles.css.
export default function Imports({ me }) {
  const narrow = useNarrow();
  const [kinds, setKinds] = useState([]);
  const [batches, setBatches] = useState([]);
  const [kind, setKind] = useState("unparsed_file");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);
  const [inputKey, setInputKey] = useState(0); // remounts the file input after an upload

  const loadFailed = "The list of files could not be loaded. Refresh the page.";

  function reload() {
    return api("GET", "/api/imports").then(setBatches).catch(() => setError(loadFailed));
  }

  useEffect(() => {
    api("GET", "/api/imports/source-kinds").then(setKinds).catch(() => setError(loadFailed));
    api("GET", "/api/imports").then(setBatches).catch(() => setError(loadFailed));
  }, [me.active_tenant_id]);

  async function submit(e) {
    e.preventDefault();
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const form = new FormData();
      form.append("source_kind", kind);
      form.append("file", file);
      const r = await upload("/api/imports", form);
      setNotice(
        r.duplicate
          ? `This file was already uploaded as "${r.batch.original_filename}" (${r.batch.status_label}). Nothing new was created.`
          : `"${r.batch.original_filename}" was uploaded and will be processed shortly. Refresh to see its status.`,
      );
      setFile(null);
      setInputKey((k) => k + 1);
      await reload();
    } catch (err) {
      // A refusal carries a sentence written for the person (what happened, what to
      // do next); anything else is the connection.
      setError(
        typeof err.detail === "string" && err.detail
          ? err.detail
          : "The upload did not complete. Check your connection and try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h2>Imports</h2>
      <form onSubmit={submit} className="card card-wide">
        <label className="label">
          Source
          <select className="input" value={kind} onChange={(e) => setKind(e.target.value)} disabled={busy}>
            {kinds.map((k) => (
              <option key={k.name} value={k.name}>
                {k.label}
                {k.extensions.length ? ` (${k.extensions.map((x) => "." + x).join(", ")})` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="label">
          File
          <input
            key={inputKey}
            type="file"
            className="input"
            onChange={(e) => setFile(e.target.files[0] || null)}
            disabled={busy}
          />
        </label>
        <button type="submit" className="button-primary" disabled={busy || !file}>
          {busy ? "Uploading…" : "Upload file"}
        </button>
        {notice && <p className="hint">{notice}</p>}
        {error && <p className="error">{error}</p>}
      </form>

      <p>
        <button type="button" className="link-button" onClick={reload}>
          Refresh
        </button>
      </p>
      {narrow ? (
        <div className="card-list">
          {batches.length === 0 && <p className="hint">No files uploaded for this company yet.</p>}
          {batches.map((b) => (
            <div key={b.id} className="item">
              <div className="item-title">{b.original_filename}</div>
              <div className="hint">
                {b.source_label} · {formatBytes(b.byte_size)} · {new Date(b.uploaded_at).toLocaleString()}
              </div>
              <div>
                <strong>{b.status_label}</strong> · {rowsText(b)}
              </div>
              {b.message && <div className={b.status === "failed" ? "error" : "hint"}>{b.message}</div>}
              <div className="hint">{b.uploaded_by_email || "unknown user"}</div>
              <a href={`/api/imports/${b.id}/download`}>Download</a>
            </div>
          ))}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>File</th>
                <th>Source</th>
                <th className="num">Size</th>
                <th>Status</th>
                <th className="num">Rows loaded</th>
                <th className="num">Rows skipped</th>
                <th>Uploaded by</th>
                <th>Uploaded</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {batches.length === 0 && (
                <tr>
                  <td colSpan={9}>No files uploaded for this company yet.</td>
                </tr>
              )}
              {batches.map((b) => (
                <tr key={b.id}>
                  <td className="wrap-anywhere">{b.original_filename}</td>
                  <td>{b.source_label}</td>
                  <td className="num">{formatBytes(b.byte_size)}</td>
                  <td>
                    {b.status_label}
                    {b.message && <div className={b.status === "failed" ? "error" : "hint"}>{b.message}</div>}
                  </td>
                  <td className="num">{b.rows_loaded}</td>
                  <td className="num">{b.rows_rejected}</td>
                  <td>{b.uploaded_by_email || "unknown user"}</td>
                  <td>{new Date(b.uploaded_at).toLocaleString()}</td>
                  <td>
                    <a href={`/api/imports/${b.id}/download`}>Download</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function rowsText(b) {
  return b.rows_rejected ? `${b.rows_loaded} rows loaded, ${b.rows_rejected} skipped` : `${b.rows_loaded} rows loaded`;
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
