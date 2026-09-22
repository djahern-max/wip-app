#!/usr/bin/env python
"""Delete a tenant and everything it holds (F05.1, D-28). An operator action, never an
API: it runs on the server as root with the **owner** role, because the append-only
triggers must be suspended for this one transaction and only the table owner can do
that (``app_rw`` cannot, and the script stops before touching anything if it is not
the owner).

  cd /opt/wip/backend && set -a && . /etc/wip/migrate.env && set +a && \\
      ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/delete_tenant.py <slug>

``DATABASE_OWNER_URL`` comes from the root-only migration file; the key ring, the
Intuit keys and the Space key come from ``app.env`` (to revoke tokens and delete
objects). Steps, in order:

1. Refuse a slug in ``PROTECTED_TENANT_SLUGS``; refuse a role that does not own the
   tenant tables; print the tenant's row count per tenant table and the object count
   under ``tenant/{id}/``; require the slug typed back exactly.
2. Revoke the QuickBooks tokens at Intuit if a connection holds any (best effort).
3. Delete the objects under the tenant's prefix.
4. One transaction: ``SET LOCAL app.tenant_id`` (forced RLS binds the owner too),
   ``ALTER TABLE … DISABLE TRIGGER`` on the append-only tenant tables, ``DELETE`` from
   every tenant table in foreign-key order (derived from the catalog, so a later
   feature's table needs no change here), ``ENABLE TRIGGER`` again, sessions pointing at
   the tenant cleared, the ``tenant`` row, and one ``firm_audit_log`` row
   (``tenant_deleted``: slug, counts, object count, operator), then COMMIT. A failure
   anywhere rolls the whole transaction back, triggers included.

``session_replication_role = replica`` is not used: on Managed Postgres nobody can set
it (superuser or ``SET`` privilege only). ``ALTER TABLE`` holds an exclusive lock on the
two append-only tables until commit, so run this in a quiet moment; the API's audit
writes wait for those seconds. Never prints a URL, a key or a row.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from app.core.audit import FirmEvent, write_firm_audit
from app.core.config import (
    MissingSettings,
    Settings,
    get_settings,
    protected_tenant_slug_set,
    require_qbo_settings,
)
from app.core.crypto import CryptoError
from app.core.db import PRODUCTION_ENGINE_OPTIONS, set_tenant_context, untenanted_session
from app.core.storage import ObjectStore, build_object_store
from app.ingest.connections import get_connection_tokens
from app.ingest.models import Connection
from app.integrations.qbo import SYSTEM, client
from app.tenancy import catalog
from app.tenancy.models import Tenant
from app.tenancy.rls import APPEND_ONLY_TABLES, append_only_trigger_names


class Refused(SystemExit):
    """Nothing was changed. The message says why in one sentence."""


@dataclass(frozen=True)
class Plan:
    tenant_id: UUID
    firm_id: UUID
    slug: str
    name: str
    counts: dict[str, int]
    object_keys: tuple[str, ...]
    order: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    plan: Plan
    revoked: bool | None  # None: no tokens to revoke
    objects_deleted: int
    rows_deleted: dict[str, int]


# --- preflight -----------------------------------------------------------------------------


def owner_engine(settings: Settings) -> Engine:
    url = settings.database_owner_url
    if not url:
        raise Refused("missing required environment variable(s): DATABASE_OWNER_URL")
    return create_engine(url, **PRODUCTION_ENGINE_OPTIONS)


def _not_owned(conn) -> list[str]:
    """Tenant tables the connected role does not own (``ALTER TABLE`` would fail)."""
    owned = set(
        conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tableowner = current_user"
            )
        ).scalars()
    )
    return [t for t in catalog.tenant_tables(conn) if t not in owned]


def deletion_order(conn) -> tuple[str, ...]:
    """Tenant tables ordered so that a table is deleted before every tenant table it
    references (foreign keys stay on: nothing here disables them)."""
    tables = catalog.tenant_tables(conn)
    refs: dict[str, set[str]] = {t: set() for t in tables}
    rows = conn.execute(
        text(
            "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text "
            "FROM pg_constraint c WHERE c.contype = 'f'"
        )
    ).all()
    for child, parent in rows:
        child, parent = child.strip('"'), parent.strip('"')
        if child in refs and parent in refs and child != parent:
            refs[child].add(parent)
    order: list[str] = []
    remaining = set(tables)
    while remaining:
        # A table may go once no remaining table references it.
        free = sorted(t for t in remaining if not any(t in refs[o] for o in remaining if o != t))
        if not free:
            raise Refused("tenant tables reference each other in a cycle; nothing was changed")
        order.extend(free)
        remaining -= set(free)
    return tuple(order)


def row_counts(engine: Engine, tenant_id: UUID, tables: tuple[str, ...]) -> dict[str, int]:
    with Session(engine) as db, db.begin():
        set_tenant_context(db, tenant_id)
        return {
            t: db.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one()  # noqa: S608
            for t in tables
        }


def plan(engine: Engine, store: ObjectStore, settings: Settings, slug: str) -> Plan:
    if slug in protected_tenant_slug_set(settings):
        raise Refused(f"refusing: tenant {slug!r} is listed in PROTECTED_TENANT_SLUGS")
    with engine.connect() as conn:
        missing = _not_owned(conn)
        if missing:
            raise Refused(
                "refusing: the connected role does not own the tenant tables "
                f"({', '.join(missing[:3])}{', …' if len(missing) > 3 else ''}); "
                "run with the owner role (DATABASE_OWNER_URL from /etc/wip/migrate.env)"
            )
        order = deletion_order(conn)
    with untenanted_session(engine) as db:
        row = db.execute(
            select(Tenant.id, Tenant.firm_id, Tenant.name).where(Tenant.slug == slug)
        ).one_or_none()
    if row is None:
        raise Refused(f"no tenant with slug {slug!r}")
    tenant_id, firm_id, name = row
    return Plan(
        tenant_id=tenant_id,
        firm_id=firm_id,
        slug=slug,
        name=name,
        counts=row_counts(engine, tenant_id, order),
        object_keys=tuple(store.list_keys(tenant_id)),
        order=order,
    )


# --- the deletion --------------------------------------------------------------------------


def revoke_tokens(engine: Engine, settings: Settings, tenant_id: UUID) -> bool | None:
    """``None`` when there is nothing to revoke; otherwise whether Intuit confirmed."""
    with Session(engine) as db, db.begin():
        set_tenant_context(db, tenant_id)
        connection = db.execute(
            select(Connection).where(Connection.tenant_id == tenant_id, Connection.system == SYSTEM)
        ).scalar_one_or_none()
        if connection is None or connection.access_token_enc is None:
            return None
        try:
            _access, refresh = get_connection_tokens(connection)
        except (LookupError, CryptoError):
            return False
    if not refresh:
        return False
    try:
        require_qbo_settings(settings)
    except MissingSettings:
        return False
    return client.revoke(settings, refresh)


def delete_objects(store: ObjectStore, tenant_id: UUID, keys: tuple[str, ...]) -> int:
    for key in keys:
        store.delete(tenant_id, key)
    return len(keys)


def delete_rows(engine: Engine, p: Plan, *, operator: str, objects_deleted: int) -> dict[str, int]:
    append_only = [t for t in APPEND_ONLY_TABLES if t in p.order]
    deleted: dict[str, int] = {}
    with Session(engine) as db, db.begin():
        set_tenant_context(db, p.tenant_id)
        for table in append_only:
            for trigger in append_only_trigger_names(table):
                db.execute(text(f'ALTER TABLE "{table}" DISABLE TRIGGER {trigger}'))
        for table in p.order:
            result = db.execute(
                text(f'DELETE FROM "{table}" WHERE tenant_id = :t'),  # noqa: S608
                {"t": p.tenant_id},
            )
            deleted[table] = result.rowcount
        for table in append_only:
            for trigger in append_only_trigger_names(table):
                db.execute(text(f'ALTER TABLE "{table}" ENABLE TRIGGER {trigger}'))
        db.execute(
            text("UPDATE session SET active_tenant_id = NULL WHERE active_tenant_id = :t"),
            {"t": p.tenant_id},
        )
        db.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": p.tenant_id})
        write_firm_audit(
            db,
            firm_id=p.firm_id,
            action=FirmEvent.tenant_deleted,
            entity_type="tenant",
            entity_id=p.tenant_id,
            actor_user_id=None,
            actor_role=None,
            detail={
                "slug": p.slug,
                "rows": deleted,
                "objects": objects_deleted,
                "operator": operator,
                "via": "cli",
            },
        )
    return deleted


def run(
    engine: Engine,
    store: ObjectStore,
    settings: Settings,
    *,
    slug: str,
    typed: str,
    operator: str,
) -> Outcome:
    """The whole operation for a slug already shown to the operator (``plan``)."""
    p = plan(engine, store, settings, slug)
    if typed != slug:
        raise Refused("the slug typed back does not match; nothing was changed")
    revoked = revoke_tokens(engine, settings, p.tenant_id)
    objects = delete_objects(store, p.tenant_id, p.object_keys)
    rows = delete_rows(engine, p, operator=operator, objects_deleted=objects)
    return Outcome(p, revoked, objects, rows)


# --- CLI -----------------------------------------------------------------------------------


def describe(p: Plan) -> list[str]:
    lines = [f"tenant {p.slug!r} ({p.name}), id {p.tenant_id}"]
    lines += [f"  {t}: {n}" for t, n in p.counts.items() if n]
    lines.append(f"  objects under tenant/{p.tenant_id}/: {len(p.object_keys)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slug")
    args = parser.parse_args(argv)
    settings = get_settings()
    engine = owner_engine(settings)
    store = build_object_store(settings)
    try:
        p = plan(engine, store, settings, args.slug)
        print("\n".join(describe(p)))
        print("This cannot be undone. Type the slug to delete it, or anything else to stop:")
        typed = input("> ").strip()
        operator = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
        outcome = run(engine, store, settings, slug=args.slug, typed=typed, operator=operator)
    finally:
        engine.dispose()
    revoked = {None: "no tokens held", True: "revoked at Intuit", False: "not revoked at Intuit"}
    print(f"QuickBooks tokens: {revoked[outcome.revoked]}")
    print(f"objects deleted: {outcome.objects_deleted}")
    print(f"rows deleted: {sum(outcome.rows_deleted.values())}; tenant {args.slug!r} removed")
    print("run scripts/prod_check.py next (append-only triggers must show enabled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
