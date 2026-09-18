import { useEffect, useState } from "react";
import { api } from "../../api.js";

// Read-only grid: division × cost category, each cell the cost code (D-23) and the
// ledger account mapped to it, or "no account". The first column (division) is held
// in place while the grid scrolls inside its container.
export default function CostCodes({ me }) {
  const [grid, setGrid] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("GET", "/api/config/cost-codes")
      .then(setGrid)
      .catch(() => setError("The cost codes could not be loaded. Refresh the page."));
  }, [me.active_tenant_id]);

  if (!grid) return <p className="hint">{error || "Loading…"}</p>;
  return (
    <div>
      <p className="hint">
        A cost code is the division digit followed by the category slot (410 = SNOW Labor). It is computed,
        never stored. "no account" means no ledger account is mapped to that cell.
      </p>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Division</th>
              {grid.categories.map((c) => (
                <th key={c.id}>
                  {c.name} <span className="hint">({c.slot})</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.rows.length === 0 && (
              <tr>
                <td colSpan={grid.categories.length + 1}>No divisions yet.</td>
              </tr>
            )}
            {grid.rows.map((r) => (
              <tr key={r.division_id}>
                <td>
                  <strong>{r.code}</strong> {r.name}
                  {!r.code_digit && <div className="hint">no code digit</div>}
                </td>
                {r.cells.map((cell) => (
                  <td key={cell.cost_category_id}>
                    {cell.code ? <div className="grid-code">{cell.code}</div> : <div className="grid-none">no code</div>}
                    {cell.accounts.length === 0 ? (
                      <div className="grid-none">no account</div>
                    ) : (
                      cell.accounts.map((a) => (
                        <div key={a.account_no} className="small">
                          {a.account_no} {a.name}
                          {a.status === "suggested" && <span className="hint"> (suggested)</span>}
                        </div>
                      ))
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
