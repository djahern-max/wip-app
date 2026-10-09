import { useEffect, useState } from "react";
import { api } from "../../api.js";

// Read-only grid: division × cost category, each cell the cost code (D-23) and the
// ledger account mapped to it, or "no account". The first column (division) is held
// in place while the grid scrolls inside its container.
//
// Layout (F09.4): the slot sits under its category's name; a cell with no account has
// its code muted, so the mapped cells are the ones that stand out.
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
      <p className="lead">
        A cost code is the division digit followed by the category slot (410 = SNOW Labor). It is computed,
        never stored. "no account" means no ledger account is mapped to that cell.
      </p>
      {grid.rows.length === 0 ? (
        <div className="empty">
          <strong>No divisions yet.</strong>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Division</th>
                {grid.categories.map((c) => (
                  <th key={c.id}>
                    {c.name}
                    <div className="cell-sub">{c.slot}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {grid.rows.map((r) => (
                <tr key={r.division_id}>
                  <td>
                    <strong>{r.code}</strong> {r.name}
                    {!r.code_digit && <div className="cell-sub">no code digit</div>}
                  </td>
                  {r.cells.map((cell) => (
                    <td key={cell.cost_category_id}>
                      {cell.code ? (
                        <div className={cell.accounts.length === 0 ? "grid-code grid-none" : "grid-code"}>{cell.code}</div>
                      ) : (
                        <div className="grid-none">no code</div>
                      )}
                      {cell.accounts.length === 0 ? (
                        <div className="grid-none">no account</div>
                      ) : (
                        cell.accounts.map((a) => (
                          <div key={a.account_no}>
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
      )}
      <p className="hint">Nothing here can be edited: mappings are changed on Accounts.</p>
    </div>
  );
}
