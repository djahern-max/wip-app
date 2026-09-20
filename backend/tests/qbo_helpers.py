"""A stand-in for Intuit behind ``httpx.MockTransport`` (F05). CI never calls
Intuit: ``conftest`` installs ``refuse_all_transport`` for every test, and a test
that needs the token endpoint or the API installs a ``FakeIntuit`` over it.

Every token, code and ``state`` that passes through here is registered with
``tests.leaks`` so the end-of-run scans can prove none reached a log line, an audit
row or a response body.
"""

import base64
import json
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import httpx

from app.integrations.qbo import client as qbo_client
from app.integrations.qbo.constants import REVOKE_URL, TOKEN_URL
from tests._env import QBO_CLIENT_SECRET
from tests.leaks import record_secret

record_secret("client_secret", QBO_CLIENT_SECRET)


def refuse_all_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call to {request.url.host}")

    return httpx.MockTransport(handler)


class FakeIntuit:
    """Token endpoint (exchange, refresh with rotation, revoke) and the API calls a
    test registers with ``on_api``. Thread-safe: the concurrent-refresh test calls it
    from two threads."""

    def __init__(self, *, realm_id: str | None = None, company_name: str = "Sandbox Co"):
        # One company belongs to one tenant (unique index), so each fake is a new company.
        self.realm_id = realm_id or "91300" + str(secrets.randbelow(10**11)).zfill(11)
        self.company_name = company_name
        self.codes: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.access_tokens: set[str] = set()
        self.revoked: list[str] = []
        self.requests: list[tuple[str, str]] = []  # (method, path)
        self.refresh_calls = 0
        self.refuse_refresh = False
        self.expires_in = 3600
        self.before_refresh: Callable[[], None] | None = None
        self.api_handlers: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self._lock = threading.Lock()

    # --- what a test does as "the user at Intuit" ----------------------------------
    def new_code(self) -> str:
        code = "code-" + secrets.token_urlsafe(24)
        record_secret("oauth_code", code)
        self.codes.add(code)
        return code

    def on_api(self, resource: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.api_handlers[resource] = handler

    # --- transport ----------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _issue(self) -> dict:
        access = "access-" + secrets.token_urlsafe(32)
        refresh = "refresh-" + secrets.token_urlsafe(32)
        record_secret("connection_token", access)
        record_secret("connection_token", refresh)
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        return {
            "token_type": "bearer",
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": self.expires_in,
            "x_refresh_token_expires_in": 8726400,
        }

    def _basic_ok(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        expected = base64.b64encode(f"test-qbo-client-id:{QBO_CLIENT_SECRET}".encode()).decode()
        return header == f"Basic {expected}"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        self.requests.append((request.method, urlparse(url).path))
        if url == TOKEN_URL:
            return self._token(request)
        if url == REVOKE_URL:
            assert self._basic_ok(request)
            self.revoked.append(json.loads(request.content)["token"])
            return httpx.Response(200)
        return self._api(request)

    def _token(self, request: httpx.Request) -> httpx.Response:
        assert self._basic_ok(request), "token endpoint needs the client credentials"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form["grant_type"] == "authorization_code":
            with self._lock:
                if form["code"] not in self.codes:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.codes.discard(form["code"])
                return httpx.Response(200, json=self._issue())
        if self.before_refresh is not None:
            self.before_refresh()
        with self._lock:
            self.refresh_calls += 1
            if self.refuse_refresh or form["refresh_token"] not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            # Rotation: the presented refresh token stops working.
            self.refresh_tokens.discard(form["refresh_token"])
            return httpx.Response(200, json=self._issue())

    def _api(self, request: httpx.Request) -> httpx.Response:
        parts = urlparse(str(request.url)).path.split("/")  # '', v3, company, realm, resource…
        realm, resource = parts[3], "/".join(parts[4:])
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.access_tokens:
            return httpx.Response(401, json={"fault": {"type": "AUTHENTICATION"}})
        if realm != self.realm_id:
            return httpx.Response(403, json={"fault": {"type": "AuthorizationFault"}})
        if resource in self.api_handlers:
            return self.api_handlers[resource](request)
        if resource == f"companyinfo/{realm}":
            return httpx.Response(
                200,
                json={"CompanyInfo": {"Id": "1", "CompanyName": self.company_name}},
                headers={"intuit_tid": "tid-" + secrets.token_hex(4)},
            )
        return httpx.Response(404, json={})


@contextmanager
def installed(fake: FakeIntuit) -> Iterator[FakeIntuit]:
    saved = qbo_client.TRANSPORT
    qbo_client.TRANSPORT = fake.transport()
    try:
        yield fake
    finally:
        qbo_client.TRANSPORT = saved


def state_from(authorization_url: str) -> str:
    state = parse_qs(urlparse(authorization_url).query)["state"][0]
    record_secret("oauth_state", state)
    return state


def connect_directly(engine, tenant_id, fake: FakeIntuit, *, access_valid_for: int = 3600):
    """A connected ``connection`` row holding tokens the fake will honour, without the
    browser flow. Returns the connection id."""
    from datetime import UTC, datetime, timedelta

    from app.core.db import tenant_session
    from app.ingest.connections import get_or_create_connection, set_connection_tokens

    issued = fake._issue()
    now = datetime.now(UTC)
    with tenant_session(engine, tenant_id) as db:
        connection = get_or_create_connection(db, tenant_id, "qbo")
        connection.realm_id = fake.realm_id
        connection.environment = "sandbox"
        connection.company_name = fake.company_name
        set_connection_tokens(
            db,
            connection,
            access_token=issued["access_token"],
            refresh_token=issued["refresh_token"],
            token_expires_at=now + timedelta(seconds=access_valid_for),
            refresh_token_expires_at=now + timedelta(days=100),
            actor_user_id=None,
            actor_role=None,
        )
        return connection.id
