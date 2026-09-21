"""Throwaway oracle for the month-totals test (F05 criterion 10). Reads the recorded
fixtures with the standard library and sums TotalAmt by calendar month of TxnDate:
invoices, credit memos (negative), sales receipts, payments. Shares no code with the
normalizers. Run: python tests/fixtures/qbo_sandbox/make_oracle.py"""

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent


def rows(name: str) -> list[dict]:
    return json.loads((HERE / f"{name}.json").read_text(), parse_float=Decimal)


def main() -> None:
    totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for entity, column, sign in (
        ("Invoice", "invoices", 1),
        ("CreditMemo", "credit_memos", -1),
        ("SalesReceipt", "sales_receipts", 1),
        ("Payment", "payments", 1),
    ):
        for r in rows(entity):
            totals[r["TxnDate"][:7]][column] += sign * Decimal(r["TotalAmt"])
    out = [
        {
            "month": month,
            **{
                c: f"{totals[month][c]:.2f}"
                for c in ("invoices", "credit_memos", "sales_receipts", "payments")
            },
        }
        for month in sorted(totals)
    ]
    (HERE / "oracle_month_totals.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
