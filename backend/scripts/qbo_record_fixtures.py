#!/usr/bin/env python
"""Record the connected sandbox company as test fixtures (F05).

    cd backend && .venv/bin/python scripts/qbo_record_fixtures.py --tenant qbo-sandbox

Writes ``tests/fixtures/qbo_sandbox/{Entity}.json`` (every record of each §6.2 entity,
paged by Id, inactive name-list rows included) and ``cdc_29_days.json`` (one Change Data
Capture response for all entities, 29 days back, which carries the deleted-record stubs).
The realm id is replaced everywhere by ``REALM_PLACEHOLDER``; no header, token or
company id is written. Amounts are written as JSON numbers exactly as Intuit sent them
(``canonical_json`` never passes through ``float``). Run by hand, never in CI; a run
makes about twenty metered reads. The company is Intuit's fictitious sample company.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.core.jsoncodec import canonical_json, json_loads
from app.ingest.models import Connection
from app.integrations.qbo.entities import ENTITIES, NAME_LISTS, SINGLETONS
from app.integrations.qbo.reader import CompanyReader
from app.tenancy.models import Tenant

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "qbo_sandbox"
REALM_PLACEHOLDER = "REALM"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", required=True, metavar="SLUG")
    args = ap.parse_args(argv)
    engine = create_app_engine()
    with untenanted_session(engine) as s:
        tenant_id = s.execute(select(Tenant.id).where(Tenant.slug == args.tenant)).scalar_one()
    with tenant_session(engine, tenant_id) as s:
        row = s.execute(select(Connection).where(Connection.system == "qbo")).scalar_one()
        if row.environment != "sandbox":
            print("fixtures are recorded from the sandbox only (D-25)", file=sys.stderr)
            return 2
        connection_id, realm = row.id, row.realm_id
    reader = CompanyReader(engine, tenant_id, connection_id)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    def write(name: str, obj) -> None:
        text = canonical_json(obj).replace(realm, REALM_PLACEHOLDER)
        json_loads(text)  # what was written reads back
        (FIXTURE_DIR / f"{name}.json").write_text(text + "\n")
        print(f"wrote {name}.json")

    for entity in ENTITIES:
        if (FIXTURE_DIR / f"{entity}.json").exists():
            continue  # a partial earlier run; delete the file to record it again
        if entity in SINGLETONS:
            if entity == "CompanyInfo":
                body = reader.get(f"companyinfo/{realm}", operation=entity)
                rows = [body["CompanyInfo"]]
            else:
                rows = reader.query(f"SELECT * FROM {entity}", operation=entity).get(entity, [])
        else:
            rows, after = [], "0"
            where = "Active IN (true, false) AND " if entity in NAME_LISTS else ""
            while True:
                statement = (
                    f"SELECT * FROM {entity} WHERE {where}Id > '{after}' ORDERBY Id MAXRESULTS 1000"
                )
                page = reader.query(statement, operation=entity).get(entity, [])
                rows += page
                if len(page) < 1000:
                    break
                after = page[-1]["Id"]
        write(entity, rows)
    since = (datetime.now(UTC) - timedelta(days=29)).replace(microsecond=0).isoformat()
    write(
        "cdc_29_days",
        reader.get("cdc", {"entities": ",".join(ENTITIES), "changedSince": since}, operation="CDC"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
