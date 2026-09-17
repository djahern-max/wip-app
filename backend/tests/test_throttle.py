"""Per-IP throttle and client-IP derivation (F02.1). Each TestClient has its own
socket address (conftest), so these tests own their buckets."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.auth import INVALID_CREDENTIALS, LOCKED
from app.auth import service
from app.core.config import get_settings
from tests.conftest import CSRF, Seed, make_client, password_login
from tests.leaks import record_secret

THRESHOLD = get_settings().ip_throttle_failures  # 20


def unknown_login(c: TestClient, **headers: str):
    email = f"nobody-{uuid.uuid4().hex[:10]}@example.test"
    record_secret("attempted_email", email)
    return c.post(
        "/api/auth/login",
        json={"email": email, "password": "whatever-whatever-1"},
        headers={**CSRF, **headers},
    )


def audit_rows(engine: Engine, ip: str) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT action, count(*) FROM firm_audit_log WHERE ip = :ip GROUP BY action"),
            {"ip": ip},
        ).all()
    return {a: n for a, n in rows}


def total_rows(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM firm_audit_log")).scalar_one()


def test_twenty_first_failure_is_throttled_and_further_attempts_write_nothing(
    client: TestClient, seed: Seed, rw_engine: Engine
) -> None:
    ip = client.ip  # type: ignore[attr-defined]
    for _ in range(THRESHOLD):
        r = unknown_login(client)
        assert (r.status_code, r.json()) == (401, {"detail": INVALID_CREDENTIALS})
    r = unknown_login(client)
    assert (r.status_code, r.json()) == (429, {"detail": LOCKED})
    assert audit_rows(rw_engine, ip) == {"login_failure": THRESHOLD, "ip_throttled": 1}
    before = total_rows(rw_engine)
    for _ in range(100):
        assert unknown_login(client).status_code == 429
    # A correct password from that IP is refused too, and writes nothing.
    assert password_login(client, seed.users["client_viewer"]).status_code == 429
    assert total_rows(rw_engine) == before
    assert audit_rows(rw_engine, ip) == {"login_failure": THRESHOLD, "ip_throttled": 1}
    # Another IP is unaffected.
    with make_client() as other:
        assert password_login(other, seed.users["client_viewer"]).status_code == 200


def test_throttled_request_performs_no_argon2_verification(
    client: TestClient, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(THRESHOLD):
        unknown_login(client)
    calls: list[int] = []

    def spy(*args, **kwargs):
        calls.append(1)
        raise AssertionError("argon2 verification ran for a throttled request")

    monkeypatch.setattr(service, "verify_password", spy)
    assert unknown_login(client).status_code == 429
    assert password_login(client, seed.users["client_viewer"]).status_code == 429
    assert calls == []


def test_failed_link_redemptions_count_toward_the_throttle(client: TestClient) -> None:
    for _ in range(THRESHOLD):
        token = "bogus-" + uuid.uuid4().hex
        record_secret("activation_token", token)
        r = client.post(
            "/api/auth/activate",
            json={"token": token, "new_password": "whatever-whatever-1"},
            headers=CSRF,
        )
        assert r.status_code == 400
    r = client.post(
        "/api/auth/activate",
        json={"token": "bogus-" + uuid.uuid4().hex, "new_password": "whatever-whatever-1"},
        headers=CSRF,
    )
    assert r.status_code == 429


# --- client IP -------------------------------------------------------------------------------


def last_failure_ip(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT ip FROM firm_audit_log WHERE action = 'login_failure' "
                "ORDER BY occurred_at DESC LIMIT 1"
            )
        ).scalar_one()


def test_forged_forwarded_for_is_ignored_without_a_trusted_proxy(
    client: TestClient, rw_engine: Engine
) -> None:
    assert get_settings().trusted_proxy_count == 0
    ip = client.ip  # type: ignore[attr-defined]
    for i in range(THRESHOLD):
        r = unknown_login(client, **{"X-Forwarded-For": f"203.0.113.{i}"})
        assert r.status_code == 401
        assert last_failure_ip(rw_engine) == ip
    # The bucket is the socket address: forging the header does not escape it.
    assert unknown_login(client, **{"X-Forwarded-For": "203.0.113.250"}).status_code == 429
    assert audit_rows(rw_engine, ip)["ip_throttled"] == 1


@pytest.fixture
def trusted_proxies() -> Iterator[None]:
    s = get_settings()
    s.trusted_proxy_count = 1
    try:
        yield
    finally:
        s.trusted_proxy_count = 0


def test_trusted_proxy_count_takes_the_nth_address_from_the_right(
    trusted_proxies: None, client: TestClient, rw_engine: Engine
) -> None:
    unknown_login(client, **{"X-Forwarded-For": "198.51.100.7"})
    assert last_failure_ip(rw_engine) == "198.51.100.7"
    unknown_login(client, **{"X-Forwarded-For": "1.1.1.1, 198.51.100.8"})
    assert last_failure_ip(rw_engine) == "198.51.100.8"  # the last hop the proxy appended


def test_missing_or_short_forwarded_for_falls_back_to_the_socket(
    trusted_proxies: None, client: TestClient, rw_engine: Engine
) -> None:
    ip = client.ip  # type: ignore[attr-defined]
    unknown_login(client)  # header missing
    assert last_failure_ip(rw_engine) == ip
    get_settings().trusted_proxy_count = 2
    unknown_login(client, **{"X-Forwarded-For": "198.51.100.9"})  # one entry, two proxies
    assert last_failure_ip(rw_engine) == ip
    unknown_login(client, **{"X-Forwarded-For": "198.51.100.10, 10.0.0.1"})
    assert last_failure_ip(rw_engine) == "198.51.100.10"
