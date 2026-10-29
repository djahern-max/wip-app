#!/usr/bin/env python
"""F10 Plan answer 2 (BLUEPRINT §13.7; ROADMAP F10 "with its receipt link"): where does a
Ramp-synced Bill carry the receipt link, if anywhere the platform holds? Read-only, from the
tenant's stored raw records; never calls Intuit. Run during the owner's pass; deleted at
that pass's close-out with its output kept in ``docs/briefs/F10.md``.

    cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \\
        scripts/cost_ramp_fields.py --tenant rye-beach --bill 98892 --bill 98908 --bill 98890

Each ``--bill`` is a QuickBooks Bill ``Id``. For each (newest stored version, not deleted)
it prints: whether ``PrivateNote`` is present and whether it carries a URL; per line,
whether ``Description`` is present and whether it carries a URL; whether an
``AttachableRef`` is present on the document or on a line (the Attachable entity, which the
platform does not fetch). No amount, vendor, customer or note text is printed: only
presence, lengths and URL hosts.
"""

import argparse
import re
import sys
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select

from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.ingest.raw import latest_raw
from app.tenancy.models import Tenant

URL = re.compile(r"https?://[^\s\"'<>]+")


def _hosts(text) -> list[str]:
    if not isinstance(text, str):
        return []
    return sorted({urlparse(m).netloc for m in URL.findall(text)})


def _tenant_id(engine, slug: str) -> UUID:
    with untenanted_session(engine) as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
        if tenant is None:
            sys.exit(f"no tenant with slug {slug!r}")
        return tenant.id


def describe(payload: dict) -> list[str]:
    out = []
    note = payload.get("PrivateNote")
    has_note = isinstance(note, str) and bool(note)
    detail = f", {len(note)} chars, url hosts {_hosts(note)}" if has_note else ""
    out.append(f"  PrivateNote: {'present' if has_note else 'absent'}{detail}")
    attached = "present" if payload.get("AttachableRef") else "absent"
    out.append(f"  AttachableRef on the document: {attached}")
    for i, line in enumerate(payload.get("Line") or [], start=1):
        if not isinstance(line, dict):
            continue
        desc = line.get("Description")
        words = (
            f"present, {len(desc)} chars, url hosts {_hosts(desc)}"
            if isinstance(desc, str) and desc
            else "absent"
        )
        out.append(
            f"  line {i} ({line.get('DetailType')}): Description {words}; "
            f"AttachableRef {'present' if line.get('AttachableRef') else 'absent'}"
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant", required=True, help="tenant slug")
    parser.add_argument("--bill", action="append", required=True, help="QuickBooks Bill Id")
    args = parser.parse_args()
    engine = create_app_engine()
    tenant_id = _tenant_id(engine, args.tenant)
    with tenant_session(engine, tenant_id) as db:
        for bill_id in args.bill:
            raw = latest_raw(db, tenant_id, "qbo", "Bill", bill_id)
            if raw is None or raw.is_deleted or not isinstance(raw.payload, dict):
                print(f"Bill {bill_id}: not held (or deleted)")
                continue
            print(f"Bill {bill_id}: TxnDate {raw.payload.get('TxnDate')}, version {raw.version}")
            print("\n".join(describe(raw.payload)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
