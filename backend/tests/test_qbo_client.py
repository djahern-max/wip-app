"""F05: the HTTP module. Backoff, ``Retry-After``, Decimal-safe parsing, quiet logs."""

from decimal import Decimal

import httpx
import pytest

from app.core.config import get_settings
from app.integrations.qbo import client as qbo_client
from app.integrations.qbo.constants import MAX_TRIES, MINOR_VERSION
from tests.leaks import record_secret
from tests.logcapture import RECORDS


def _install(handler) -> list[float]:
    sleeps: list[float] = []
    qbo_client.TRANSPORT = httpx.MockTransport(handler)
    qbo_client.SLEEP = sleeps.append
    return sleeps


def _get(token: str = "access-for-client-test") -> object:
    record_secret("connection_token", token)
    return qbo_client.api_get(
        environment="sandbox",
        realm_id="123",
        access_token=token,
        resource="query",
        params={"query": "select * from Invoice"},
        operation="Invoice",
    )


def test_amounts_are_decimals_and_never_floats() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b'{"QueryResponse":{"Invoice":[{"TotalAmt":100.1}]}}')

    _install(handler)
    body = _get()
    amount = body["QueryResponse"]["Invoice"][0]["TotalAmt"]
    assert isinstance(amount, Decimal) and amount == Decimal("100.1")
    assert str(amount) == "100.1"
    assert seen[0].url.params["minorversion"] == str(MINOR_VERSION)
    assert seen[0].url.host == "sandbox-quickbooks.api.intuit.com"


def test_429_honours_retry_after_then_succeeds() -> None:
    answers = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, json={"ok": True}),
    ]
    sleeps = _install(lambda request: answers.pop(0))
    assert _get() == {"ok": True}
    assert sleeps == [7, 2.0]  # the header, then the doubled fallback


def test_gives_up_after_max_tries_and_names_no_body() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"secret-ish": "body"})

    sleeps = _install(handler)
    with pytest.raises(qbo_client.QboError) as err:
        _get()
    assert len(calls) == MAX_TRIES and len(sleeps) == MAX_TRIES - 1
    assert err.value.code == "unavailable" and err.value.status == 500
    assert "body" not in str(err.value)


def test_transport_errors_are_retried() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={})

    _install(handler)
    assert _get() == {}


def test_401_is_its_own_error_and_is_not_retried_here() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={})

    _install(handler)
    with pytest.raises(qbo_client.Unauthorized):
        _get()
    assert len(calls) == 1


def test_log_lines_carry_operation_status_and_tid_only() -> None:
    token = "access-log-line-check-0123456789"
    before = len(RECORDS)
    _install(
        lambda request: httpx.Response(
            200, json={"CompanyName": "Acme Secret Name"}, headers={"intuit_tid": "tid-abc"}
        )
    )
    _get(token)
    lines = [m for n, m in RECORDS[before:] if n == "app.qbo"]
    assert lines == ["qbo Invoice status=200 tid=tid-abc try=1"]
    everything = "\n".join(m for _, m in RECORDS[before:])
    assert token not in everything and "Acme Secret Name" not in everything
    assert "select * from" not in everything  # the query text is not logged either


def test_token_endpoint_sends_the_secret_only_as_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"error": "invalid_grant"})

    _install(handler)
    with pytest.raises(qbo_client.TokenRefused):
        qbo_client.exchange_code(get_settings(), "some-code")
    secret = get_settings().qbo_client_secret
    assert secret and secret not in str(seen[0].url) and secret.encode() not in seen[0].content
    assert seen[0].headers["authorization"].startswith("Basic ")
