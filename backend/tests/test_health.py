"""Health endpoint and the request-scoped tenant session dependency (now derived
from the server-side session, F02)."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.product import PRODUCT_NAME
from app.main import create_app
from tests.conftest import Seed


def test_health_ok(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "product": PRODUCT_NAME, "db": "ok"}


def test_health_degraded_when_db_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody:x@localhost:1/none")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            r = c.get("/api/health")
    finally:
        get_settings.cache_clear()
    assert r.status_code == 503
    assert r.json()["db"] == "unavailable"


def test_request_dependency_sets_tenant_context_from_session(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    a = login_as("client_pm").get("/api/_probe/rows")
    b = login_as("client_admin_b", tenant=seed.tenant_b).get("/api/_probe/rows")
    assert (a.status_code, a.json()) == (200, ["a-row"])
    assert (b.status_code, b.json()) == (200, ["b-row"])


def test_request_without_session_is_401(client: TestClient, seed: Seed) -> None:
    assert client.get("/api/_probe/rows").status_code == 401
    # The F01 header placeholder is gone: it neither authenticates nor selects.
    r = client.get("/api/_probe/rows", headers={"X-Tenant-Id": str(seed.tenant_a)})
    assert r.status_code == 401


def test_engine_is_not_module_level(rw_engine: Engine) -> None:
    """CLAUDE.md: no module-level or cached sessions."""
    import app.core.db as db

    assert not any(isinstance(v, Engine) for v in vars(db).values())
