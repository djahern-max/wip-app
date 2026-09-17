"""Session cookie, storage, rotation, expiry, logout, CSRF, header ignored (F02)."""

import hashlib
import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from app.core.auth import CSRF_HEADER
from app.core.db import untenanted_session
from app.tenancy.models import UserSession
from tests.conftest import (
    COOKIE,
    CSRF,
    Seed,
    password_login,
    record_cookie,
    reset_totp_counter,
    totp_code,
)


def _hashes(engine: Engine, user_id: uuid.UUID) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(UserSession.token_hash).where(UserSession.user_id == user_id)
            ).scalars()
        )


def test_cookie_flags_and_hash_only_storage(
    client: TestClient, seed: Seed, rw_engine: Engine
) -> None:
    su = seed.users["client_viewer"]
    r = password_login(client, su)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered
    assert "path=/" in lowered
    token = record_cookie(client)
    hashes = _hashes(rw_engine, su.id)
    assert token not in hashes
    assert hashlib.sha256(token.encode()).hexdigest() in hashes


def test_session_id_rotates_on_login_and_on_totp_verification(
    client: TestClient, seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    su = seed.users["firm_admin"]
    password_login(client, su)
    first = record_cookie(client)
    password_login(client, su)  # logging in again replaces the session
    second = record_cookie(client)
    assert first != second
    assert hashlib.sha256(first.encode()).hexdigest() not in _hashes(rw_engine, su.id)
    reset_totp_counter(owner_engine, su.id)
    r = client.post("/api/auth/totp/verify", json={"code": totp_code(su.totp_secret)}, headers=CSRF)
    assert r.status_code == 200
    third = record_cookie(client)
    assert third != second
    hashes = _hashes(rw_engine, su.id)
    assert hashlib.sha256(second.encode()).hexdigest() not in hashes
    assert hashlib.sha256(third.encode()).hexdigest() in hashes


def _age_session(engine: Engine, token: str, column: str) -> None:
    with untenanted_session(engine) as s:
        s.execute(
            text(
                f"UPDATE session SET {column} = now() - interval '1 second' WHERE token_hash = :h"
            ),
            {"h": hashlib.sha256(token.encode()).hexdigest()},
        )


def test_idle_timeout_ends_the_session(
    client: TestClient, seed: Seed, owner_engine: Engine, rw_engine: Engine
) -> None:
    su = seed.users["client_viewer"]
    password_login(client, su)
    token = record_cookie(client)
    assert client.get("/api/session/me").status_code == 200
    with untenanted_session(owner_engine) as s:
        s.execute(
            text(
                "UPDATE session SET last_seen_at = now() - interval '61 minutes' "
                "WHERE token_hash = :h"
            ),
            {"h": hashlib.sha256(token.encode()).hexdigest()},
        )
    r = client.get("/api/session/me")
    assert (r.status_code, r.json()) == (401, {"detail": "session expired"})
    assert hashlib.sha256(token.encode()).hexdigest() not in _hashes(rw_engine, su.id)


def test_absolute_timeout_ends_the_session(
    client: TestClient, seed: Seed, owner_engine: Engine, rw_engine: Engine
) -> None:
    su = seed.users["client_viewer"]
    password_login(client, su)
    token = record_cookie(client)
    _age_session(owner_engine, token, "expires_at")
    assert client.get("/api/session/me").status_code == 401
    assert hashlib.sha256(token.encode()).hexdigest() not in _hashes(rw_engine, su.id)


def test_logout_deletes_the_row(client: TestClient, seed: Seed, rw_engine: Engine) -> None:
    su = seed.users["client_viewer"]
    password_login(client, su)
    token = record_cookie(client)
    r = client.post("/api/auth/logout", headers=CSRF)
    assert r.status_code == 204
    assert "set-cookie" in r.headers  # cleared
    assert hashlib.sha256(token.encode()).hexdigest() not in _hashes(rw_engine, su.id)
    client.cookies.set(COOKIE, token)  # a replayed cookie is dead
    assert client.get("/api/session/me").status_code == 401


def test_state_changing_request_without_csrf_header_is_rejected(
    client: TestClient, seed: Seed, login_as: Callable[..., TestClient]
) -> None:
    su = seed.users["client_viewer"]
    r = client.post("/api/auth/login", json={"email": su.email, "password": su.password})
    assert r.status_code == 403
    assert CSRF_HEADER in r.json()["detail"]
    c = login_as("firm_admin")
    r = c.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_b)})
    assert r.status_code == 403
    assert c.get("/api/session/me").json()["active_tenant_id"] == str(seed.tenant_a)


def test_tenant_header_has_no_effect(login_as: Callable[..., TestClient], seed: Seed) -> None:
    """A session active in tenant A sending tenant B's id in the old header reads A."""
    for key in ("client_pm", "firm_admin"):
        c = login_as(key)
        r = c.get("/api/_probe/rows", headers={"X-Tenant-Id": str(seed.tenant_b)})
        assert (r.status_code, r.json()) == (200, ["a-row"])
        r = c.get("/api/session/me", headers={"X-Tenant-Id": str(seed.tenant_b)})
        assert r.json()["active_tenant_id"] == str(seed.tenant_a)


def test_request_id_is_echoed(client: TestClient) -> None:
    r = client.get("/api/health")
    assert len(r.headers["X-Request-Id"]) == 32


def test_session_deleted_by_an_admin_is_401_on_its_very_next_request(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    victim = login_as("client_viewer")
    assert victim.get("/api/session/me").status_code == 200
    admin = login_as("firm_admin")
    r = admin.post(
        f"/api/admin/users/{seed.users['client_viewer'].id}/activation-link", headers=CSRF
    )
    assert r.status_code == 200
    from tests.conftest import activation_token_from

    activation_token_from(r.json()["activation_url"])
    assert victim.get("/api/session/me").status_code == 401  # the very next request


def test_last_seen_is_written_only_when_stale(
    client: TestClient, seed: Seed, owner_engine: Engine, rw_engine: Engine
) -> None:
    su = seed.users["client_viewer"]
    password_login(client, su)
    token = record_cookie(client)
    h = hashlib.sha256(token.encode()).hexdigest()

    def last_seen():
        with rw_engine.connect() as conn:
            return conn.execute(
                select(UserSession.last_seen_at).where(UserSession.token_hash == h)
            ).scalar_one()

    before = last_seen()
    assert client.get("/api/session/me").status_code == 200
    assert last_seen() == before  # fresh: no write
    with untenanted_session(owner_engine) as s:
        s.execute(
            text(
                "UPDATE session SET last_seen_at = now() - interval '61 seconds' "
                "WHERE token_hash = :h"
            ),
            {"h": h},
        )
    stale = last_seen()
    assert client.get("/api/session/me").status_code == 200
    assert last_seen() > stale  # stale by more than SESSION_TOUCH_SECONDS: written
