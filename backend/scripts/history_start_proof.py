#!/usr/bin/env python
"""The F05.3 step-1 proof (D-48): does QuickBooks accept the history-start query
grammar, and does its count agree with the pages? For each transactional entity the
script runs the page query the backfill will send, ``SELECT * FROM {entity} WHERE
TxnDate >= '{start}' AND Id > '{after}' ORDERBY Id MAXRESULTS 1000``, walking pages by
Id until a short page, and the count query the nightly check will send, ``SELECT
COUNT(*) FROM {entity} WHERE TxnDate >= '{start}'``. Read-only: nothing is stored,
nothing in QuickBooks changes. It prints, per entity, whether the grammar was accepted,
the page total (and how many pages it took) and the count, and whether they match.
No amount, document number or name is read from a payload or printed.

    cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \\
        scripts/history_start_proof.py --tenant rye-beach --history-start YYYY-MM-DD

Before F05.3 is deployed the file is copied to ``/tmp`` on the droplet and run from
there with ``PYTHONPATH=/opt/wip/backend`` (the checkout must stay clean for deploy.sh).

The date is an argument on purpose: the history start is data per company (D-48),
never a value in code.
"""

import argparse
import sys
from datetime import date
from uuid import UUID

from sqlalchemy import Engine, select

from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.ingest.models import Connection
from app.integrations.qbo import SYSTEM, client
from app.integrations.qbo.entities import ENTITIES
from app.integrations.qbo.reader import CompanyReader
from app.tenancy.models import Tenant

PAGE_SIZE = 1000
# The same split as ``entities.REFERENCE`` / ``TRANSACTIONAL``, spelled out here so the
# proof runs on a checkout deployed before F05.3 (copied to the droplet by hand).
REFERENCE = frozenset({"Account", "Customer", "Vendor", "Item", "Preferences", "CompanyInfo"})
TRANSACTIONAL = tuple(e for e in ENTITIES if e not in REFERENCE)


def page_total(reader: CompanyReader, entity: str, start: date) -> tuple[int, int]:
    """``(records, pages)`` walking the pages by Id with the TxnDate clause."""
    records = pages = 0
    after = "0"
    while True:
        statement = (
            f"SELECT * FROM {entity} WHERE TxnDate >= '{start.isoformat()}' AND Id > '{after}' "
            f"ORDERBY Id MAXRESULTS {PAGE_SIZE}"
        )
        rows = reader.query(statement, operation=entity).get(entity, [])
        rows = [r for r in rows if isinstance(r, dict)]
        pages += 1
        records += len(rows)
        if len(rows) < PAGE_SIZE:
            return records, pages
        after = str(rows[-1].get("Id"))


def count(reader: CompanyReader, entity: str, start: date) -> int | None:
    body = reader.query(
        f"SELECT COUNT(*) FROM {entity} WHERE TxnDate >= '{start.isoformat()}'",
        operation=f"{entity} count",
    )
    total = body.get("totalCount")
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant", required=True, help="tenant slug")
    parser.add_argument(
        "--history-start", required=True, type=date.fromisoformat, help="YYYY-MM-DD"
    )
    args = parser.parse_args(argv)
    engine: Engine = create_app_engine()
    with untenanted_session(engine) as db:
        tenant_id: UUID | None = db.execute(
            select(Tenant.id).where(Tenant.slug == args.tenant)
        ).scalar_one_or_none()
    if tenant_id is None:
        print(f"no tenant with slug {args.tenant!r}", file=sys.stderr)
        return 2
    with tenant_session(engine, tenant_id) as db:
        connection = db.execute(
            select(Connection).where(Connection.tenant_id == tenant_id, Connection.system == SYSTEM)
        ).scalar_one_or_none()
        if connection is None or connection.status != "connected":
            print("QuickBooks is not connected for this tenant", file=sys.stderr)
            return 2
        connection_id = connection.id
    reader = CompanyReader(engine, tenant_id, connection_id)
    print(f"history start {args.history_start.isoformat()}; {len(TRANSACTIONAL)} entities")
    failed = 0
    for entity in TRANSACTIONAL:
        try:
            records, pages = page_total(reader, entity, args.history_start)
            total = count(reader, entity, args.history_start)
        except client.QboError as exc:
            failed += 1
            print(f"{entity}: grammar refused ({exc.code}; intuit_tid {exc.tid})")
            continue
        verdict = "matched" if total == records else "DID NOT MATCH"
        print(
            f"{entity}: grammar accepted; page total {records} ({pages} page"
            f"{'' if pages == 1 else 's'}); count {total}; {verdict}"
        )
        if total != records:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
