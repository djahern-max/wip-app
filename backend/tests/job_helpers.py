"""Helpers shared by the F07 tests: a tenant configured as Rye Beach is on production
(grid, D-05 basis, time zone, the D-34 burden rates), the synthetic QuickBooks
customers the brief names (built from rows, not a file; no real names), the three
estimate fixtures loaded in production order (the two template files, then the
80-row sheet; owner's answer 12), and small API helpers."""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.audit.models import AuditLog
from app.core.db import tenant_session
from app.domain.billing.models import Billing, Customer, Payment
from app.domain.config.audit import Actor
from app.domain.config.burden import add_burden_rate, deactivate_burden_rate
from app.domain.config.models import Division
from app.domain.config.policy import set_policy
from app.ingest.models import Connection, RawRecord, SyncRun
from tests.config_helpers import run_until_quiet
from tests.conftest import CSRF, Seed
from tests.estimate_helpers import D04_BASIS, EIGHTY, ELM, TURLEY, configure_tenant, upload_template

D = Decimal
D05_BASIS = [*D04_BASIS, "20"]
RATES = {"LS": "0.2136", "EX": "0.1959", "GC": "0.2207", "SNOW": "0.2061"}  # D-34
JAN1 = date(2026, 1, 1)
MONEY = re.compile(r"^-?\d+\.\d\d$")

ELM_ID, TURLEY_ID, TURLEY_HESS_ID = "EST6115758", "EST6120638", "EST6366990"
DEVELLIS_ID, DEVELLIS_ADDON_ID = "EST6346291", "EST6281138"

# key → (external id, display name, parent key, is_project, active)
CUSTOMERS: dict[str, tuple[str, str, str | None, bool, bool]] = {
    "c05": ("101", "Client 05", None, False, True),
    "c26": ("102", "Client 26", None, False, True),
    "c23": ("103", "Client 23", None, False, True),
    "pool_customer": ("104", "Pool - Hydroseed", None, False, True),
    "elm": ("201", "6115758 Client 05 - 67 Elm St Parking Lot", "c05", True, True),
    "turley": ("202", "EST6120638 Client 26 - Landscape Projects 2026", "c26", True, True),
    "dunbarton": ("203", "Client 26 - 378 E Dunbarton Rd", "c26", True, True),
    "ocean": ("204", "1701 Ocean Boulevard", "c23", True, True),
    "pool": ("205", "Pool - Hydroseed", "pool_customer", True, True),
    "old": ("206", "Old", "c26", False, True),  # the sub-customer Client 26:Old
    "inactive": ("207", "6115758 Client 05 - Parking Lot (old)", "c05", True, False),
}


def actor(seed: Seed) -> Actor:
    return Actor(user_id=seed.users["rotate_me"].id)


def policy(engine: Engine, seed: Seed, tenant_id: uuid.UUID, key: str, value) -> None:
    with tenant_session(engine, tenant_id) as s:
        set_policy(s, tenant_id, key, value, decision_ref="F07 (test)", actor=actor(seed))


