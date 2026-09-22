#!/usr/bin/env python
"""Read-only production check (F05.0, D-27): the production database and object store
pass the rules the test suite enforces. Run after every deploy, as the application
would run (``DATABASE_URL``, the ``app_rw`` role; ``ENV_FILE`` picks the file):

  sudo -u wip ENV_FILE=/etc/wip/app.env /opt/wip/backend/.venv/bin/python \\
      /opt/wip/backend/scripts/prod_check.py

Checks: the connected role is ``app_rw``, ``NOSUPERUSER``, ``NOBYPASSRLS`` and owns no
table; the connection is over SSL; every table with a ``tenant_id`` column has RLS
enabled and forced with ``tenant_isolation``; no policy outside the D-11 allow-list
(``app.tenancy.catalog.EXTRA_POLICIES``); both append-only triggers present and
enabled on each of ``APPEND_ONLY_TABLES`` and the register matches the catalog; the
object store answers a HEAD on the bucket (``OBJECT_STORE=s3``; with ``local`` it says
so and passes). Prints ``ok: <check>`` or ``FAIL: <check>: <what>`` per check and exits
1 on any failure. Never prints a URL, a key, a hostname or a row: a failure names a
table, policy, trigger or role only.
"""

import sys
from collections.abc import Callable

from sqlalchemy import Engine, text

from app.core.config import get_settings, require_object_store_settings
from app.core.db import create_app_engine
from app.core.storage import ObjectStore, S3ObjectStore, build_object_store
from app.tenancy import catalog

APP_ROLE = "app_rw"

Check = Callable[[], list[str]]  # → failure lines (empty = ok)


def _role_checks(engine: Engine) -> list[tuple[str, Check]]:
    def role_flags() -> list[str]:
        with engine.connect() as conn:
            name, superuser, bypass = conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
        failures = []
        if name != APP_ROLE:
            failures.append(f"connected as {name}, expected {APP_ROLE}")
        if superuser:
            failures.append(f"{name} is SUPERUSER")
        if bypass:
            failures.append(f"{name} has BYPASSRLS")
        return failures

    def owns_no_table() -> list[str]:
        with engine.connect() as conn:
            owned = (
                conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tableowner = current_user ORDER BY 1"
                    )
                )
                .scalars()
                .all()
            )
        return [f"owned by the application role: {t}" for t in owned]

    def ssl() -> list[str]:
        with engine.connect() as conn:
            on = conn.execute(
                text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            ).scalar_one_or_none()
        return [] if on else ["the connection is not over SSL"]

    return [
        (f"role is {APP_ROLE}, NOSUPERUSER, NOBYPASSRLS", role_flags),
        ("role owns no table", owns_no_table),
        ("connection over SSL", ssl),
    ]


def _catalog_checks(engine: Engine) -> list[tuple[str, Check]]:
    def wrap(fn: Callable) -> Check:
        def run() -> list[str]:
            with engine.connect() as conn:
                return fn(conn)

        return run

    def tables_found() -> list[str]:
        with engine.connect() as conn:
            n = len(catalog.tenant_tables(conn))
        return [] if n else ["no table with a tenant_id column was found"]

    return [
        ("tenant tables enumerated", tables_found),
        (
            "RLS enabled and forced with tenant_isolation on every tenant table",
            wrap(catalog.rls_failures),
        ),
        ("no policy outside the D-11 allow-list", wrap(catalog.policy_failures)),
        ("append-only triggers present and enabled", wrap(catalog.append_only_failures)),
        ("append-only register matches the catalog", wrap(catalog.append_only_register_mismatch)),
    ]


def bucket_check(store: ObjectStore) -> list[str]:
    """HEAD on the bucket. A refused or failed call is reported by its code, never by
    the bucket name, the endpoint or the key."""
    if not isinstance(store, S3ObjectStore):
        return []
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        store.client.head_bucket(Bucket=store.bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "?")
        return [f"HEAD on the bucket failed ({code})"]
    except BotoCoreError as exc:
        return [f"HEAD on the bucket failed ({type(exc).__name__})"]
    return []


def run_checks(engine: Engine, store: ObjectStore) -> list[tuple[str, list[str]]]:
    """Every check, in order: (name, failure lines). A check that raises is a failure
    naming the exception type only."""
    store_label = (
        "object store answers a HEAD on the bucket"
        if isinstance(store, S3ObjectStore)
        else "object store is local (no bucket to check)"
    )
    checks: list[tuple[str, Check]] = [
        *_role_checks(engine),
        *_catalog_checks(engine),
        (store_label, lambda: bucket_check(store)),
    ]
    results: list[tuple[str, list[str]]] = []
    for name, check in checks:
        try:
            failures = check()
        except Exception as exc:  # noqa: BLE001 - reported by type, never by message
            failures = [f"check raised {type(exc).__name__}"]
        results.append((name, failures))
    return results


def report(results: list[tuple[str, list[str]]]) -> tuple[list[str], int]:
    lines: list[str] = []
    for name, failures in results:
        if failures:
            lines.extend(f"FAIL: {name}: {f}" for f in failures)
        else:
            lines.append(f"ok: {name}")
    failed = sum(1 for _, f in results if f)
    lines.append(
        f"{len(results) - failed} of {len(results)} checks passed"
        if failed
        else f"all {len(results)} checks passed"
    )
    return lines, (1 if failed else 0)


def main() -> int:
    settings = get_settings()
    require_object_store_settings(settings)
    engine = create_app_engine(production=True)
    try:
        results = run_checks(engine, build_object_store(settings))
    finally:
        engine.dispose()
    lines, code = report(results)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
