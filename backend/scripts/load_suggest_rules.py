#!/usr/bin/env python
"""Load (replace) a tenant's account-suggestion rules from a JSON file (F04).

    cd backend && .venv/bin/python scripts/load_suggest_rules.py --tenant rye-beach \\
        --file tests/fixtures/rye_beach/account_suggest_rules.json

The file has the shape of ``tests/fixtures/rye_beach/account_suggest_rules.json``:
``divisions`` (created when missing) and ``rules`` (ordered; first match wins). Runs
as the application role with tenant context, through the same audited service as
``PUT /api/config/suggest-rules`` (actor "cli"). One bad pattern refuses the whole
load, naming the rule. Then re-run suggestions from the Accounts page, or pass
``--suggest`` to run them here.
"""

import argparse
import json
import sys

from sqlalchemy import select

from app.core.audit import RequestMeta
from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.domain.config.audit import Actor
from app.domain.config.chart import suggest
from app.domain.config.rules import RuleError
from app.domain.config.service import ConfigError, load_suggest_rules
from app.tenancy.models import Tenant

CLI = Actor(meta=RequestMeta(request_id="cli"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", required=True, metavar="SLUG")
    ap.add_argument("--file", required=True)
    ap.add_argument("--suggest", action="store_true", help="run suggestions afterwards")
    args = ap.parse_args(argv)
    with open(args.file, encoding="utf-8") as fh:
        spec = json.load(fh)
    engine = create_app_engine()
    try:
        with untenanted_session(engine) as db:
            tenant = db.execute(
                select(Tenant).where(Tenant.slug == args.tenant)
            ).scalar_one_or_none()
            if tenant is None:
                print(f"unknown tenant slug: {args.tenant}", file=sys.stderr)
                return 1
            tenant_id = tenant.id
        try:
            with tenant_session(engine, tenant_id) as db:
                n = load_suggest_rules(db, tenant_id, spec, CLI)
            print(f"loaded {n} rule(s) for {args.tenant}")
            if args.suggest:
                with tenant_session(engine, tenant_id) as db:
                    c = suggest(db, tenant_id, CLI)
                print(
                    f"suggestions: new {c.suggested}, unchanged {c.unchanged}, "
                    f"removed {c.removed}, unmatched {c.unmatched}"
                )
        except (RuleError, ConfigError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