def divisions(engine: Engine, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    with tenant_session(engine, tenant_id) as s:
        return {d.code: d.id for d in s.execute(select(Division)).scalars()}


def seed_rates(engine: Engine, seed: Seed, tenant_id: uuid.UUID) -> None:
    """D-34: one deactivated EX row, the four active worksheet rows from 2026-01-01."""
    codes = divisions(engine, tenant_id)
    with tenant_session(engine, tenant_id) as s:
        old = add_burden_rate(
            s,
            tenant_id,
            division_id=codes["EX"],
            effective_from=JAN1,
            effective_to=None,
            rate=D("0.1945"),
            basis_note="25-26 audit (test)",
            actor=actor(seed),
        )
        deactivate_burden_rate(s, tenant_id, old.id, actor(seed))
        for code, rate in RATES.items():
            add_burden_rate(
                s,
                tenant_id,
                division_id=codes[code],
                effective_from=JAN1,
                effective_to=None,
                rate=D(rate),
                basis_note="worksheet 2026-09-27 (test)",
                actor=actor(seed),
            )


def _raw(s, tenant_id: uuid.UUID) -> uuid.UUID:
    marker = uuid.uuid4().hex[:12]
    conn = Connection(tenant_id=tenant_id, system=f"f07-{marker}", status="disconnected")
    s.add(conn)
    s.flush()
    run = SyncRun(tenant_id=tenant_id, connection_id=conn.id, kind="backfill")
    s.add(run)
    s.flush()
    raw = RawRecord(
        tenant_id=tenant_id,
        source="qbo",
        entity_type="Customer",
        external_id=marker,
        version=1,
        payload={"test": "F07 synthetic customers"},
        payload_sha256=uuid.uuid4().hex * 2,
        sync_run_id=run.id,
    )
    s.add(raw)
    s.flush()
    return raw.id


def seed_customers(
    engine: Engine,
    tenant_id: uuid.UUID,
    rows: dict[str, tuple[str, str, str | None, bool, bool]] | None = None,
) -> dict[str, dict]:
    """The customers as rows (parents first), one billing row on the 1701 Ocean
    project. Returns key → {"id", "external_id", "display_name"}."""
    rows = rows or CUSTOMERS
    out: dict[str, dict] = {}
    with tenant_session(engine, tenant_id) as s:
        raw_id = _raw(s, tenant_id)
        ordered = sorted(rows.items(), key=lambda kv: kv[1][2] is not None)
        for key, (ext, name, parent, project, active) in ordered:
            parent_row = out.get(parent) if parent else None
            c = Customer(
                tenant_id=tenant_id,
                source="qbo",
                external_id=ext,
                display_name=name,
                parent_external_id=parent_row["external_id"] if parent_row else None,
                parent_customer_id=parent_row["id"] if parent_row else None,
                is_project=project,
                active=active,
                raw_record_id=raw_id,
            )
            s.add(c)
            s.flush()
            out[key] = {"id": c.id, "external_id": ext, "display_name": name}
        if "ocean" in out:
            add_billing(s, tenant_id, raw_id, out["ocean"])
    return out


def add_billing(s, tenant_id: uuid.UUID, raw_id: uuid.UUID, customer: dict) -> None:
    ext = uuid.uuid4().hex[:12]
    s.add(
        Billing(
            tenant_id=tenant_id,
            kind="invoice",
            source="qbo",
            external_id=ext,
            txn_date=date(2026, 8, 31),
            customer_external_id=customer["external_id"],
            customer_id=customer["id"],
            subtotal=D("1000.00"),
            discount_total=D("0.00"),
            tax_total=D("0.00"),
            total=D("1000.00"),
            balance=D("1000.00"),
            voided=False,
            raw_record_id=raw_id,
        )
    )
    s.flush()


def add_payment(engine: Engine, tenant_id: uuid.UUID, customer: dict) -> None:
    with tenant_session(engine, tenant_id) as s:
        raw_id = _raw(s, tenant_id)
        s.add(
            Payment(
                tenant_id=tenant_id,
                kind="payment",
                source="qbo",
                external_id=uuid.uuid4().hex[:12],
                txn_date=date(2026, 8, 31),
                customer_external_id=customer["external_id"],
                customer_id=customer["id"],
                total=D("10.00"),
                unapplied_amount=D("0.00"),
                raw_record_id=raw_id,
            )
        )


@dataclass
class Tenant:
    client: TestClient
    id: uuid.UUID
    engine: Engine
    seed: Seed
    customers: dict[str, dict]
    divisions: dict[str, uuid.UUID]

    # --- API helpers ------------------------------------------------------------------

    def get(self, path: str) -> dict:
        r = self.client.get(path)
        assert r.status_code == 200, r.text
        return r.json()

    def send(self, method: str, path: str, body: dict | None = None, status: int = 200):
        r = self.client.request(method, path, json=body, headers=CSRF)
        assert r.status_code == status, r.text
        return r.json()

    def queue(self) -> dict[str, dict]:
        return {e["external_id"]: e for e in self.get("/api/jobs/review")["entries"]}

    def estimate_id(self, external_id: str) -> str:
        rows = self.get("/api/estimates")["estimates"]
        return next(r["id"] for r in rows if r["external_id"] == external_id)

    def new_job(self, external_id: str, division: str | None = None, status: int = 201) -> dict:
        entry = self.queue()[external_id]
        division_id = str(self.divisions[division]) if division else entry["division_suggestion_id"]
        return self.send(
            "POST",
            "/api/jobs",
            {"estimate_id": entry["estimate_id"], "division_id": division_id},
            status,
        )

    def job(self, job_id: str) -> dict:
        return self.get(f"/api/jobs/{job_id}")

    def link(self, job_id: str, key: str, status: int = 200, in_progress: bool = False) -> dict:
        body = {
            "system": "qbo_customer",
            "external_id": self.customers[key]["external_id"],
            "set_in_progress": in_progress,
        }
        return self.send("POST", f"/api/jobs/{job_id}/aliases", body, status)

    def audit_rows(self) -> int:
        with tenant_session(self.engine, self.id) as s:
            return s.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == self.id)
            ).scalar_one()

    def audit_actions(self, since: int) -> list[AuditLog]:
        with tenant_session(self.engine, self.id) as s:
            rows = list(
                s.execute(
                    select(AuditLog)
                    .where(AuditLog.tenant_id == self.id)
                    .order_by(AuditLog.occurred_at, AuditLog.id)
                ).scalars()
            )
            for r in rows:
                s.expunge(r)
        return rows[since:]


def load_estimates(t: Tenant) -> None:
    """Production order (owner's answer 12): the two template files, then the sheet."""
    upload_template(t.client, ELM.read_bytes(), "elm.xlsx")
    upload_template(t.client, TURLEY.read_bytes(), "turley.xlsx")
    run_until_quiet(t.engine)
    upload_template(t.client, EIGHTY.read_bytes(), "estimates_2026-09-17.xlsx")
    run_until_quiet(t.engine)


def make_tenant(
    seed: Seed,
    rw_engine: Engine,
    login_as: Callable[..., TestClient],
    tenant_id: uuid.UUID,
    *,
    load: bool = True,
) -> Tenant:
    configure_tenant(rw_engine, seed, tenant_id, basis=False)
    policy(rw_engine, seed, tenant_id, "wip_basis", D05_BASIS)
    policy(rw_engine, seed, tenant_id, "timezone", "America/New_York")
    seed_rates(rw_engine, seed, tenant_id)
    t = Tenant(
        client=login_as("rotate_me", tenant=tenant_id),
        id=tenant_id,
        engine=rw_engine,
        seed=seed,
        customers=seed_customers(rw_engine, tenant_id),
        divisions=divisions(rw_engine, tenant_id),
    )
    if load:
        load_estimates(t)
    return t


def money_values(obj, path: str = "$") -> list[tuple[str, object]]:
    """Every value under a money-named key anywhere in a body."""
    keys = {"price", "revised_contract", "unapproved_change_orders", "eac_in_basis"}
    out: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                out.append((f"{path}.{k}", v))
            out.extend(money_values(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(money_values(v, f"{path}[{i}]"))
    return out
