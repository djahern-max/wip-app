"""Test harness.

Requires a Postgres created by ``db/init/01_roles.sh`` (``make db-up``). Two URLs:
- TEST_DATABASE_OWNER_URL  app_owner: runs migrations, seeds firm/tenant/user rows
- TEST_DATABASE_URL        app_rw:    what the application uses; RLS applies

The schema is migrated to head once per session and torn down to base at the end.
Seed: one firm with tenants A and B (plus an empty tenant "new" for the
first-membership case), a second firm with tenant C, and one user per role.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, create_engine, text

from app.core.auth import CSRF_HEADER  # noqa: E402  (after tests._env on purpose)
from app.core.config import get_settings  # noqa: E402
from app.core.crypto import get_keyring  # noqa: E402
from app.core.db import tenant_session, untenanted_session  # noqa: E402
from app.core.security import (  # noqa: E402
    hash_password,
    hash_recovery_code,
    new_recovery_codes,
    new_totp_secret,
    totp_code_at,
)
from app.main import create_app  # noqa: E402
from app.tenancy.models import Firm, Membership, RlsProbe, Role, Tenant, User  # noqa: E402
from tests import _env
from tests._env import OWNER_URL, RW_URL
from tests.leaks import record_secret
from tests.logcapture import ensure_capture

assert _env.ACTIVE_KEY_ID  # tests._env must be imported before settings are read
get_settings.cache_clear()

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSRF = {CSRF_HEADER: "fetch"}
COOKIE = get_settings().session_cookie_name


def alembic_config(owner_url: str) -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    cfg.set_main_option("owner_url", owner_url)
    return cfg


@pytest.fixture(autouse=True)
def _capture_logs() -> None:
    ensure_capture()


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    engine = create_engine(OWNER_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def rw_engine() -> Iterator[Engine]:
    engine = create_engine(RW_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated_db(owner_engine: Engine) -> Iterator[None]:
    cfg = alembic_config(OWNER_URL)
    command.downgrade(cfg, "base")  # clean slate if a previous run aborted
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


# --- seed -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedUser:
    id: uuid.UUID
    email: str
    password: str
    totp_secret: str | None
    recovery_codes: tuple[str, ...]
    memberships: dict[uuid.UUID, Role] = field(default_factory=dict)


@dataclass(frozen=True)
class Seed:
    firm_id: uuid.UUID
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    tenant_new: uuid.UUID  # same firm, no members
    other_firm_id: uuid.UUID
    tenant_c: uuid.UUID  # other firm
    user_id: uuid.UUID  # legacy F01 staff user: memberships in A and B, no password
    users: dict[str, SeedUser]


# key → (role, tenants, totp enrolled). Passwords are derived from the key.
ROLE_USERS: dict[str, tuple[Role | None, tuple[str, ...], bool]] = {
    "firm_admin": (Role.firm_admin, ("a", "b"), True),
    "firm_staff": (Role.firm_staff, ("a", "b"), True),
    "client_admin": (Role.client_admin, ("a",), False),
    "client_pm": (Role.client_pm, ("a",), False),
    "client_viewer": (Role.client_viewer, ("a",), False),
    "client_admin_b": (Role.client_admin, ("b",), False),
    "firm_nototp": (Role.firm_staff, ("a",), False),  # firm role, nothing enrolled
    "enrol_me": (Role.firm_staff, ("a",), False),  # enrolment flow test
    "recover_me": (Role.firm_staff, ("a", "b"), True),  # recovery-code test
    "replay_me": (Role.firm_staff, ("a",), True),  # TOTP replay test
    "rotate_me": (Role.firm_admin, ("a", "b"), True),  # key-rotation test
    "lockme": (Role.client_viewer, ("a",), False),  # lockout test
    "scratch": (None, (), False),  # no membership; target of admin routes
}


def password_for(key: str) -> str:
    return f"pw-{key}-correct-horse-battery"


@pytest.fixture(scope="session")
def seed(migrated_db: None, owner_engine: Engine) -> Seed:
    now = datetime.now(UTC)
    keyring = get_keyring()
    firm = Firm(name="Test CPA Practice")
    other = Firm(name="Other Practice")
    with untenanted_session(owner_engine) as s:
        s.add_all([firm, other])
        s.flush()
        tenants = {
            "a": Tenant(firm_id=firm.id, name="Tenant A", slug="tenant-a"),
            "b": Tenant(firm_id=firm.id, name="Tenant B", slug="tenant-b"),
            "new": Tenant(firm_id=firm.id, name="Tenant New", slug="tenant-new"),
            "c": Tenant(firm_id=other.id, name="Tenant C", slug="tenant-c"),
        }
        legacy = User(email="staff@example.test", display_name="Firm Staff")
        s.add_all([*tenants.values(), legacy])
        s.flush()
        tenant_ids = {k: t.id for k, t in tenants.items()}
        users: dict[str, SeedUser] = {}
        for key, (role, tenant_keys, totp) in ROLE_USERS.items():
            uid = uuid.uuid4()
            pw = password_for(key)
            record_secret("password", pw)
            u = User(
                id=uid,
                email=f"{key.replace('_', '-')}@example.test",
                display_name=key,
                password_hash=hash_password(pw),
                password_changed_at=now,
            )
            secret = None
            codes: tuple[str, ...] = ()
            if totp:
                secret = new_totp_secret()
                record_secret("totp_secret", secret)
                u.totp_key_id, u.totp_secret_enc = keyring.encrypt(secret.encode(), aad=uid.bytes)
                u.totp_enrolled_at = now
                codes = tuple(new_recovery_codes())
                for c in codes:
                    record_secret("recovery_code", c)
                u.recovery_code_hashes = [hash_recovery_code(c) for c in codes]
            s.add(u)
            users[key] = SeedUser(
                id=uid,
                email=u.email,
                password=pw,
                totp_secret=secret,
                recovery_codes=codes,
                memberships={tenant_ids[t]: role for t in tenant_keys} if role else {},
            )
        s.flush()
        ids = Seed(
            firm_id=firm.id,
            tenant_a=tenant_ids["a"],
            tenant_b=tenant_ids["b"],
            tenant_new=tenant_ids["new"],
            other_firm_id=other.id,
            tenant_c=tenant_ids["c"],
            user_id=legacy.id,
            users=users,
        )
    for tid, label in ((ids.tenant_a, "a-row"), (ids.tenant_b, "b-row")):
        with tenant_session(owner_engine, tid) as s:
            s.add(Membership(tenant_id=tid, user_id=ids.user_id, role=Role.firm_staff))
            s.add(RlsProbe(tenant_id=tid, label=label))
    for su in users.values():
        for tid, role in su.memberships.items():
            with tenant_session(owner_engine, tid) as s:
                s.add(Membership(tenant_id=tid, user_id=su.id, role=role))
    return ids


# --- HTTP clients and login helpers --------------------------------------------------------


def make_client() -> TestClient:
    # https so the Secure cookie is sent back by the client's cookie jar.
    return TestClient(create_app(), base_url="https://testserver")


@pytest.fixture
def client(migrated_db: None) -> Iterator[TestClient]:
    with make_client() as c:
        yield c


@pytest.fixture
def clients(migrated_db: None) -> Iterator[Callable[[], TestClient]]:
    """Factory for several independent clients (separate cookie jars) in one test."""
    with ExitStack() as stack:
        yield lambda: stack.enter_context(make_client())


def record_cookie(client: TestClient) -> str | None:
    token = client.cookies.get(COOKIE)
    record_secret("session_token", token)
    return token


def totp_code(secret: str, at: datetime | None = None) -> str:
    code = totp_code_at(secret, at or datetime.now(UTC))
    record_secret("totp_code", code)
    return code


def reset_totp_counter(owner_engine: Engine, user_id: uuid.UUID) -> None:
    """Tests log the same TOTP user in several times inside one 30 s step; the replay
    guard would reject the second code. Clearing the stored counter between logins
    keeps the guard testable on its own (see test_auth)."""
    with untenanted_session(owner_engine) as s:
        s.execute(
            text('UPDATE "user" SET totp_last_counter = NULL WHERE id = :id'), {"id": user_id}
        )


def password_login(client: TestClient, user: SeedUser, password: str | None = None) -> Response:
    r = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": password or user.password},
        headers=CSRF,
    )
    record_cookie(client)
    return r


def full_login(
    client: TestClient,
    seed: Seed,
    key: str,
    owner_engine: Engine,
    tenant: uuid.UUID | None,
) -> dict:
    """Password → TOTP (if enrolled) → tenant selection (if asked). Returns /me."""
    user = seed.users[key]
    r = password_login(client, user)
    assert r.status_code == 200, r.text
    data = r.json()
    if data["totp"] == "verify_required":
        reset_totp_counter(owner_engine, user.id)
        r = client.post(
            "/api/auth/totp/verify", json={"code": totp_code(user.totp_secret)}, headers=CSRF
        )
        assert r.status_code == 200, r.text
        record_cookie(client)
        data = r.json()
    if tenant is not None and data["active_tenant_id"] != str(tenant):
        r = client.post("/api/session/tenant", json={"tenant_id": str(tenant)}, headers=CSRF)
        assert r.status_code == 200, r.text
    r = client.get("/api/session/me")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def login_as(
    seed: Seed, owner_engine: Engine, clients: Callable[[], TestClient]
) -> Callable[..., TestClient]:
    """``login_as("client_pm")`` → a client logged in with tenant A active.
    ``tenant=None`` skips tenant selection (auto-selection still applies)."""

    def _login(key: str, tenant: uuid.UUID | None | str = "a") -> TestClient:
        c = clients()
        tid = seed.tenant_a if tenant == "a" else tenant
        full_login(c, seed, key, owner_engine, tid)
        return c

    return _login
