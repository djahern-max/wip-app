# QuickBooks sandbox fixtures (F05)

Recorded from Intuit's fictitious sample company on 2026-09-21 by
`backend/scripts/qbo_record_fixtures.py`, after the owner had deleted one invoice and
voided another (S-01 (c), (d)). One file per §6.2 entity holding every record (inactive
name-list rows included), plus `cdc_29_days.json`, one Change Data Capture response for
all entities 29 days back, which carries the deleted invoice's stub. The realm id is
replaced by `REALM`; no header, token or company id is present. Amounts are JSON numbers
exactly as Intuit sent them.

Hand-made files, derived from the recorded ones (what changed is in each file's `_note`):
- `cdc_changed_and_deleted.json`: one invoice from `Invoice.json` with a changed line
  amount and total, and the recorded deleted stub.
- `invoice_before_void.json`: the voided invoice as it would have been before the void
  (amounts restored from its `Qty` and the item's price), so the void rule has an earlier
  non-zero version to look at.
- `payment_reapplied.json`: the credit-memo payment from `Payment.json` with both of its
  lines removed and 100.00 unapplied, as after voiding the invoice it paid (the owner's
  "unobserved case").

`make_oracle.py` computes `oracle_month_totals.json` from the recorded files with code that
shares nothing with the normalizers; the month-totals test compares the two.
