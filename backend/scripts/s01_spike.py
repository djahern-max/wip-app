#!/usr/bin/env python
# ruff: noqa: E501 - throwaway spike; long print lines are its report
"""Spike S-01 (BLUEPRINT §13.7; D-25): read-only questions put to the Intuit sandbox.

    cd backend && .venv/bin/python scripts/s01_spike.py --tenant qbo-sandbox

Throwaway. Uses the tenant's stored QuickBooks connection (connect it first on the
Connections page). **Prints structure only**: field names, JSON types, counts, and a
few schema-level words (``DetailType``, ``TxnType``, ``status``, booleans). It never
prints a name, an amount, an address, a document number or an id. Nothing is written
to the database except what a token refresh stores. The findings go, by hand, into
``docs/spikes/S-01.md``.

Questions (F05 brief):
  (a) how a project appears on Customer (IsProject, Job, ParentRef)
  (b) whether an invoice header and an expense line carry a reference to the project
  (c) what Change Data Capture returns for a deleted transaction
  (d) how a voided invoice appears
Extras (F05 plan): a payment line that applies a credit memo; which entities CDC
accepts; whether ``WHERE Id > 'n'`` is accepted; accounts with and without AcctNum.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.ingest.models import Connection
from app.integrations.qbo import client as qbo_client
from app.integrations.qbo.reader import CompanyReader
from app.tenancy.models import Tenant

ENTITIES = (
    "Account Customer Vendor Item Invoice Payment CreditMemo SalesReceipt Deposit Bill "
    "VendorCredit Purchase JournalEntry TimeActivity Preferences CompanyInfo"
).split()
NAME_LISTS = {"Account", "Customer", "Vendor", "Item"}
# Schema-level words that may be printed as values.
ENUM_KEYS = {
    "DetailType",
    "TxnType",
    "status",
    "domain",
    "type",
    "PaymentType",
    "GlobalTaxCalculation",
}


def shape(value, key: str = ""):
    """Field names and JSON types. Lists are merged into one element shape."""
    if isinstance(value, dict):
        return {k: shape(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        merged: dict = {}
        scalars: set[str] = set()
        for item in value:
            s = shape(item, key)
            if isinstance(s, dict):
                for k, v in s.items():
                    seen = merged.setdefault(k, v)
                    if isinstance(seen, str) and isinstance(v, str) and "=" in v:
                        values = set(seen.split("=", 1)[1].split("|")) | {v.split("=", 1)[1]}
                        merged[k] = seen.split("=", 1)[0] + "=" + "|".join(sorted(values))
            else:
                scalars.add(str(s))
        return [merged] if merged else [" | ".join(sorted(scalars)) or "empty"]
    if isinstance(value, bool):
        return f"bool={value}"
    if value is None:
        return "null"
    if isinstance(value, int | Decimal):
        return "number"
    if key in ENUM_KEYS:
        return f"string={value}"
    return "string"


def show(title: str, value, indent: int = 2) -> None:
    print(f"\n{title}")
    _print(shape(value), indent)


def _print(s, indent: int) -> None:
    pad = " " * indent
    if isinstance(s, dict):
        for k, v in s.items():
            if isinstance(v, dict | list):
                print(f"{pad}{k}:")
                _print(v, indent + 2)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(s, list):
        for item in s:
            if isinstance(item, dict):
                print(f"{pad}[each]")
                _print(item, indent + 2)
            else:
                print(f"{pad}[{item}]")
    else:
        print(f"{pad}{s}")


def all_rows(reader: CompanyReader, entity: str, where: str = "") -> list[dict]:
    rows: list[dict] = []
    start = 1
    while True:
        clause = f" WHERE {where}" if where else ""
        response = reader.query(
            f"SELECT * FROM {entity}{clause} STARTPOSITION {start} MAXRESULTS 1000",
            operation=entity,
        )
        page = response.get(entity, [])
        rows += page
        if len(page) < 1000:
            return rows
        start += len(page)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Spike S-01 against the connected sandbox company")
    ap.add_argument("--tenant", required=True, metavar="SLUG")
    args = ap.parse_args(argv)
    engine = create_app_engine()
    with untenanted_session(engine) as s:
        tenant_id = s.execute(
            select(Tenant.id).where(Tenant.slug == args.tenant)
        ).scalar_one_or_none()
    if tenant_id is None:
        print(f"no tenant with slug {args.tenant!r}", file=sys.stderr)
        return 2
    with tenant_session(engine, tenant_id) as s:
        row = s.execute(select(Connection).where(Connection.system == "qbo")).scalar_one_or_none()
        if row is None or row.status != "connected":
            print("this tenant has no connected QuickBooks company", file=sys.stderr)
            return 2
        if row.environment != "sandbox":
            print("S-01 runs against the sandbox only (D-25)", file=sys.stderr)
            return 2
        connection_id = row.id
    reader = CompanyReader(engine, tenant_id, connection_id)

    print("=" * 78, "\nS-01 · structure only · environment=sandbox\n", "=" * 78, sep="")

    # --- (a) projects on Customer ----------------------------------------------------
    customers = all_rows(reader, "Customer", "Active IN (true, false)")
    projects = [c for c in customers if c.get("IsProject") is True]
    jobs = [c for c in customers if c.get("Job") is True]
    print(
        f"\n(a) Customer rows: {len(customers)}; IsProject=true: {len(projects)}; Job=true: {len(jobs)}"
    )
    print(
        f"    Job=true and IsProject not true (sub-customers): "
        f"{sum(1 for c in jobs if c.get('IsProject') is not True)}"
    )
    print(f"    every IsProject row has Job=true: {all(c.get('Job') is True for c in projects)}")
    print(f"    every IsProject row has ParentRef: {all('ParentRef' in c for c in projects)}")
    print(
        f"    IsProject key present on non-project rows: "
        f"{sum(1 for c in customers if 'IsProject' in c and c.get('IsProject') is not True)} of "
        f"{len(customers) - len(projects)}"
    )
    print(f"    Level values on projects: {sorted({str(c.get('Level')) for c in projects})}")
    print(
        f"    FullyQualifiedName of a project contains ':': "
        f"{all(':' in str(c.get('FullyQualifiedName', '')) for c in projects)}"
    )
    if projects:
        show("    shape of a project Customer:", projects[0], 6)
    plain = next((c for c in customers if c.get("Job") is not True), None)
    if plain:
        print(
            f"    keys on a project not on a plain customer: "
            f"{sorted(set(projects[0]) - set(plain)) if projects else '-'}"
        )
    project_ids = {c["Id"] for c in projects}

    # --- (b) references back to the project -----------------------------------------
    invoices = all_rows(reader, "Invoice")
    on_project = [i for i in invoices if i.get("CustomerRef", {}).get("value") in project_ids]
    print(
        f"\n(b) Invoice rows: {len(invoices)}; with header CustomerRef = a project: {len(on_project)}"
    )
    print(
        f"    invoices carrying a header ProjectRef: {sum(1 for i in invoices if 'ProjectRef' in i)}"
    )
    if on_project:
        inv = on_project[0]
        show("    shape of an invoice on a project (header and lines):", inv, 6)
    purchases = all_rows(reader, "Purchase")
    bills = all_rows(reader, "Bill")
    for label, txns in (("Purchase", purchases), ("Bill", bills)):
        lines = [(t, ln) for t in txns for ln in t.get("Line", [])]

        def detail(ln):
            return (
                ln.get("AccountBasedExpenseLineDetail")
                or ln.get("ItemBasedExpenseLineDetail")
                or {}
            )

        with_cust = [(t, ln) for t, ln in lines if "CustomerRef" in detail(ln)]
        to_project = [
            (t, ln) for t, ln in with_cust if detail(ln)["CustomerRef"].get("value") in project_ids
        ]
        print(
            f"    {label}: {len(txns)} txns, {len(lines)} lines; lines with detail.CustomerRef: "
            f"{len(with_cust)}; of those pointing at a project: {len(to_project)}"
        )
        print(
            f"      lines with detail.ProjectRef: {sum(1 for _, ln in lines if 'ProjectRef' in detail(ln))}; "
            f"headers with ProjectRef: {sum(1 for t in txns if 'ProjectRef' in t)}; "
            f"headers with CustomerRef: {sum(1 for t in txns if 'CustomerRef' in t)}"
        )
        print(
            f"      BillableStatus values on lines with a customer: "
            f"{sorted({str(detail(ln).get('BillableStatus')) for _, ln in with_cust})}"
        )
        if to_project:
            show(f"      shape of a {label} line pointing at a project:", to_project[0][1], 8)

    # --- (c) CDC and deletes ----------------------------------------------------------------
    since = (datetime.now(UTC) - timedelta(days=29)).replace(microsecond=0).isoformat()
    print("\n(c) CDC, changedSince = 29 days ago; one call per entity to see which are accepted")
    deleted_example = None
    for entity in ENTITIES:
        try:
            body = reader.get(
                "cdc", {"entities": entity, "changedSince": since}, operation=f"CDC {entity}"
            )
        except qbo_client.QboError as exc:
            print(f"    {entity}: refused (status {exc.status})")
            continue
        changed = [
            r
            for block in body.get("CDCResponse", [])
            for q in block.get("QueryResponse", [])
            for r in q.get(entity, [])
        ]
        deleted = [r for r in changed if r.get("status") == "Deleted"]
        print(f"    {entity}: accepted; changed={len(changed)} deleted={len(deleted)}")
        if deleted and deleted_example is None:
            deleted_example = (entity, deleted[0])
    try:
        body = reader.get(
            "cdc",
            {"entities": ",".join(ENTITIES[:4]), "changedSince": since},
            operation="CDC multi",
        )
        show(
            "    envelope of a CDC response for several entities:",
            {k: v for k, v in body.items() if k != "CDCResponse"}
            | {
                "CDCResponse": [
                    {
                        "QueryResponse": [
                            {k: ("…" if isinstance(v, list) else v) for k, v in q.items()}
                            for q in block.get("QueryResponse", [])
                        ]
                    }
                    for block in body.get("CDCResponse", [])
                ]
            },
            6,
        )
    except qbo_client.QboError as exc:
        print(f"    multi-entity CDC refused (status {exc.status})")
    if deleted_example:
        show(
            f"    a deleted {deleted_example[0]} as CDC returns it (complete):",
            deleted_example[1],
            6,
        )
    else:
        print(
            "    NO deleted record in the window: delete one invoice in the sandbox, then run again."
        )
    try:
        old = (datetime.now(UTC) - timedelta(days=45)).replace(microsecond=0).isoformat()
        reader.get("cdc", {"entities": "Invoice", "changedSince": old}, operation="CDC 45 days")
        print("    changedSince 45 days ago: accepted (no error)")
    except qbo_client.QboError as exc:
        print(f"    changedSince 45 days ago: refused (status {exc.status})")

    # --- (d) a voided invoice -----------------------------------------------------------------
    zero = [i for i in invoices if i.get("TotalAmt") == 0]
    voided = [i for i in zero if "void" in str(i.get("PrivateNote", "")).lower()]
    print(
        f"\n(d) invoices with TotalAmt = 0: {len(zero)}; of those with 'void' in PrivateNote: {len(voided)}"
    )
    if voided:
        v = voided[0]
        print(
            f"    PrivateNote starts with 'Voided': {str(v.get('PrivateNote', '')).startswith('Voided')}"
        )
        print(
            f"    Balance = 0: {v.get('Balance') == 0}; every line Amount = 0: "
            f"{all(ln.get('Amount', 0) == 0 for ln in v.get('Line', []))}"
        )
        print(
            f"    keys on the voided invoice not on an ordinary one: "
            f"{sorted(set(v) - set(next(i for i in invoices if i.get('TotalAmt') != 0)))}"
        )
        show("    shape of a voided invoice:", v, 6)
    else:
        print("    NO voided invoice found: void one invoice in the sandbox, then run again.")

    # --- extras ---------------------------------------------------------------------------------
    payments = all_rows(reader, "Payment")
    print(f"\n(extra 1) Payment rows: {len(payments)}")
    kinds: dict[str, int] = {}
    mismatched = 0
    credit_example = None
    for p in payments:
        total = sum((ln.get("Amount", 0) for ln in p.get("Line", [])), Decimal(0))
        signed = Decimal(0)
        for ln in p.get("Line", []):
            for lt in ln.get("LinkedTxn", []):
                kinds[lt.get("TxnType", "?")] = kinds.get(lt.get("TxnType", "?"), 0) + 1
                if lt.get("TxnType") == "CreditMemo":
                    credit_example = credit_example or p
            is_credit = any(lt.get("TxnType") == "CreditMemo" for lt in ln.get("LinkedTxn", []))
            signed += -ln.get("Amount", 0) if is_credit else ln.get("Amount", 0)
        if total + p.get("UnappliedAmt", 0) != p.get("TotalAmt", 0):
            mismatched += 1
            if signed + p.get("UnappliedAmt", 0) == p.get("TotalAmt", 0):
                kinds["(identity holds only with credit memo lines negative)"] = (
                    kinds.get("(identity holds only with credit memo lines negative)", 0) + 1
                )
    print(f"    LinkedTxn.TxnType counts on payment lines: {kinds}")
    print(f"    payments where sum(Line.Amount) + UnappliedAmt != TotalAmt: {mismatched}")
    if credit_example:
        print(
            f"    in a payment applying a credit memo, every Line.Amount > 0: "
            f"{all(ln.get('Amount', 0) > 0 for ln in credit_example.get('Line', []))}"
        )
        show("    shape of a payment that applies a credit memo:", credit_example, 6)
    else:
        print("    no payment applies a credit memo in this company.")
    if payments:
        show("    shape of an ordinary payment:", payments[0], 6)

    print("\n(extra 2) keyset paging")
    try:
        r = reader.query(
            "SELECT Id FROM Invoice WHERE Id > '1' ORDERBY Id MAXRESULTS 5",
            operation="Invoice keyset",
        )
        ids = [int(i["Id"]) for i in r.get("Invoice", [])]
        print(
            f"    WHERE Id > '1' ORDERBY Id: accepted; ascending numerically: {ids == sorted(ids)}; rows: {len(ids)}"
        )
    except qbo_client.QboError as exc:
        print(f"    WHERE Id > '1': refused (status {exc.status})")
    r = reader.query("SELECT COUNT(*) FROM Invoice", operation="Invoice count")
    show("    shape of a COUNT(*) response:", r, 6)

    accounts = all_rows(reader, "Account", "Active IN (true, false)")
    print(
        f"\n(extra 3) Account rows: {len(accounts)}; with AcctNum: {sum(1 for a in accounts if a.get('AcctNum'))}"
    )
    if accounts:
        show("    shape of an Account:", accounts[0], 6)
    for entity in ("CreditMemo", "SalesReceipt", "Deposit"):
        rows = all_rows(reader, entity)
        print(f"\n(extra 4) {entity} rows: {len(rows)}")
        if rows:
            show(f"    shape of a {entity}:", rows[0], 6)
    # --- second pass: questions the first run raised ---------------------------------------
    customer_ids = {c["Id"] for c in customers}
    print("\n(second pass 1) what ProjectRef holds")
    for inv in on_project:
        pr, cr = inv.get("ProjectRef", {}).get("value"), inv.get("CustomerRef", {}).get("value")
        print(
            f"    invoice header: ProjectRef.value == CustomerRef.value: {pr == cr}; "
            f"ProjectRef.value is a Customer.Id: {pr in customer_ids}; "
            f"digits: ProjectRef={len(str(pr))} CustomerRef={len(str(cr))}"
        )
    for label, txns in (("Purchase", purchases), ("Bill", bills)):
        for t in txns:
            for ln in t.get("Line", []):
                if "ProjectRef" in ln:
                    d = (
                        ln.get("AccountBasedExpenseLineDetail")
                        or ln.get("ItemBasedExpenseLineDetail")
                        or {}
                    )
                    pr, cr = ln["ProjectRef"].get("value"), d.get("CustomerRef", {}).get("value")
                    print(
                        f"    {label} line: ProjectRef sits on the line (beside the detail): True; "
                        f"ProjectRef.value == detail.CustomerRef.value: {pr == cr}; "
                        f"detail.CustomerRef is a project: {cr in project_ids}; "
                        f"same ProjectRef as the invoice: "
                        f"{pr in {i.get('ProjectRef', {}).get('value') for i in on_project}}"
                    )
    sub_ids = {c["Id"] for c in jobs if c.get("IsProject") is not True}
    print(
        f"    invoices whose CustomerRef is a sub-customer (Job, not project): "
        f"{sum(1 for i in invoices if i.get('CustomerRef', {}).get('value') in sub_ids)}; "
        f"of those with ProjectRef: "
        f"{sum(1 for i in invoices if i.get('CustomerRef', {}).get('value') in sub_ids and 'ProjectRef' in i)}"
    )

    print("\n(second pass 2) sales-form lines and totals")
    sales = {
        "Invoice": invoices,
        "CreditMemo": all_rows(reader, "CreditMemo"),
        "SalesReceipt": all_rows(reader, "SalesReceipt"),
    }
    for entity, docs in sales.items():
        types: dict[str, int] = {}
        ok_sub = ok_total = deep = with_deposit = no_subtotal = 0
        for d in docs:
            item_sum, sub = Decimal(0), None
            for ln in d.get("Line", []):
                types[ln.get("DetailType", "?")] = types.get(ln.get("DetailType", "?"), 0) + 1
                if ln.get("DetailType") == "SubTotalLineDetail":
                    sub = ln.get("Amount", Decimal(0))
                elif ln.get("DetailType") == "SalesItemLineDetail":
                    item_sum += ln.get("Amount", 0)
                amt = ln.get("Amount")
                if isinstance(amt, Decimal) and amt.as_tuple().exponent < -2:
                    deep += 1
            tax = (d.get("TxnTaxDetail") or {}).get("TotalTax", 0)
            if sub is None:
                no_subtotal += 1
            else:
                ok_sub += item_sum == sub
                ok_total += sub + tax == d.get("TotalAmt")
            with_deposit += "Deposit" in d
            for key in ("TotalAmt", "Balance"):
                v = d.get(key)
                if isinstance(v, Decimal) and v.as_tuple().exponent < -2:
                    deep += 1
        print(f"    {entity}: docs={len(docs)} line DetailType counts={types}")
        print(
            f"      no SubTotal line: {no_subtotal}; sum(SalesItem lines) == SubTotal line: {ok_sub}; "
            f"SubTotal + TotalTax == TotalAmt: {ok_total}; amounts with more than 2 decimals: {deep}; "
            f"docs with a header 'Deposit' field: {with_deposit}"
        )
        print(
            f"      docs without TxnTaxDetail.TotalTax: "
            f"{sum(1 for d in docs if 'TotalTax' not in (d.get('TxnTaxDetail') or {}))}; "
            f"docs with GlobalTaxCalculation: {sum(1 for d in docs if 'GlobalTaxCalculation' in d)}"
        )

    print("\n(second pass 3) is Id compared as a number in a query?")
    r = reader.query(
        "SELECT COUNT(*) FROM Invoice WHERE Id > '9'", operation="Invoice keyset count"
    )
    numeric = sum(1 for i in invoices if int(i["Id"]) > 9)
    textual = sum(1 for i in invoices if i["Id"] > "9")
    print(
        f"    COUNT(Id > '9') == count by number: {r.get('totalCount') == numeric}; "
        f"== count by text: {r.get('totalCount') == textual}; (the two differ here: {numeric != textual})"
    )

    print("\n(second pass 4) payments")
    print(
        f"    without DepositToAccountRef: {sum(1 for p in payments if 'DepositToAccountRef' not in p)} of {len(payments)}; "
        f"lines with more than one LinkedTxn: "
        f"{sum(1 for p in payments for ln in p.get('Line', []) if len(ln.get('LinkedTxn', [])) > 1)}; "
        f"payments with UnappliedAmt > 0: {sum(1 for p in payments if p.get('UnappliedAmt', 0) > 0)}"
    )
    credit_lines = [
        ln
        for p in payments
        for ln in p.get("Line", [])
        if any(lt.get("TxnType") == "CreditMemo" for lt in ln.get("LinkedTxn", []))
    ]
    for ln in credit_lines:
        show("    a payment line that applies a credit memo:", ln, 6)
    deposits = all_rows(reader, "Deposit")
    dl = [ln for d in deposits for ln in d.get("Line", [])]
    print(
        f"\n(second pass 5) Deposit lines: {len(dl)}; linked to a Payment: "
        f"{sum(1 for ln in dl if any(lt.get('TxnType') == 'Payment' for lt in ln.get('LinkedTxn', [])))}; "
        f"linked to something else: "
        f"{sorted({lt.get('TxnType') for ln in dl for lt in ln.get('LinkedTxn', [])} - {'Payment'})}; "
        f"with no LinkedTxn: {sum(1 for ln in dl if not ln.get('LinkedTxn'))}"
    )
    unlinked = next((ln for ln in dl if not ln.get("LinkedTxn")), None)
    if unlinked:
        show("    shape of a deposit line with no LinkedTxn:", unlinked, 6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
