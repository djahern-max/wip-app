"""Alembic environment. Migrations run as ``app_owner`` (DATABASE_OWNER_URL), which
is required here and only here (the API process never holds it, F02.1). It is also
all this process reads: not the application URL, not the encryption keys.

Destructive-downgrade guard (F03 close-out, owner rule). ``alembic downgrade``
runs only when **both** hold:

1. ``ALLOW_DESTRUCTIVE_DOWNGRADE=1`` is set in the migration process environment;
2. the target database name (the database in the URL Alembic will connect to) is
   not one of ``PROTECTED_DATABASE_NAMES`` (comma-separated; default ``wip``, the
   local dev database; production sets its own name, and the test database
   ``wip_test`` and the ``wip_mig_*`` scratch databases are never listed).

Otherwise the process exits non-zero naming the database, before connecting, so
nothing changes. ``upgrade`` is never guarded. Migration round-trip checks run only
against a scratch database (``tests/conftest.py::scratch_db_url``, CI).
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, make_url, pool

# Imported for their side effect: register every model on Base.metadata.
import app.audit.models  # noqa: F401
import app.domain.billing.models  # noqa: F401
import app.domain.config.models  # noqa: F401
import app.ingest.models  # noqa: F401
import app.worker.models  # noqa: F401
from app.core.config import get_migration_settings
from app.tenancy.models import Base

config = context.config
if config.config_file_name is not None:
    # Keep loggers created by the application (and by pytest's capture) alive.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

DOWNGRADE_FLAG = "ALLOW_DESTRUCTIVE_DOWNGRADE"
PROTECTED_NAMES_VAR = "PROTECTED_DATABASE_NAMES"
DEFAULT_PROTECTED_NAMES = "wip"


def _owner_url() -> str:
    # Tests pass the URL through alembic's config attributes.
    url = config.get_main_option("owner_url") or get_migration_settings().database_owner_url
    if not url:
        sys.exit("missing required environment variable(s): DATABASE_OWNER_URL")
    return url


def _is_downgrade() -> bool:
    """``alembic.command.downgrade`` hands the environment a function named
    ``downgrade`` (``_proxy`` is the live EnvironmentContext behind the module
    proxy). Fallback on the public destination argument: ``base`` (None) or a
    relative step downwards."""
    env = getattr(context, "_proxy", None)
    fn = env.context_opts.get("fn") if env is not None else None
    if fn is not None:
        return getattr(fn, "__name__", "") == "downgrade"
    destination = context.get_revision_argument()
    return destination is None or str(destination).startswith("-")


def protected_database_names() -> frozenset[str]:
    raw = os.environ.get(PROTECTED_NAMES_VAR, DEFAULT_PROTECTED_NAMES)
    return frozenset(n.strip() for n in raw.split(",") if n.strip())


def guard_downgrade(url: str) -> None:
    """Exit, naming the database and changing nothing, unless the rule above holds."""
    if not _is_downgrade():
        return
    database = make_url(url).database or "?"
    if os.environ.get(DOWNGRADE_FLAG) != "1":
        sys.exit(
            f"refusing to downgrade database {database!r}: {DOWNGRADE_FLAG}=1 is not set "
            "(downgrades run only against a scratch database; nothing was changed)"
        )
    if database in protected_database_names():
        sys.exit(
            f"refusing to downgrade database {database!r}: it is listed in "
            f"{PROTECTED_NAMES_VAR} (downgrades run only against a scratch database; "
            "nothing was changed)"
        )


def run_migrations_offline() -> None:
    url = _owner_url()
    guard_downgrade(url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _owner_url()
    guard_downgrade(url)
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
