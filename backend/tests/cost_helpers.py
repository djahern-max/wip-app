"""Helpers for the F10 tests: the constructed cost fixture (``tests/fixtures/rye_beach/cost``,
its README holds the hand-computed oracle), the account side the pure normalizer is told
about, and applying the fixture through the same ``apply_raw`` the worker uses."""

import csv
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, select

from app.core.db import tenant_session
from app.core.jsoncodec import json_loads
from app.domain.billing.sync import attach_account_ids
from app.domain.cost import sync as cost_sync
from app.domain.cost.models import LedgerLine
from app.ingest.models import RawRecord
from app.ingest.raw import latest_raw_versions, store_raw
from app.integrations.qbo.normalize.cost import AccountSide
from tests.billing_helpers import _origin

D = Decimal
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rye_beach" / "cost"
COST_ENTITIES = ("Bill", "VendorCredit", "Purchase", "JournalEntry", "Deposit")
REFERENCE = ("Account", "Vendor", "Item")
# QuickBooks account id → account number, as Account.json holds them.
ACCOUNT_NUMBERS = {
    "301": "5110",
    "302": "5210",
    "303": "5230",
    "304": "5240",
    "305": "5135",
    "306": "2110",
    "307": "2000",
    "308": "1010",
    "309": "2510",
}
EXPENSE_SIDE = frozenset({"301", "302", "303", "304", "305"})
ITEM_ACCOUNTS = {"601": "303", "602": None}
# The expense-side accounts and items, as the pure tests hand them to the normalizer.
SIDE = AccountSide(expense_side=EXPENSE_SIDE, item_accounts=ITEM_ACCOUNTS)


def fixture(name: str):
    return json_loads((FIXTURES / f"{name}.json").read_text())


def record(entity: str, external_id: str) -> dict:
    """One record of a list file, or the one record of a single-version file
    (``Bill_98893_assigned``)."""
    rows = fixture(entity)
    if isinstance(rows, dict):
        assert rows["Id"] == external_id
        return rows
    return next(r for r in rows if r["Id"] == external_id)


def oracle() -> list[dict]:
    with (FIXTURES / "cost_expected.csv").open() as fh:
        return list(csv.DictReader(fh))


def ledger_oracle() -> dict[tuple[str, str], dict[str, Decimal]]:
    """(month, account number or '') → the five parts and the total."""
    out = {}
    for r in oracle():
        if r["kind"] != "ledger":
            continue
        out[(r["month"], r["account_no"])] = {
            k: D(r[k]) for k in ("on_jobs", "memo", "unassigned", "labor", "outside", "total")
        }
    return out


def job_oracle(job: str) -> dict[str, Decimal]:
    return {r["slot"]: D(r["total"]) for r in oracle() if r["kind"] == "job" and r["job"] == job}


def store_fixture(engine: Engine, tenant_id: uuid.UUID) -> None:
    """Store every fixture record raw (reference records first) and attach the account ids
    to the chart by number, as F05.1 does."""
    with tenant_session(engine, tenant_id) as s:
        origin = _origin(s, tenant_id)
        for entity in (*REFERENCE, *COST_ENTITIES):
            for payload in fixture(entity):
                store_raw(s, tenant_id, "qbo", entity, payload["Id"], payload, origin)
        attach_account_ids(s, tenant_id)


def apply_all(engine: Engine, tenant_id: uuid.UUID) -> list[cost_sync.Outcome]:
    """Apply the latest raw version of every cost document, as the normalize task does."""
    out: list[cost_sync.Outcome] = []
    with tenant_session(engine, tenant_id) as s:
        lookup = cost_sync.load_lookup(s, tenant_id)
        for entity in COST_ENTITIES:
            for raw in latest_raw_versions(s, tenant_id, "qbo", entity):
                out.append(
                    cost_sync.apply_raw(s, tenant_id, raw, history_start=None, lookup=lookup)
                )
    return out


def apply_version(
    engine: Engine, tenant_id: uuid.UUID, entity: str, payload: dict, *, deleted: bool = False
) -> cost_sync.Outcome:
    """Store ``payload`` as the next raw version of its record and apply it."""
    with tenant_session(engine, tenant_id) as s:
        result = store_raw(
            s,
            tenant_id,
            "qbo",
            entity,
            payload["Id"],
            payload,
            _origin(s, tenant_id),
            deleted=deleted,
        )
        raw = result.record
        if raw is None:
            raw = next(
                r
                for r in latest_raw_versions(s, tenant_id, "qbo", entity)
                if r.external_id == payload["Id"]
            )
        return cost_sync.apply_raw(
            s, tenant_id, raw, history_start=None, lookup=cost_sync.load_lookup(s, tenant_id)
        )


def lines(engine: Engine, tenant_id: uuid.UUID) -> list[LedgerLine]:
    with tenant_session(engine, tenant_id) as s:
        rows = list(
            s.execute(
                select(LedgerLine).order_by(
                    LedgerLine.txn_date,
                    LedgerLine.txn_type,
                    LedgerLine.txn_external_id,
                    LedgerLine.line_no,
                )
            ).scalars()
        )
        for r in rows:
            s.expunge(r)
        return rows


def raw_versions(engine: Engine, tenant_id: uuid.UUID, entity: str, external_id: str) -> int:
    with tenant_session(engine, tenant_id) as s:
        return len(
            s.execute(
                select(RawRecord.id).where(
                    RawRecord.entity_type == entity, RawRecord.external_id == external_id
                )
            ).all()
        )
