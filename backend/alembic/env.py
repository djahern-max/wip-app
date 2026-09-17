"""Alembic environment. Migrations run as ``app_owner`` (DATABASE_OWNER_URL)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Imported for their side effect: register every model on Base.metadata.
import app.audit.models  # noqa: F401
from app.core.config import get_settings
from app.tenancy.models import Base

config = context.config
if config.config_file_name is not None:
    # Keep loggers created by the application (and by pytest's capture) alive.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _owner_url() -> str:
    # Tests pass the URL through alembic's -x option / config attributes.
    return config.get_main_option("owner_url") or get_settings().database_owner_url


def run_migrations_offline() -> None:
    context.configure(
        url=_owner_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_owner_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
