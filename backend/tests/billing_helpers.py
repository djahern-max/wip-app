"""Helpers for the F08 tests: QuickBooks payloads in the sandbox shape (an invoice, a
credit memo, a sales receipt, a payment, an item), stored raw and applied through the
same ``apply_raw`` the sync uses, so lines, applications, the void rule and deletes run
exactly as they do in production. No real name or amount: the figures come from the
briefs and decisions (D-02's 67 Elm Street case, D-39's constructed invoice)."""

import uuid
from decimal import Decimal

from sqlalchemy import Engine, select

from app.core.db import tenant_session
from app.domain.billing.sync import apply_raw
from app.domain.config.policy import set_policy
from app.ingest.models import Connection, SyncRun
from app.ingest.raw import RawOrigin, latest_raw_versions, store_raw
from tests.conftest import Seed
from tests.job_helpers import actor

D = Decimal
DEPOSIT_ITEM, FUEL_ITEM, WORK_ITEM = "901", "902", "903"
ITEMS = {
    DEPOSIT_ITEM: "Customer deposit",
    FUEL_ITEM: "Fuel surcharge (EX)",
    WORK_ITEM: "Site work",
}


def _origin(s, tenant_id: uuid.UUID) -> RawOrigin:
    run_id = s.execute(select(SyncRun.id).where(SyncRun.tenant_id == tenant_id)).scalars().first()
    if run_id is None:
        conn = Connection(tenant_id=tenant_id, system="f08-test", status="disconnected")
        s.add(conn)
        s.flush()
        run = SyncRun(tenant_id=tenant_id, connection_id=conn.id, kind="backfill")
        s.add(run)
        s.flush()
        run_id = run.id
    return RawOrigin(sync_run_id=run_id)


def _num(value: str):
    return D(value)


def item_payload(item_id: str, name: str, *, active: bool = True) -> dict:
    return {
        "Id": item_id,
        "Name": name,
        "Type": "Service",
        "Active": active,
        "IncomeAccountRef": {"value": "79"},
    }


def line(amount: str, item: str, description: str = "Work", line_id: str | None = None) -> dict:
    return {
        "Id": line_id or str(uuid.uuid4().int % 100000),
        "DetailType": "SalesItemLineDetail",
        "Amount": _num(amount),
        "Description": description,
        "SalesItemLineDetail": {"ItemRef": {"value": item, "name": ITEMS.get(item, item)}},
    }


def document_payload(
    doc_id: str,
    *,
    customer: str,
    date: str,
    lines: list[dict],
    doc_number: str | None = None,
    tax: str = "0",
    balance: str | None = None,
    private_note: str | None = None,
    total: str | None = None,
) -> dict:
    subtotal = sum((D(str(ln["Amount"])) for ln in lines), D("0"))
    full = D(total) if total is not None else subtotal + D(tax)
    payload = {
        "Id": doc_id,
        "TxnDate": date,
        "CustomerRef": {"value": customer},
        "Line": [*lines, {"DetailType": "SubTotalLineDetail", "Amount": subtotal}],
        "TxnTaxDetail": {"TotalTax": _num(tax)},
        "TotalAmt": full,
        "Balance": D(balance) if balance is not None else full,
    }
    if doc_number is not None:
        payload["DocNumber"] = doc_number
    if private_note is not None:
        payload["PrivateNote"] = private_note
    return payload


def voided_payload(payload: dict) -> dict:
    """The same document after a void in QuickBooks: total and balance 0, the note."""
    out = dict(payload)
    out["TotalAmt"] = D("0")
    out["Balance"] = D("0")
    out["TxnTaxDetail"] = {"TotalTax": D("0")}
    out["Line"] = [{**ln, "Amount": D("0")} for ln in payload["Line"]]
    out["PrivateNote"] = "Voided"
    return out


def payment_payload(
    payment_id: str,
    *,
    customer: str,
    date: str,
    total: str,
    unapplied: str = "0",
    applied: list[tuple[str, str, str]] | None = None,  # (amount, TxnType, TxnId)
) -> dict:
    return {
        "Id": payment_id,
        "TxnDate": date,
        "CustomerRef": {"value": customer},
        "TotalAmt": _num(total),
        "UnappliedAmt": _num(unapplied),
        "Line": [
            {"Amount": _num(amount), "LinkedTxn": [{"TxnId": txn_id, "TxnType": txn_type}]}
            for amount, txn_type, txn_id in (applied or [])
        ],
    }


def apply_payloads(
    engine: Engine, tenant_id: uuid.UUID, payloads: list[tuple[str, dict]], *, deleted: bool = False
) -> None:
    """Store each (entity, payload) as the next raw version and apply it."""
    with tenant_session(engine, tenant_id) as s:
        origin = _origin(s, tenant_id)
        for entity, payload in payloads:
            result = store_raw(
                s, tenant_id, "qbo", entity, payload["Id"], payload, origin, deleted=deleted
            )
            raw = result.record
            if raw is None:
                raw = next(
                    r
                    for r in latest_raw_versions(s, tenant_id, "qbo", entity)
                    if r.external_id == payload["Id"]
                )
            if entity != "Item":
                outcome = apply_raw(s, tenant_id, raw)
                assert outcome.result in ("applied", "deleted"), outcome


def seed_items(engine: Engine, tenant_id: uuid.UUID, items: dict[str, str] | None = None) -> None:
    apply_payloads(
        engine, tenant_id, [("Item", item_payload(i, n)) for i, n in (items or ITEMS).items()]
    )


def billing_policy(
    engine: Engine,
    seed: Seed,
    tenant_id: uuid.UUID,
    *,
    deposit: list[str] | None = None,
    surcharge: list[str] | None = None,
    rate: str | None = "0.0500",
) -> None:
    """The two F08 keys as Rye Beach would set them: one deposit item, one fuel item."""
    seed_items(engine, tenant_id)
    with tenant_session(engine, tenant_id) as s:
        set_policy(
            s,
            tenant_id,
            "deposit_identification",
            {"item_ids": deposit if deposit is not None else [DEPOSIT_ITEM]},
            decision_ref="D-02",
            actor=actor(seed),
        )
        set_policy(
            s,
            tenant_id,
            "fuel_surcharge_treatment",
            {"item_ids": surcharge if surcharge is not None else [FUEL_ITEM], "rate": rate},
            decision_ref="D-39",
            actor=actor(seed),
        )
