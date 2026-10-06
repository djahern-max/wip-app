import { useEffect, useState } from "react";
import { api } from "../api.js";
import { formatMoney } from "../money.js";

// F07.4 (D-42): every kept, confirmed, unapproved change-order work area on a job that is
// Sold, In progress or Substantially complete, with its amount and age, and a total. Read
// by every role; each row links to its job, where the project manager approves. Mount
// effects only read (GET). The table scrolls inside .table-wrap with the first column held.

export default function UnapprovedChangeOrders({ me, onBack, onOpenJob }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("GET", "/api/change-orders/unapproved")
      .then(setData)
      .catch(() => setError("The unapproved change orders could not be loaded. Refresh the page."));
  }, [me.active_tenant_id]);

  return (
    <div>
      <p>
        <button type="button" className="link-button" onClick={onBack}>
          ← All jobs
        </button>
      </p>
      <h2>Unapproved change orders</h2>
      {error && <p className="error">{error}</p>}
      {!data ? (
        <p className="hint">{error ? "" : "Loading…"}</p>
      ) : (
        <>
          <p className="hint">
            {data.tenant_name}. As of {data.as_of}: every change order on a sold, in progress or substantially
            complete job that no one has recorded the customer agreeing to (D-42). Its estimated cost is already in
            EAC (D-44); its price is outside the revised contract until it is approved on the job.
          </p>
          <p>
            {data.count === 0
              ? "Every change order on an open job is approved."
              : `${data.count} change ${data.count === 1 ? "order" : "orders"}, ${formatMoney(data.total)} in total.`}
          </p>
          {data.count > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Estimate</th>
                    <th>Estimator</th>
                    <th>Order</th>
                    <th>Work area</th>
                    <th className="num">Price</th>
                    <th className="num">Days since first appeared</th>
                    <th>Note</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r) => (
                    <tr key={`${r.job_id}-${r.estimate_number}-${r.order_no}`}>
                      <td>
                        <button type="button" className="link-button" onClick={() => onOpenJob(r.job_id)}>
                          {r.job_name}
                        </button>
                      </td>
                      <td>{r.estimate_number}</td>
                      <td>{r.estimator || ""}</td>
                      <td>#{r.order_no}</td>
                      <td>{r.name}</td>
                      <td className="num">{formatMoney(r.price)}</td>
                      <td className="num">{r.days}</td>
                      <td>{r.approval_ended ? "An earlier approval no longer applies: the price or the name changed" : ""}</td>
                    </tr>
                  ))}
                  <tr className="totals">
                    <td>Total</td>
                    <td></td>
                    <td></td>
                    <td></td>
                    <td>{data.count} change {data.count === 1 ? "order" : "orders"}</td>
                    <td className="num">{formatMoney(data.total)}</td>
                    <td></td>
                    <td></td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
