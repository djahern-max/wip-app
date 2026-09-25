"""``scripts/s01_q4.py`` (S-01 question 4): finds a Bill by vendor fragment and DocNumber
in the newest stored version only, reads ``CustomerRef`` per line, never prints an amount."""

import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.core.db import tenant_session
from app.ingest.models import Connection
from app.ingest.raw import RawOrigin, store_delete, store_raw
from app.ingest.sync_runs import start_sync_run

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import s01_q4  # noqa: E402


def bill(bill_id: str, vendor: str, doc: str, lines: list[dict]) -> dict:
    return {
        "Id": bill_id,
        "DocNumber": doc,
        "TxnDate": "2026-09-15",
        "VendorRef": {"value": "77", "name": vendor},
        "TotalAmt": Decimal("1234.56"),
        "Line": [
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": Decimal("100.25"),
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "88"}, **ln},
            }
            for ln in lines
        ],
    }


@pytest.fixture
def tenant(fresh_tenant: uuid.UUID, rw_engine: Engine) -> uuid.UUID:
    with tenant_session(rw_engine, fresh_tenant) as db:
        c = Connection(tenant_id=fresh_tenant, system="qbo", status="connected")
        db.add(c)
        db.flush()
        run = start_sync_run(db, fresh_tenant, c.id, "backfill")
        origin = RawOrigin(sync_run_id=run.id)
        job = {"CustomerRef": {"value": "412", "name": "6055424 Job - Site"}}
        # v1 has no refs; v2 (the newest) carries the job on line 1 only
        store_raw(
            db,
            fresh_tenant,
            "qbo",
            "Bill",
            "9001",
            bill("9001", "J&R Concrete Foundations LLC", "202698", [{}, {}]),
            origin,
        )
        store_raw(
            db,
            fresh_tenant,
            "qbo",
            "Bill",
            "9001",
            bill("9001", "J&R Concrete Foundations LLC", "202698", [job, {}]),
            origin,
        )
        # same DocNumber, another vendor: must not match the J&R fragment
        store_raw(
            db,
            fresh_tenant,
            "qbo",
            "Bill",
            "9002",
            bill("9002", "Other Vendor", "202698", [job]),
            origin,
        )
        # a deleted bill is left out
        store_raw(
            db,
            fresh_tenant,
            "qbo",
            "Bill",
            "9003",
            bill("9003", "Cut To Fit Co LLC", "1171", [job]),
            origin,
        )
        store_delete(
            db, fresh_tenant, "qbo", "Bill", "9003", {"Id": "9003", "status": "Deleted"}, origin
        )
    return fresh_tenant


def test_finds_the_newest_version_by_vendor_and_docnumber_and_reads_line_refs(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    wanted = [("j&r concrete", "202698"), ("Cut To Fit", "1171"), ("East Coast", "3919")]
    found = s01_q4.find_bills(rw_engine, tenant, wanted)
    (jr,) = found[wanted[0]]
    assert (jr.bill_id, jr.doc_number, len(jr.lines)) == ("9001", "202698", 2)
    assert [ln.has_customer for ln in jr.lines] == [True, False]
    assert (jr.lines[0].customer_value, jr.lines[0].customer_name) == ("412", "6055424 Job - Site")
    assert jr.lines[0].account == "88"
    assert found[wanted[1]] == []  # deleted
    assert found[wanted[2]] == []  # never stored

    text = s01_q4.report(found)
    assert "Bill Id 9001" in text and "CustomerRef on 1 of 2" in text
    assert "NOT IN raw_record" in text
    for amount in ("1234.56", "100.25"):
        assert amount not in text


def test_a_bill_argument_needs_vendor_and_docnumber() -> None:
    with pytest.raises(SystemExit):
        s01_q4.main(["--tenant", "x", "--bill", "no-separator"])
