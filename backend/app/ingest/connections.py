"""Connection rows and their tokens (F03; BLUEPRINT §6.1, §11). No OAuth flow and
no HTTP client here (``app.integrations.qbo``). Tokens are ciphertext under the key ring with
associated data ``tenant_id | connection_id | field``: a blob copied to another
row, another field or another tenant fails to decrypt. Values never reach
``last_error``, a log line, an audit row or a response."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import RequestMeta, TenantEvent, write_tenant_audit
from app.core.crypto import aad_for, get_keyring
from app.ingest.models import Connection
from app.integrations.base import SOURCE_KINDS
from app.tenancy.models import Role

TOKEN_FIELDS = ("access_token", "refresh_token")


def known_systems() -> frozenset[str]:
    return frozenset(k.source for k in SOURCE_KINDS.values()) | frozenset({"qbo"})


def get_or_create_connection(db: Session, tenant_id: UUID, system: str) -> Connection:
    if system not in known_systems():
        raise ValueError(f"unknown system {system!r}")
    row = db.execute(
        select(Connection).where(Connection.tenant_id == tenant_id, Connection.system == system)
    ).scalar_one_or_none()
    if row is None:
        row = Connection(tenant_id=tenant_id, system=system, status="disconnected")
        db.add(row)
        db.flush()
    return row


def _write_tokens(connection: Connection, access_token: str, refresh_token: str | None) -> None:
    ring = get_keyring()
    tid, cid = connection.tenant_id, connection.id
    connection.access_token_key_id, connection.access_token_enc = ring.encrypt(
        access_token.encode(), aad=aad_for(tid, cid, "access_token")
    )
    if refresh_token is None:
        connection.refresh_token_key_id = connection.refresh_token_enc = None
    else:
        connection.refresh_token_key_id, connection.refresh_token_enc = ring.encrypt(
            refresh_token.encode(), aad=aad_for(tid, cid, "refresh_token")
        )


def set_connection_tokens(
    db: Session,
    connection: Connection,
    *,
    access_token: str,
    refresh_token: str | None,
    token_expires_at: datetime | None,
    actor_user_id: UUID | None,
    actor_role: Role | None,
    meta: RequestMeta | None = None,
    refresh_token_expires_at: datetime | None = None,
) -> None:
    """Tokens from a connect or reconnect: status ``connected``, audited."""
    tid, cid = connection.tenant_id, connection.id
    _write_tokens(connection, access_token, refresh_token)
    connection.token_expires_at = token_expires_at
    connection.refresh_token_expires_at = refresh_token_expires_at
    connection.status = "connected"
    connection.last_error = None
    db.flush()
    write_tenant_audit(
        db,
        tenant_id=tid,
        action=TenantEvent.connection_tokens_set,
        entity_type="connection",
        entity_id=cid,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        detail={"system": connection.system, "has_refresh_token": refresh_token is not None},
        meta=meta,
    )


def store_refreshed_tokens(
    db: Session,
    connection: Connection,
    *,
    access_token: str,
    refresh_token: str,
    token_expires_at: datetime,
    refresh_token_expires_at: datetime | None,
    refreshed_at: datetime,
) -> None:
    """Both tokens from a refresh response. No audit row (owner answer 8, F05): the
    moment is kept in ``tokens_refreshed_at``. A refresh *failure* is audited
    (``mark_needs_reconnect``)."""
    _write_tokens(connection, access_token, refresh_token)
    connection.token_expires_at = token_expires_at
    connection.refresh_token_expires_at = refresh_token_expires_at
    connection.tokens_refreshed_at = refreshed_at
    db.flush()


def clear_connection_tokens(connection: Connection) -> None:
    connection.access_token_enc = connection.access_token_key_id = None
    connection.refresh_token_enc = connection.refresh_token_key_id = None
    connection.token_expires_at = connection.refresh_token_expires_at = None


def mark_needs_reconnect(db: Session, connection: Connection, *, reason: str) -> None:
    """The source refused the refresh token, or the connection was revoked there.
    ``reason`` is a short code (``invalid_grant``), never a response body. Tokens
    that no longer work are cleared. Audited; the actor is the system."""
    if connection.status == "needs_reconnect":
        return
    clear_connection_tokens(connection)
    connection.status = "needs_reconnect"
    connection.last_error = reason[:500]
    db.flush()
    write_tenant_audit(
        db,
        tenant_id=connection.tenant_id,
        action=TenantEvent.connection_needs_reconnect,
        entity_type="connection",
        entity_id=connection.id,
        actor_user_id=None,
        actor_role=None,
        detail={"system": connection.system, "reason": reason[:80]},
    )


def mark_disconnected(
    db: Session,
    connection: Connection,
    *,
    revoked_at_source: bool,
    actor_user_id: UUID | None,
    actor_role: Role | None,
    meta: RequestMeta | None = None,
) -> None:
    """Tokens cleared, status ``disconnected``, audited. Synced data stays, and so
    does the company link (``realm_id``)."""
    clear_connection_tokens(connection)
    connection.status = "disconnected"
    connection.last_error = None
    connection.oauth_state_sha256 = None
    connection.oauth_state_user_id = None
    connection.oauth_state_expires_at = None
    db.flush()
    write_tenant_audit(
        db,
        tenant_id=connection.tenant_id,
        action=TenantEvent.connection_disconnected,
        entity_type="connection",
        entity_id=connection.id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        detail={"system": connection.system, "revoked_at_source": revoked_at_source},
        meta=meta,
    )


def get_connection_tokens(connection: Connection) -> tuple[str, str | None]:
    """``(access_token, refresh_token)``; ``CryptoError`` when a blob does not belong
    to this row. Callers (F05) never log or store the result."""
    ring = get_keyring()
    tid, cid = connection.tenant_id, connection.id
    if connection.access_token_enc is None or connection.access_token_key_id is None:
        raise LookupError("connection has no tokens")
    access = ring.decrypt(
        connection.access_token_key_id,
        connection.access_token_enc,
        aad=aad_for(tid, cid, "access_token"),
    ).decode()
    refresh = None
    if connection.refresh_token_enc is not None and connection.refresh_token_key_id is not None:
        refresh = ring.decrypt(
            connection.refresh_token_key_id,
            connection.refresh_token_enc,
            aad=aad_for(tid, cid, "refresh_token"),
        ).decode()
    return access, refresh
