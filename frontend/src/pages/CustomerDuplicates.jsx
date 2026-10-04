import { useEffect, useState } from "react";
import { api } from "../api.js";

// Possible duplicate customers (F07, §10 CUSTOMER_FUZZY): read-only, behind a link on
// the Customers page since F07.2 (D-37). Two active top-level QuickBooks customers whose
// names have the same words once punctuation, "and", "&" and suffixes are dropped, or
// differ by one word (a first name). The owner merges in QuickBooks and the platform
// follows; nothing is written here. Mount effects only read (GET).

export default function CustomerDuplicates({ me }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("GET", "/api/customers/duplicates")
      .then(setData)
      .catch(() => setError("The customer list could not be loaded. Refresh the page."));
  }, [me.active_tenant_id]);

  return (
    <div>
      <h2>Possible duplicate customers</h2>
      <p className="hint">Merge in QuickBooks; the platform follows. Nothing on this page changes QuickBooks.</p>
      {error && <p className="error">{error}</p>}
      {!data ? (
        <p className="hint">{error ? "" : "Loading…"}</p>
      ) : data.pairs.length === 0 ? (
        <p>No customers look like duplicates.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Customer</th>
                <th>QuickBooks id</th>
                <th className="num">Invoices and credits</th>
                <th className="num">Payments</th>
                <th>Looks like</th>
                <th>QuickBooks id</th>
                <th className="num">Invoices and credits</th>
                <th className="num">Payments</th>
              </tr>
            </thead>
            <tbody>
              {data.pairs.map((p) => (
                <tr key={`${p.a.customer_id}-${p.b.customer_id}`}>
                  <td>{p.a.display_name}</td>
                  <td>{p.a.external_id}</td>
                  <td className="num">{p.a.billing_count}</td>
                  <td className="num">{p.a.payment_count}</td>
                  <td>{p.b.display_name}</td>
                  <td>{p.b.external_id}</td>
                  <td className="num">{p.b.billing_count}</td>
                  <td className="num">{p.b.payment_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
