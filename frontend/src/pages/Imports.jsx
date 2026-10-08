import { useEffect, useState } from "react";
import { api, upload } from "../api.js";
import { useNarrow } from "../narrow.js";
import { chooseSource, rememberSource, rememberedSource, sessionStore } from "../sourceChoice.js";

// Imports (F03): choose a source and a file, upload, see the batches of the active
// company. Mount effects only read (GET); the upload is an event handler. The
// worker sets the status; "Refresh" re-reads the list (no polling).
//
// Everything a person reads comes from the API's display fields (source_label,
// status_label, message); the machine values (source_kind, status, error_detail)
// are for OPERATIONS and never shown (D-22).
//
// The Source stays as chosen after an upload, after opening another screen and after
// a page refresh (sourceChoice.js: per company, for the browser session). The first
// visit per company per session opens on "Choose a source" and Upload waits for a
// choice (F06.1).
//
// Layout (F09.3): source, file and the upload button on one line on a laptop, wrapping
// on a phone; the files in a table with .num columns on a laptop; below 640 px this admin screen
// stacks to cards. Reports (F08+) must NOT do that: they use the container-scroll
// pattern (.table-wrap, first column held in place) from styles.css.
export default function Imports({ me }) {
  const narrow = useNarrow();
  const [kinds, setKinds] = useState([]);
  const [batches, setBatches] = useState([]);
  const [kind, setKind] = useState(() =>
    chooseSource([], rememberedSource(sessionStore(), me.active_tenant_id), null),
  );
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);
  const [inputKey, setInputKey] = useState(0); // remounts the file input after an upload

  const loadFailed = "The list of files could not be loaded. Refresh the page.";

  function reload() {
    return api("GET", "/api/imports").then(setBatches).catch(() => setError(loadFailed));
  }

  function chooseKind(name) {
    setKind(name);
    rememberSource(sessionStore(), me.active_tenant_id, name);
  }

  useEffect(() => {
    const remembered = rememberedSource(sessionStore(), me.active_tenant_id);
    setKind(chooseSource([], remembered, null)); // another company: its own choice
    api("GET", "/api/imports/source-kinds")
      .then((list) => {
        setKinds(list);
        setKind((current) => chooseSource(list, current, remembered));
      })
      .catch(() => setError(loadFailed));
    api("GET", "/api/imports").then(setBatches).catch(() => setError(loadFailed));
  }, [me.active_tenant_id]);

  async function submit(e) {
    e.preventDefault();
    if (!file || !kind || busy) return;
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
      <div className="page-head">
        <h1>Imports</h1>
      </div>
      <form onSubmit={submit} className="upload-form">
        <label className="field">
          <span className="field-label">Source</span>
          <select className="select" value={kind} onChange={(e) => chooseKind(e.target.value)} disabled={busy}>
            <option value="">Choose a source</option>
            {kinds.map((k) => (
              <option key={k.name} value={k.name}>
                {k.label}
                {k.extensions.length ? ` (${k.extensions.map((x) => "." + x).join(", ")})` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field-label">File</span>
          <input
            key={inputKey}
            type="file"
            className="file-input"
            onChange={(e) => setFile(e.target.files[0] || null)}
            disabled={busy}
          />
        </label>
        <button type="submit" className="button-primary" disabled={busy || !file || !kind}>
          {busy ? "Uploading…" : "Upload file"}
        </button>
        {!busy && (!file || !kind) && !notice && !error && (
          <p className="hint upload-note">Choose a source and a file, then upload.</p>
        )}
        {notice && <p className="hint upload-note">{notice}</p>}
        {error && <p className="error upload-note">{error}</p>}
      </form>

      <div className="section-head section-head-wide">
        <h2>Uploaded files</h2>
        <button type="button" className="button" onClick={reload}>
          Refresh
        </button>
      </div>
      {narrow ? (
        <div className="card-list">
          {batches.length === 0 && <div className="empty">No files uploaded for this company yet.</div>}
          {batches.map((b) => (
            <div key={b.id} className="item">
              <div className="item-title">{b.original_filename}</div>
              <div className="hint">
                {b.source_label} · {formatBytes(b.byte_size)} · {new Date(b.uploaded_at).toLocaleString()}
              </div>
              <div>
                <strong>{b.status_label}</strong> · {rowsText(b)}
              </div>
              {b.message && <div className={messageClass(b)}>{b.message}</div>}
              <Issues items={b.issues} />
              <div className="hint">{b.uploaded_by_email || "unknown user"}</div>
              <a href={`/api/imports/${b.id}/download`}>Download</a>
            </div>
          ))}
        </div>
      ) : (
        batches.length === 0 ? (
          <div className="empty">No files uploaded for this company yet.</div>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Status</th>
                  <th className="num">Rows loaded</th>
                  <th className="num">Rows skipped</th>
                  <th>Uploaded</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={b.id}>
                    <td className="wrap-anywhere">
                      {b.original_filename}
                      <div className="cell-sub">
                        {b.source_label} · {formatBytes(b.byte_size)}
                      </div>
                    </td>
                    <td>
                      {b.status_label}
                      {b.message && <div className={messageClass(b)}>{b.message}</div>}
                      <Issues items={b.issues} />
                    </td>
                    <td className="num">{b.rows_loaded}</td>
                    <td className="num">{b.rows_rejected}</td>
                    <td>
                      {new Date(b.uploaded_at).toLocaleString()}
                      <div className="cell-sub">{b.uploaded_by_email || "unknown user"}</div>
                    </td>
                    <td>
                      <a href={`/api/imports/${b.id}/download`}>Download</a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
}

// F06: rows the file could not load and file-level facts, one sentence each (the
// EST_* code travels in the API for OPERATIONS and is never shown).
function Issues({ items }) {
  if (!items || items.length === 0) return null;
  return (
    <ul className="issues">
      {items.map((i, n) => (
        <li key={n}>{i.message}</li>
      ))}
    </ul>
  );
}

// Colour second: the sentence already says what happened. A file that could not be
// read, or from which nothing could be read, needs the person to act.
function messageClass(b) {
  return b.status === "failed" || b.status === "nothing_loaded" ? "error" : "hint";
}

function rowsText(b) {
  return b.rows_rejected ? `${b.rows_loaded} rows loaded, ${b.rows_rejected} skipped` : `${b.rows_loaded} rows loaded`;
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
