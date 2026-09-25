"""``scripts/accounts_without_number.py``: the page's rule (newest version, not deleted,
active, no ``AcctNum``), grouped by ``AccountType``, no balance printed."""

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
import accounts_without_number  # noqa: E402


def account(account_id: str, name: str, account_type: str, **extra) -> dict:
    return {
        "Id": account_id,
        "Name": name.split(":")[-1],
        "FullyQualifiedName": name,
        "AccountType": account_type,
        "AccountSubType": extra.pop("sub_type", "OtherMiscellaneousExpense"),
        "Active": extra.pop("active", True),
        "SubAccount": ":" in name,
        "CurrentBalance": Decimal("9876.54"),
        **extra,
    }


@pytest.fixture
def tenant(fresh_tenant: uuid.UUID, rw_engine: Engine) -> uuid.UUID:
    with tenant_session(rw_engine, fresh_tenant) as db:
        c = Connection(tenant_id=fresh_tenant, system="qbo", status="connected")
        db.add(c)
        db.flush()
        run = start_sync_run(db, fresh_tenant, c.id, "backfill")
        origin = RawOrigin(sync_run_id=run.id)

        def put(payload: dict) -> None:
            store_raw(db, fresh_tenant, "qbo", "Account", payload["Id"], payload, origin)

        put(account("1", "Numbered", "Expense", AcctNum="6100"))
        put(account("2", "Blank number", "Expense", AcctNum="  "))
        put(account("3", "Payroll Liabilities:Federal", "Other Current Liability"))
        put(account("4", "Payroll Liabilities:State", "Other Current Liability"))
        put(account("5", "Inactive one", "Expense", active=False))
        # v1 had no number, v2 (newest) has one: not listed
        put(account("6", "Now numbered", "Bank"))
        put(account("6", "Now numbered", "Bank", AcctNum="1099"))
        # deleted: left out
        put(account("7", "Gone", "Bank"))
        store_delete(
            db, fresh_tenant, "qbo", "Account", "7", {"Id": "7", "status": "Deleted"}, origin
        )
    return fresh_tenant


def test_groups_the_active_unnumbered_accounts_by_type(
    tenant: uuid.UUID, rw_engine: Engine
) -> None:
    total, accounts = accounts_without_number.unnumbered_accounts(rw_engine, tenant)
    assert total == 5  # 1, 2, 3, 4, 6 are active; 5 is inactive, 7 deleted
    assert sorted(a.account_id for a in accounts) == ["2", "3", "4"]
    text = accounts_without_number.report(total, accounts)
    assert text.startswith("3 of 5 active accounts without a number")
    assert text.index("Other Current Liability: 2") < text.index("Expense: 1")
    assert "Payroll Liabilities:Federal  [OtherMiscellaneousExpense] (sub-account)" in text
    assert "9876.54" not in text
