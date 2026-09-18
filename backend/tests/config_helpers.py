"""Helpers shared by the F04 tests: the Rye Beach fixtures, loading rules for a
tenant, uploading a chart through the API and running the worker until quiet."""

import csv
import io
import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.core.db import tenant_session
from app.domain.config.audit import Actor
from app.domain.config.models import AccountMap, CostCategory, Division, GlAccount
from app.domain.config.service import load_suggest_rules
from app.ingest.models import ImportBatch
from app.worker.runner import Worker
from tests.conftest import CSRF

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rye_beach"
CHART_CSV = FIXTURES / "chart_of_accounts.csv"
RULES_JSON = FIXTURES / "account_suggest_rules.json"
EXPECTED_CSV = FIXTURES / "account_map_expected.csv"


def rules_spec() -> dict:
    return json.loads(RULES_JSON.read_text())


def expected_rows() -> dict[str, dict]:
    with EXPECTED_CSV.open() as fh:
        return {r["account_no"]: r for r in csv.DictReader(fh)}


def load_rye_beach_rules(engine: Engine, tenant_id: uuid.UUID) -> int:
    with tenant_session(engine, tenant_id) as s:
        return load_suggest_rules(s, tenant_id, rules_spec(), Actor())


def upload_chart(client: TestClient, content: bytes, filename: str = "chart.csv") -> dict:
    ctype = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if filename.endswith(".xlsx")
        else "text/csv"
    )
    r = client.post(
        "/api/imports",
        data={"source_kind": "chart_of_accounts"},
        files={"file": (filename, io.BytesIO(content), ctype)},
        headers=CSRF,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def run_until_quiet(engine: Engine, name: str = "w-config") -> int:
    w = Worker(engine, name=name, listen=False, poll_seconds=0.01)
    total = 0
    while (n := w.run_once()) > 0:
        total += n
    return total


def batch_json(client: TestClient, batch_id: str) -> dict:
    r = client.get(f"/api/imports/{batch_id}")
    assert r.status_code == 200, r.text
    return r.json()


def account_state(engine: Engine, tenant_id: uuid.UUID) -> dict[str, dict]:
    """account_no → {name, active, division, cost_category, in_job_cost, status, rule}"""
    with tenant_session(engine, tenant_id) as s:
        divisions = {d.id: d.code for d in s.execute(select(Division)).scalars()}
        categories = {c.id: c.name for c in s.execute(select(CostCategory)).scalars()}
        maps = {m.gl_account_id: m for m in s.execute(select(AccountMap)).scalars()}
        out = {}
        for a in s.execute(select(GlAccount)).scalars():
            m = maps.get(a.id)
            out[a.account_no] = {
                "name": a.name,
                "ledger_type": a.ledger_type,
                "active": a.active,
                "division": divisions.get(m.division_id, "") if m and m.division_id else "",
                "cost_category": categories.get(m.cost_category_id, "")
                if m and m.cost_category_id
                else "",
                "in_job_cost": (str(m.in_job_cost).lower() if m else None),
                "status": m.status if m else None,
                "rule": m.suggested_by_rule if m else None,
            }
        return out


def chart_batches(engine: Engine, tenant_id: uuid.UUID) -> list[ImportBatch]:
    with tenant_session(engine, tenant_id) as s:
        rows = (
            s.execute(select(ImportBatch).where(ImportBatch.source_kind == "chart_of_accounts"))
            .scalars()
            .all()
        )
        for r in rows:
            s.expunge(r)
        return rows
