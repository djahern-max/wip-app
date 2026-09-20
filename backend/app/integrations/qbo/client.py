"""The one module that talks HTTP to Intuit (F05). Core Accounting REST API and the
OAuth token endpoints; nothing else in the application opens a connection to them.

- **Bodies are parsed with ``app.core.jsoncodec.json_loads``, never
  ``response.json()``**: QuickBooks sends amounts as JSON numbers and no amount may
  pass through ``float``.
- 429, 5xx and transport failures: wait (``Retry-After`` when given, otherwise
  doubling from one second, capped) and try again, ``MAX_TRIES`` in all.
- Log lines carry the operation, the status code and Intuit's ``intuit_tid``; never
  a payload, a token, a code, the client secret or a name.
- ``TRANSPORT`` is ``None`` in production (the network). The test harness installs
  a stub here, so CI never reaches Intuit.

One request in flight per connection is the caller's job (``app.integrations.qbo.
reader`` takes the lock); this module is stateless.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import Settings
from app.core.jsoncodec import json_loads
from app.integrations.qbo.constants import (
    API_BASE_URLS,
    BACKOFF_CAP_SECONDS,
    BACKOFF_FIRST_SECONDS,
    CONNECT_TIMEOUT,
    MAX_TRIES,
    MINOR_VERSION,
    READ_TIMEOUT,
    REVOKE_URL,
    TID_HEADER,
    TOKEN_TIMEOUT,
    TOKEN_URL,
)

log = logging.getLogger("app.qbo")
# httpx logs each request's full URL at INFO; ours say what may be said.
logging.getLogger("httpx").setLevel(logging.WARNING)

TRANSPORT: httpx.BaseTransport | None = None
SLEEP = time.sleep


class QboError(Exception):
    """``code`` is a short machine word; messages never carry a response body."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class TokenRefused(QboError):
    """The token endpoint refused the grant (``invalid_grant``): the code was already
    used or has expired, or the refresh token is no longer valid."""


class Unauthorized(QboError):
    """The API answered 401 for this access token."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime | None


def _client(read_timeout: float) -> httpx.Client:
    timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT)
    return httpx.Client(transport=TRANSPORT, timeout=timeout)


def _retry_after(response: httpx.Response | None, fallback: float) -> float:
    if response is not None:
        value = response.headers.get("retry-after", "")
        if value.isdigit():
            return min(int(value), BACKOFF_CAP_SECONDS)
    return fallback


def _send(operation: str, build: Any, *, read_timeout: float) -> httpx.Response:
    """``build(client)`` sends one request. Retries 429, 5xx and transport errors."""
    delay = BACKOFF_FIRST_SECONDS
    with _client(read_timeout) as client:
        for attempt in range(1, MAX_TRIES + 1):
            response: httpx.Response | None = None
            try:
                response = build(client)
            except httpx.TransportError as exc:
                log.warning(
                    "qbo %s transport_error=%s try=%d", operation, type(exc).__name__, attempt
                )
            else:
                log.info(
                    "qbo %s status=%d tid=%s try=%d",
                    operation,
                    response.status_code,
                    response.headers.get(TID_HEADER, "-"),
                    attempt,
                )
                if response.status_code != 429 and response.status_code < 500:
                    return response
            if attempt == MAX_TRIES:
                break
            SLEEP(_retry_after(response, delay))
            delay = min(delay * 2, BACKOFF_CAP_SECONDS)
    raise QboError("unavailable", None if response is None else response.status_code)


def _body(response: httpx.Response) -> Any:
    try:
        return json_loads(response.content)
    except ValueError:
        return None


# --- OAuth token endpoints ------------------------------------------------------------------------


def _token_set(response: httpx.Response, now: datetime) -> TokenSet:
    body = _body(response)
    if response.status_code != 200 or not isinstance(body, dict):
        error = body.get("error") if isinstance(body, dict) else None
        if error == "invalid_grant":
            raise TokenRefused("invalid_grant", response.status_code)
        raise QboError("token_endpoint_error", response.status_code)
    try:
        refresh_in = body.get("x_refresh_token_expires_in")
        return TokenSet(
            access_token=str(body["access_token"]),
            refresh_token=str(body["refresh_token"]),
            access_expires_at=now + timedelta(seconds=int(body["expires_in"])),
            refresh_expires_at=(
                None if refresh_in is None else now + timedelta(seconds=int(refresh_in))
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise QboError("token_response_unreadable", response.status_code) from None


def _token_request(settings: Settings, operation: str, form: dict[str, str]) -> TokenSet:
    now = datetime.now(UTC)
    response = _send(
        operation,
        lambda c: c.post(
            TOKEN_URL,
            data=form,
            auth=(settings.qbo_client_id or "", settings.qbo_client_secret or ""),
            headers={"Accept": "application/json"},
        ),
        read_timeout=TOKEN_TIMEOUT,
    )
    return _token_set(response, now)


def exchange_code(settings: Settings, code: str) -> TokenSet:
    return _token_request(
        settings,
        "token_exchange",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.qbo_redirect_uri or "",
        },
    )


def refresh_tokens(settings: Settings, refresh_token: str) -> TokenSet:
    """Every response replaces **both** tokens: Intuit rotates the refresh token."""
    return _token_request(
        settings, "token_refresh", {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )


def revoke(settings: Settings, token: str) -> bool:
    """Revoke at Intuit. ``False`` when Intuit did not confirm; the caller clears the
    local tokens either way."""
    try:
        response = _send(
            "token_revoke",
            lambda c: c.post(
                REVOKE_URL,
                json={"token": token},
                auth=(settings.qbo_client_id or "", settings.qbo_client_secret or ""),
                headers={"Accept": "application/json"},
            ),
            read_timeout=TOKEN_TIMEOUT,
        )
    except QboError:
        return False
    return response.status_code == 200


# --- Accounting API -------------------------------------------------------------------------------


def api_get(
    *,
    environment: str,
    realm_id: str,
    access_token: str,
    resource: str,
    params: dict[str, str] | None = None,
    operation: str,
) -> Any:
    """GET ``/v3/company/{realm_id}/{resource}``. ``operation`` names the call in the
    log (an entity or verb, never a query text). Returns the parsed body."""
    url = f"{API_BASE_URLS[environment]}/v3/company/{realm_id}/{resource}"
    query = {"minorversion": str(MINOR_VERSION), **(params or {})}
    response = _send(
        operation,
        lambda c: c.get(
            url,
            params=query,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        ),
        read_timeout=READ_TIMEOUT,
    )
    if response.status_code == 401:
        raise Unauthorized("unauthorized", 401)
    body = _body(response)
    if response.status_code != 200 or body is None:
        raise QboError("api_error", response.status_code)
    return body
