#!/usr/bin/env python
"""S-01 question 4 (BLUEPRINT §13.7; F05.1): do Ramp-synced Bills carry the Customer/Job
on the line? Read-only, from the tenant's stored raw records; never calls Intuit.

    cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \\
        scripts/s01_q4.py --tenant rye-beach \\
        --bill "J&R Concrete:202698" --bill "Cut To Fit:1171" --bill "East Coast:3919"

Each ``--bill`` is ``<vendor name fragment>:<DocNumber>``; the fragment is matched
case-insensitively against ``VendorRef.name``. For every matching Bill (newest stored
version, not deleted) it prints the QuickBooks Bill ``Id``, ``TxnDate``, the line count
and, per line, the ``DetailType``, the account and whether
``AccountBasedExpenseLineDetail.CustomerRef`` is present, with its ``value`` and ``name``.
Amounts are never printed. Only the ids go into the repo (BLUEPRINT §13.7).
"""

import argparse
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, select

from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.ingest.raw import latest_raw_versions
from app.tenancy.models import Tenant


@dataclass(frozen=True)
class LineRef:
    detail_type: str | None
    account: str | None
    customer_value: str | None
    customer_name: str | None

    @property
    def has_customer(self) -> bool:
        return self.customer_value is not None


@dataclass(frozen=True)
class BillRefs:
    bill_id: str
    txn_date: str | None
    vendor_name: str | None
    doc_number: str | None
    lines: tuple[LineRef, ...]


def _ref(obj: dict | None, key: str) -> tuple[str | None, str | None]:
    ref = (obj or {}).get(key)
    if not isinstance(ref, dict):
        return None, None
    value = ref.get("value")
    name = ref.get("name")
    return (str(value) if value is not None else None, str(name) if name is not None else None)


def line_refs(payload: dict) -> tuple[LineRef, ...]:
    out: list[LineRef] = []
    for line in payload.get("Line") or []:
        if not isinstance(line, dict):
            continue
        detail_type = line.get("DetailType")
        detail = line.get("AccountBasedExpenseLineDetail")
        account_value, _ = _ref(detail, "AccountRef")
        customer_value, customer_name = _ref(detail, "CustomerRef")
        out.append(LineRef(detail_type, account_value, customer_value, customer_name))
    return tuple(out)


def matches(payload: dict, vendor_fragment: str, doc_number: str) -> bool:
    _, vendor_name = _ref(payload, "VendorRef")
    return (
        str(payload.get("DocNumber") or "").strip() == doc_number.strip()
        and vendor_fragment.lower() in (vendor_name or "").lower()
    )


def find_bills(
    engine: Engine, tenant_id: UUID, wanted: list[tuple[str, str]]
) -> dict[tuple[str, str], list[BillRefs]]:
    """``{(vendor fragment, DocNumber): [matching bills]}`` from the newest stored
    version of every Bill of the tenant, deleted records left out."""
    found: dict[tuple[str, str], list[BillRefs]] = {w: [] for w in wanted}
    with tenant_session(engine, tenant_id) as db:
        for rec in latest_raw_versions(db, tenant_id, "qbo", "Bill"):
            if rec.is_deleted or not isinstance(rec.payload, dict):
                continue
            payload = rec.payload
            for w in wanted:
                if matches(payload, *w):
                    _, vendor_name = _ref(payload, "VendorRef")
                    found[w].append(
                        BillRefs(
                            bill_id=str(payload.get("Id") or rec.external_id),
                            txn_date=payload.get("TxnDate"),
                            vendor_name=vendor_name,
                            doc_number=str(payload.get("DocNumber") or ""),
                            lines=line_refs(payload),
                        )
                    )
    return found


def report(found: dict[tuple[str, str], list[BillRefs]]) -> str:
    out: list[str] = []
    for (vendor, doc), bills in found.items():
        if not bills:
            out.append(f"{vendor} #{doc}: NOT IN raw_record (press Sync now once, then retry)")
            continue
        for b in bills:
            with_ref = sum(1 for ln in b.lines if ln.has_customer)
            out.append(
                f"{vendor} #{doc}: Bill Id {b.bill_id}, TxnDate {b.txn_date}, "
                f"{len(b.lines)} line(s), CustomerRef on {with_ref} of {len(b.lines)}"
            )
            for i, ln in enumerate(b.lines, 1):
                ref = (
                    f"CustomerRef value={ln.customer_value} name={ln.customer_name!r}"
                    if ln.has_customer
                    else "no CustomerRef"
                )
                out.append(f"  line {i}: {ln.detail_type}, account {ln.account}, {ref}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant", required=True, help="tenant slug")
    parser.add_argument(
        "--bill",
        action="append",
        required=True,
        metavar="VENDOR:DOCNUMBER",
        help="vendor name fragment and DocNumber; repeatable",
    )
    args = parser.parse_args(argv)
    wanted: list[tuple[str, str]] = []
    for item in args.bill:
        vendor, sep, doc = item.rpartition(":")
        if not sep or not vendor or not doc:
            parser.error(f"--bill wants VENDOR:DOCNUMBER, got {item!r}")
        wanted.append((vendor, doc))

    engine = create_app_engine()
    with untenanted_session(engine) as db:
        tenant_id = db.execute(
            select(Tenant.id).where(Tenant.slug == args.tenant)
        ).scalar_one_or_none()
    if tenant_id is None:
        print(f"no tenant with slug {args.tenant!r}", file=sys.stderr)
        return 2
    print(report(find_bills(engine, tenant_id, wanted)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
