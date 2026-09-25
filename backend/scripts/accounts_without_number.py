#!/usr/bin/env python
"""The active QuickBooks accounts that carry no account number, grouped by
``AccountType`` (F05.1, the "Accounts without a number" attention line). Read-only,
from the tenant's stored raw ``Account`` records; never calls Intuit; prints no balance.

    cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \\
        scripts/accounts_without_number.py --tenant rye-beach

Per group: the count, then one line per account: QuickBooks ``Id``, ``FullyQualifiedName``
(``Parent:Child`` for a sub-account), ``AccountSubType``. The same rule as the page:
newest stored version, not deleted, ``Active`` not false, ``AcctNum`` missing or blank.
"""

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, select

from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.ingest.raw import latest_raw_versions
from app.tenancy.models import Tenant


@dataclass(frozen=True)
class Unnumbered:
    account_id: str
    name: str
    account_type: str
    sub_type: str | None
    sub_account: bool


def unnumbered_accounts(engine: Engine, tenant_id: UUID) -> tuple[int, list[Unnumbered]]:
    """``(active total, the active accounts without a number)``."""
    total = 0
    out: list[Unnumbered] = []
    with tenant_session(engine, tenant_id) as db:
        for rec in latest_raw_versions(db, tenant_id, "qbo", "Account"):
            if rec.is_deleted or not isinstance(rec.payload, dict):
                continue
            p = rec.payload
            if p.get("Active") is False:
                continue
            total += 1
            number = p.get("AcctNum")
            if isinstance(number, str) and number.strip():
                continue
            out.append(
                Unnumbered(
                    account_id=str(p.get("Id") or rec.external_id),
                    name=str(p.get("FullyQualifiedName") or p.get("Name") or ""),
                    account_type=str(p.get("AccountType") or "(no AccountType)"),
                    sub_type=p.get("AccountSubType"),
                    sub_account=bool(p.get("SubAccount")),
                )
            )
    return total, out


def report(total: int, accounts: list[Unnumbered]) -> str:
    groups: dict[str, list[Unnumbered]] = defaultdict(list)
    for a in accounts:
        groups[a.account_type].append(a)
    lines = [f"{len(accounts)} of {total} active accounts without a number"]
    for account_type in sorted(groups, key=lambda t: (-len(groups[t]), t)):
        members = sorted(groups[account_type], key=lambda a: a.name.lower())
        lines.append(f"\n{account_type}: {len(members)}")
        for a in members:
            sub = " (sub-account)" if a.sub_account else ""
            lines.append(f"  {a.account_id}  {a.name}  [{a.sub_type or '-'}]{sub}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant", required=True, help="tenant slug")
    args = parser.parse_args(argv)
    engine = create_app_engine()
    with untenanted_session(engine) as db:
        tenant_id = db.execute(
            select(Tenant.id).where(Tenant.slug == args.tenant)
        ).scalar_one_or_none()
    if tenant_id is None:
        print(f"no tenant with slug {args.tenant!r}", file=sys.stderr)
        return 2
    print(report(*unnumbered_accounts(engine, tenant_id)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
