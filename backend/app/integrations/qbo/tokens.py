"""The access token for a connection, refreshed under a row lock (F05).

A refresh is a short transaction of its own: ``SELECT … FOR UPDATE`` on the
connection row, re-read the expiry (a worker that waited for the lock finds the
newer pair and makes no call), call Intuit's token endpoint, store **both** tokens
and both expiries from the response, commit. It never shares a transaction with an
API call, so a later failure cannot roll back a refresh token Intuit has already
replaced.

``invalid_grant`` (or no refresh token at all, or a connection made with the other
Intuit environment than the server's keys, F05.1) sets ``needs_reconnect`` with an
audit row, in the same transaction, and raises ``NeedsReconnect``. Tasks turn that into a
permanent failure: no retries, the worker keeps running.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, select

from app.core.config import get_settings, require_qbo_settings
from app.core.crypto import CryptoError
from app.core.db import tenant_session
from app.ingest.connections import (
    get_connection_tokens,
    mark_needs_reconnect,
    store_refreshed_tokens,
)
from app.ingest.models import Connection
from app.integrations.qbo import client
from app.integrations.qbo.constants import REFRESH_MARGIN_SECONDS

log = logging.getLogger("app.qbo")


class NeedsReconnect(Exception):
    """The connection cannot be used until a firm admin reconnects it. ``tid`` is
    Intuit's trace id of the refusal when Intuit was asked (F05.1)."""

    def __init__(self, reason: str, *, tid: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tid = tid


@dataclass(frozen=True)
class Access:
    access_token: str
    realm_id: str
    environment: str


def _fresh(connection: Connection, now: datetime, *, not_before: datetime | None) -> bool:
    """Usable without a refresh: enough time left and, when a caller saw a 401 at
    ``not_before``, refreshed since then."""
    if connection.token_expires_at is None:
        return False
    if connection.token_expires_at - now < timedelta(seconds=REFRESH_MARGIN_SECONDS):
        return False
    if not_before is not None:
        refreshed = connection.tokens_refreshed_at
        return refreshed is not None and refreshed > not_before
    return True


def access_for(
    engine: Engine,
    tenant_id: UUID,
    connection_id: UUID,
    *,
    rejected_at: datetime | None = None,
) -> Access:
    """A usable access token. ``rejected_at``: the API answered 401 at that moment,
    so anything stored before it is refreshed whatever its expiry says."""
    settings = get_settings()
    require_qbo_settings(settings)
    with tenant_session(engine, tenant_id) as db:
        connection = db.execute(
            select(Connection).where(Connection.id == connection_id).with_for_update()
        ).scalar_one_or_none()
        if connection is None or connection.status != "connected" or not connection.realm_id:
            raise NeedsReconnect("not connected")
        try:
            access_token, refresh_token = get_connection_tokens(connection)
        except (LookupError, CryptoError):
            access_token, refresh_token = None, None
        now = datetime.now(UTC)
        environment = connection.environment or settings.qbo_environment or ""
        tid: str | None = None
        if connection.environment and connection.environment != settings.qbo_environment:
            # F05.1 (plan question 5): the server's keys belong to the other Intuit
            # environment (the sandbox tenant after the production key swap). A
            # refresh would be refused with something other than invalid_grant and
            # retried for ever; end the chain once instead.
            mark_needs_reconnect(db, connection, reason="environment_mismatch")
            reason = "environment_mismatch"
        elif access_token and _fresh(connection, now, not_before=rejected_at):
            return Access(access_token, connection.realm_id, environment)
        elif not refresh_token:
            mark_needs_reconnect(db, connection, reason="no_refresh_token")
            reason = "no_refresh_token"
        else:
            try:
                tokens = client.refresh_tokens(settings, refresh_token)
            except client.TokenRefused as exc:
                tid = exc.tid
                mark_needs_reconnect(db, connection, reason="invalid_grant", tid=tid)
                reason = "invalid_grant"
            else:
                store_refreshed_tokens(
                    db,
                    connection,
                    access_token=tokens.access_token,
                    refresh_token=tokens.refresh_token,
                    token_expires_at=tokens.access_expires_at,
                    refresh_token_expires_at=tokens.refresh_expires_at,
                    refreshed_at=now,
                )
                log.info("qbo tokens refreshed connection=%s tenant=%s", connection_id, tenant_id)
                return Access(tokens.access_token, connection.realm_id, environment)
    # The transaction above committed the needs_reconnect status and its audit row.
    log.warning(
        "qbo needs reconnect connection=%s tenant=%s reason=%s tid=%s",
        connection_id,
        tenant_id,
        reason,
        tid or "-",
    )
    raise NeedsReconnect(reason, tid=tid)
