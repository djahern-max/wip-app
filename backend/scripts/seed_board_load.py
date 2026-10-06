#!/usr/bin/env python
"""F08.2 item 2: seed a development database to the size of the problem. Adds
synthetic invoices (one line each, 100.00, paid in full) and their payments on one
untracked customer row of a tenant of its own, ``load-test``, spread over 308 months
(2001-01 to 2026-08, the span of a company with history back to 2000), so the Jobs
page, a job page, the exports and Connections can be timed against a tenant that
holds as many rows as a real one (``scripts/collected_tieout.py --counts`` says how
many).

    cd backend && .venv/bin/python scripts/seed_board_load.py --documents 10000

Development only: it refuses a database whose name is not ``wip`` or ``wip_test`` and
a slug in ``PROTECTED_TENANT_SLUGS``. Running it again adds more rows to the same
tenant. Nothing synthetic lands on a real tenant; no figure is stored (the rows are
F05 rows, read by the board like any other).
"""

import argparse
import sys
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.engine import make_url

from app.auth import admin
from app.core.audit import RequestMeta
from app.core.config import get_settings, protected_tenant_slug_set
from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.domain.billing.models import Billing, BillingLine, Customer, Payment, PaymentApplication
from app.ingest.models import Connection, RawRecord, SyncRun
from app.tenancy.models import Tenant

ALLOWED_DATABASES = frozenset({"wip", "wip_test"})
DEFAULT_SLUG = "load-test"
MONTHS = [(y, m) for y in range(2001, 2027) for m in range(1, 13)][:308]
AMOUNT = Decimal("100.00")
CHUNK = 500
META = RequestMeta(ip=None, request_id="scripts/seed_board_load.py")


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Seeded:
    tenant_id: UUID
    customer_external_id: str
    documents: int
    payments: int


def check_database_name(url: str) -> str:
    name = make_url(url).database or ""
    if name not in ALLOWED_DATABASES:
        raise Refused(
            f"refusing: database {name!r} is not a development database "
            f"({', '.join(sorted(ALLOWED_DATABASES))})"
        )
    return name


def _tenant_id(engine: Engine, slug: str, firm_id: UUID | None) -> UUID:
    if slug in protected_tenant_slug_set(get_settings()):
        raise Refused(f"refusing: tenant {slug!r} is listed in PROTECTED_TENANT_SLUGS")
    with untenanted_session(engine) as db:
        existing = db.execute(select(Tenant.id).where(Tenant.slug == slug)).scalar_one_or_none()
        if existing is not None:
            return existing
        if firm_id is None:
            firm_id = admin.only_firm_id(db)  # the deployment's one firm (D-17)
    return admin.create_tenant_and_seed(
        engine, None, name="Load test", slug=slug, meta=META, firm_id=firm_id, via="cli"
    ).id


def seed(engine: Engine, slug: str, documents: int, *, firm_id: UUID | None = None) -> Seeded:
    tenant_id = _tenant_id(engine, slug, firm_id)
    marker = uuid.uuid4().hex[:10]
    with tenant_session(engine, tenant_id) as db:
        conn = Connection(tenant_id=tenant_id, system=f"load-test-{marker}", status="disconnected")
        db.add(conn)
        db.flush()
        run = SyncRun(tenant_id=tenant_id, connection_id=conn.id, kind="backfill")
        db.add(run)
        db.flush()
        raw = RawRecord(
            tenant_id=tenant_id,
            source="qbo",
            entity_type="Customer",
            external_id=f"LT{marker}",
            version=1,
            payload={"load_test": True},
            payload_sha256=uuid.uuid4().hex * 2,
            sync_run_id=run.id,
        )
        db.add(raw)
        db.flush()
        customer = Customer(
            tenant_id=tenant_id,
            source="qbo",
            external_id=f"LT{marker}",
            display_name=f"Load test customer {marker}",
            is_project=False,
            active=True,
            raw_record_id=raw.id,
        )
        db.add(customer)
        db.flush()
        customer_id, customer_external_id = customer.id, customer.external_id
        for start in range(0, documents, CHUNK):
            for i in range(start, min(start + CHUNK, documents)):
                y, m = MONTHS[i % len(MONTHS)]
                when = date(y, m, 15)
                doc_ext = f"LT{marker}-{i}"
                billing = Billing(
                    tenant_id=tenant_id,
                    kind="invoice",
                    source="qbo",
                    external_id=doc_ext,
                    doc_number=doc_ext,
                    txn_date=when,
                    customer_external_id=customer_external_id,
                    customer_id=customer_id,
                    subtotal=AMOUNT,
                    discount_total=Decimal("0.00"),
                    tax_total=Decimal("0.00"),
                    total=AMOUNT,
                    balance=Decimal("0.00"),
                    voided=False,
                    raw_record_id=raw.id,
                )
                db.add(billing)
                db.flush()
                db.add(
                    BillingLine(
                        tenant_id=tenant_id,
                        billing_id=billing.id,
                        line_no=1,
                        line_kind="SalesItemLineDetail",
                        description="Load test",
                        item_external_id="1",
                        amount=AMOUNT,
                    )
                )
                payment = Payment(
                    tenant_id=tenant_id,
                    kind="payment",
                    source="qbo",
                    external_id=f"LTP{marker}-{i}",
                    txn_date=when,
                    customer_external_id=customer_external_id,
                    customer_id=customer_id,
                    total=AMOUNT,
                    unapplied_amount=Decimal("0.00"),
                    raw_record_id=raw.id,
                )
                db.add(payment)
                db.flush()
                db.add(
                    PaymentApplication(
                        tenant_id=tenant_id,
                        payment_id=payment.id,
                        line_no=1,
                        linked_txn_type="Invoice",
                        linked_txn_external_id=doc_ext,
                        billing_id=billing.id,
                        amount=AMOUNT,
                    )
                )
            db.flush()
    return Seeded(tenant_id, customer_external_id, documents, documents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--documents", type=int, required=True, help="invoices (and payments)")
    parser.add_argument(
        "--slug", default=DEFAULT_SLUG, help=f"tenant slug (default {DEFAULT_SLUG})"
    )
    args = parser.parse_args(argv)
    try:
        check_database_name(get_settings().database_url)
        seeded = seed(create_app_engine(), args.slug, args.documents)
    except Refused as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        f"tenant {args.slug} ({seeded.tenant_id}): added {seeded.documents} invoices and "
        f"{seeded.payments} payments on untracked customer {seeded.customer_external_id}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
