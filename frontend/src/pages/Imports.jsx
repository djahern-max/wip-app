import { useEffect, useState } from "react";
import { api, upload } from "../api.js";
import { styles } from "./Login.jsx";

// Imports (F03): choose a source kind and a file, upload, see the batches of the
// active company. Mount effects only read (GET); the upload is an event handler.
// The worker sets the status; "Refresh" re-reads the list (no polling).
export default function Imports({ me }) {
  const [kinds, setKinds] = useState([]);
  const [batches, setBatches] = useState([]);
  const [kind, setKind] = useState("unparsed_file");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);
  const [inputKey, setInputKey] = useState(0); // remounts the file input after an upload

  function reload() {
    return api("GET", "/api/imports").then(setBatches).catch((e) => setError(e.detail || e.message));
  }

  useEffect(() => {
    api("GET", "/api/imports/source-kinds").then(setKinds).catch((e) => setError(e.detail || e.message));
    api("GET", "/api/imports").then(setBatches).catch((e) => setError(e.detail || e.message));
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
          ? `Already uploaded: this file matches "${r.batch.original_filename}" (${r.batch.status}). No new batch was created.`
          : `Uploaded "${r.batch.original_filename}". The worker will process it.`,
      );
      setFile(null);
      setInputKey((k) => k + 1);
      await reload();
    } catch (err) {
      setError(err.detail || "Upload failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Imports</h2>
      <form onSubmit={submit} style={{ ...styles.card, maxWidth: 520, marginBottom: "1.5rem" }}>
        <label style={styles.label}>
          Source
          <select style={styles.input} value={kind} onChange={(e) => setKind(e.target.value)} disabled={busy}>
            {kinds.map((k) => (
              <option key={k.name} value={k.name}>
                {k.name}
                {k.extensions.length ? ` (.${k.extensions.join(", .")})` : ""}
              </option>
            ))}
          </select>
        </label>
        <label style={styles.label}>
          File
          <input
            key={inputKey}
            type="file"
            style={styles.input}
            onChange={(e) => setFile(e.target.files[0] || null)}
            disabled={busy}
          />
        </label>
        <button type="submit" style={styles.button} disabled={busy || !file}>
          {busy ? "Uploading…" : "Upload"}
        </button>
        {notice && <p style={styles.hint}>{notice}</p>}
        {error && <p style={styles.error}>{error}</p>}
      </form>

      <p>
        <button type="button" style={styles.linkButton} onClick={reload}>
          Refresh
        </button>
      </p>
      <table style={table}>
        <thead>
          <tr>
            <th style={th}>File</th>
            <th style={th}>Source</th>
            <th style={th}>Status</th>
            <th style={th}>Rows</th>
            <th style={th}>Uploaded by</th>
            <th style={th}>Uploaded</th>
            <th style={th}></th>
          </tr>
        </thead>
        <tbody>
          {batches.length === 0 && (
            <tr>
              <td style={td} colSpan={7}>
                No files uploaded for this company yet.
              </td>
            </tr>
          )}
          {batches.map((b) => (
            <tr key={b.id}>
              <td style={td} title={b.sha256}>
                {b.original_filename} <span style={styles.hint}>({formatBytes(b.byte_size)})</span>
              </td>
              <td style={td}>{b.source_kind}</td>
              <td style={td}>
                {b.status}
                {b.error && <div style={styles.error}>{b.error}</div>}
              </td>
              <td style={td}>
                {b.rows_loaded}
                {b.rows_rejected ? ` loaded, ${b.rows_rejected} rejected` : ""}
              </td>
              <td style={td}>{b.uploaded_by_email || "—"}</td>
              <td style={td}>{new Date(b.uploaded_at).toLocaleString()}</td>
              <td style={td}>
                <a href={`/api/imports/${b.id}/download`}>Download</a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

const table = { borderCollapse: "collapse", width: "100%", fontSize: 14 };
const th = { textAlign: "left", borderBottom: "1px solid #ddd", padding: "0.4rem 0.6rem" };
const td = { borderBottom: "1px solid #eee", padding: "0.4rem 0.6rem", verticalAlign: "top" };
