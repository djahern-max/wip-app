"""Database engine and request-scoped sessions with tenant context.

Tenant isolation is enforced by Postgres Row-Level Security (BLUEPRINT §3.8, §11).
Every session that touches a tenant table must set ``app.tenant_id`` with
``SET LOCAL`` inside its transaction; the RLS policy created by
``app.tenancy.rls.enable_tenant_rls`` reads that setting. ``app.user_id`` is set
the same way and lets a user read their own ``membership`` rows before a tenant
is chosen (D-11).

Rules (CLAUDE.md):
- The app connects as ``app_rw`` (not owner, no BYPASSRLS).
- No module-level or cached sessions. The engine (a connection pool) is shared;
  sessions are created per request / per task and closed with it.
- Worker tasks take ``tenant_id`` explicitly and use ``tenant_session`` the same way.
- The request principal and tenant come from the server-side session
  (``app.core.auth``), never from a header.

Engine options (F02.1, owner answer to call 7): outside the test harness the
engine hides bound parameters from log lines and exception messages and never
echoes SQL, so a row value (a password hash, a token hash) cannot reach a log or
an error body through the driver. The test harness overrides ``ENGINE_OPTIONS``
so its statement/parameter capture keeps working; ``PRODUCTION_ENGINE_OPTIONS``
stays the reference the tests compare against.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import get_settings

PRODUCTION_ENGINE_OPTIONS: dict = {"pool_pre_ping": True, "hide_parameters": True, "echo": False}
ENGINE_OPTIONS: dict = dict(PRODUCTION_ENGINE_OPTIONS)


def create_app_engine(url: str | None = None, *, production: bool = False) -> Engine:
    """Create the application engine (``app_rw`` role). ``production=True`` ignores
    any harness override of ``ENGINE_OPTIONS``."""
    options = PRODUCTION_ENGINE_OPTIONS if production else ENGINE_OPTIONS
    return create_engine(url or get_settings().database_url, **options)


def set_tenant_context(session: Session, tenant_id: UUID) -> None:
    """``SET LOCAL app.tenant_id`` for the session's current transaction.

    ``set_config(..., is_local => true)`` is the parameterizable form of ``SET LOCAL``;
    the value reverts when the transaction ends, so nothing leaks across requests
    that reuse a pooled connection. Calling it again inside the same transaction
    replaces the value: a firm_admin acting on a tenant other than the active one
    does exactly that, once, before the write (see ``app.auth.admin``).
    """
    session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def set_user_context(session: Session, user_id: UUID | None) -> None:
    """``SET LOCAL app.user_id``: enables the own-membership read policy (D-11).
    ``None`` clears it (the policy then matches nothing)."""
    session.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": "" if user_id is None else str(user_id)},
    )


def current_user_context(session: Session) -> str | None:
    """The transaction's ``app.user_id`` as a string, or ``None`` when unset/empty."""
    value = session.execute(text("SELECT current_setting('app.user_id', true)")).scalar_one()
    return value or None


@contextmanager
def tenant_session(engine: Engine, tenant_id: UUID) -> Iterator[Session]:
    """One transaction, scoped to one tenant. Commits on success, rolls back on error."""
    with Session(engine) as session, session.begin():
        set_tenant_context(session, tenant_id)
        yield session


@contextmanager
def untenanted_session(engine: Engine) -> Iterator[Session]:
    """A transaction with **no** tenant context.

    Only for tables without ``tenant_id`` (``firm``, ``tenant``, ``user``, ``session``,
    ``firm_audit_log``, ``firm_membership``) and for health checks. Tenant tables
    read as empty and reject writes in this session.
    """
    with Session(engine) as session, session.begin():
        yield session


def describe_db_error(exc: DBAPIError) -> str:
    """What may be logged about a database error: the SQLSTATE and the server's
    primary message, never the statement, its parameters, or the DETAIL line
    (Postgres echoes the conflicting key value there, e.g. an e-mail address)."""
    orig = exc.orig
    diag = getattr(orig, "diag", None)
    sqlstate = getattr(diag, "sqlstate", None) or getattr(orig, "sqlstate", None) or "unknown"
    primary = getattr(diag, "message_primary", None) or type(orig).__name__
    return f"{type(exc).__name__} [{sqlstate}]: {primary}"


# --- FastAPI dependencies -------------------------------------------------------


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_request_session(engine: Annotated[Engine, Depends(get_engine)]) -> Iterator[Session]:
    """One transaction per request. Opens with no context; ``app.core.auth.get_principal``
    sets ``app.user_id`` and, when a tenant is active, ``app.tenant_id`` on it.
    Commits when the handler returns, rolls back if it raises, so an action and
    its audit row always land together."""
    with Session(engine) as session, session.begin():
        yield session


RequestSession = Annotated[Session, Depends(get_request_session)]
AppEngine = Annotated[Engine, Depends(get_engine)]
