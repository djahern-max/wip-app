"""Test harness.

Requires a Postgres created by ``db/init/01_roles.sh`` (``make db-up``). Two URLs:
- TEST_DATABASE_OWNER_URL  app_owner: runs migrations, seeds firm/tenant/user rows
- TEST_DATABASE_URL        app_rw:    what the application uses; RLS applies

The schema is migrated to head once per session and torn down to base at the end.
Seed: one firm with tenants A and B (plus an empty tenant "new" for the
first-membership case), a second firm with tenant C, one user per role, and one
orphan (an entry row with no firm_membership, D-15 owner condition).

Firm users hold a ``firm_membership`` and entry rows (``membership.role`` NULL) in
their tenants; client users hold client-role rows (D-15).

Harness-only behaviour (F02.1): the probe router is mounted here, never by the
application; every ``TestClient`` gets its own socket address so IP throttle
buckets do not collide across tests; every response body is recorded for the
end-of-run scan; the engine keeps parameter logging on so the log-leak test can
inspect statements (production hides parameters, see ``test_hygiene``).
"""

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, text

import app.core.db as app_db
from app.core.auth import CSRF_HEADER
from app.core.config import Settings, get_settings
from app.core.crypto import get_keyring
from app.core.db import create_app_engine, tenant_session, untenanted_session
from app.core.security import (
    hash_password,
    hash_recovery_code,
    new_recovery_codes,
    new_totp_secret,
    totp_code_at,
)
from app.main import create_app
from app.tenancy.models import (
    FIRM_ROLES,
    Firm,
    FirmMembership,
    Membership,
    RlsProbe,
    Role,
    Tenant,
    User,
)
from tests import _env, csv_source
from tests._env import OWNER_URL, RW_URL
from tests.leaks import record_secret
from tests.logcapture import ensure_capture
from tests.probes import build_probe_router
from tests.responses import record_response

# F03: the test-only CSV source kind (never registered by the application).
csv_source.register()

# Every other setting keeps its coded default: a variable exported in the developer's
# shell (or the CI job's DATABASE_OWNER_URL) does not reach the application under test.
for _name in Settings.model_fields:
    if _name.upper() not in _env.ENVIRONMENT:
        os.environ.pop(_name.upper(), None)
get_settings.cache_clear()

# Harness override (owner answer to call 7): keep bound parameters in SQL log lines
# so test_zz_log_leaks can prove no secret is ever sent in clear. Production keeps
# PRODUCTION_ENGINE_OPTIONS (hide_parameters=True, echo=False).
app_db.ENGINE_OPTIONS["hide_parameters"] = False

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSRF = {CSRF_HEADER: "fetch"}
COOKIE = get_settings().session_cookie_name
APP_ORIGIN = get_settings().app_base_url.rstrip("/")


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
    # Built like the application's engine (F03): the same JSON codec, so what a
    # test reads from a JSONB column is what the application would read.
    engine = create_app_engine(OWNER_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def rw_engine() -> Iterator[Engine]:
    engine = create_app_engine(RW_URL)
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
    firm_role: Role | None = None
    tenants: tuple[uuid.UUID, ...] = ()  # entry rows (firm user) or client rows
    memberships: dict[uuid.UUID, Role] = field(default_factory=dict)  # client rows only


@dataclass(frozen=True)
class Seed:
    firm_id: uuid.UUID
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    tenant_new: uuid.UUID  # same firm, no members
    other_firm_id: uuid.UUID
    tenant_c: uuid.UUID  # other firm
    user_id: uuid.UUID  # legacy F01 staff user: entry rows in A and B, no password
    users: dict[str, SeedUser]


# key → (role, tenants, totp enrolled). Passwords are derived from the key.
# A firm role means a firm_membership plus entry rows in the tenants.
ROLE_USERS: dict[str, tuple[Role | None, tuple[str, ...], bool]] = {
    "firm_admin": (Role.firm_admin, ("a", "b"), True),
    "firm_staff": (Role.firm_staff, ("a", "b"), True),
    "client_admin": (Role.client_admin, ("a",), False),
    "client_pm": (Role.client_pm, ("a",), False),
    "client_viewer": (Role.client_viewer, ("a",), False),
    "client_admin_b": (Role.client_admin, ("b",), False),
    "firm_nototp": (Role.firm_staff, ("a",), False),  # firm user, nothing enrolled: no login
    "recover_me": (Role.firm_staff, ("a", "b"), True),  # recovery-code test
    "replay_me": (Role.firm_staff, ("a",), True),  # TOTP replay test
    "rotate_me": (Role.firm_admin, ("a", "b"), True),  # key-rotation test; second admin
    "lockme": (Role.client_viewer, ("a",), False),  # lockout test
    "scratch": (None, (), False),  # no membership; target of admin routes
    "orphan": (None, ("a",), False),  # entry row in A, no firm_membership (D-15 orphan)
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
        s.add(FirmMembership(firm_id=firm.id, user_id=legacy.id, role=Role.firm_staff))
        tenant_ids = {k: t.id for k, t in tenants.items()}
        users: dict[str, SeedUser] = {}
        pending_firm_rows: list[FirmMembership] = []  # inserted after the users exist
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
            firm_role = role if role in FIRM_ROLES else None
            if firm_role is not None:
                pending_firm_rows.append(
                    FirmMembership(firm_id=firm.id, user_id=uid, role=firm_role)
                )
            users[key] = SeedUser(
                id=uid,
                email=u.email,
                password=pw,
                totp_secret=secret,
                recovery_codes=codes,
                firm_role=firm_role,
                tenants=tuple(tenant_ids[t] for t in tenant_keys),
                memberships=(
                    {tenant_ids[t]: role for t in tenant_keys}
                    if role is not None and firm_role is None
                    else {}
                ),
            )
        s.flush()
        s.add_all(pending_firm_rows)
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
            s.add(Membership(tenant_id=tid, user_id=ids.user_id, role=None))
            s.add(RlsProbe(tenant_id=tid, label=label))
    for su in users.values():
        for tid in su.tenants:
            with tenant_session(owner_engine, tid) as s:
                s.add(Membership(tenant_id=tid, user_id=su.id, role=su.memberships.get(tid)))
    return ids


@pytest.fixture
def scratch_db_url(owner_engine: Engine) -> Iterator[str]:
    """An empty database for migration and bootstrap tests, dropped afterwards."""
    from sqlalchemy import make_url

    name = f"wip_mig_{uuid.uuid4().hex[:8]}"
    with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(OWNER_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


# --- HTTP clients and login helpers --------------------------------------------------------

_ip_counter = 0


def next_ip() -> str:
    """A fresh socket address per client: throttle buckets never collide."""
    global _ip_counter
    _ip_counter += 1
    n = _ip_counter
    return f"10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


class RecordingClient(TestClient):
    """Records every response body for ``test_zz_response_scan``."""

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        r = super().request(method, url, *args, **kwargs)
        record_response(method, str(url), r.status_code, r.content)
        return r


def make_client(ip: str | None = None) -> RecordingClient:
    app = create_app()
    app.include_router(build_probe_router(), prefix="/api")  # test-only routes
    # https so the Secure cookie is sent back by the client's cookie jar.
    ip = ip or next_ip()
    c = RecordingClient(app, base_url="https://testserver", client=(ip, 50000))
    c.ip = ip  # type: ignore[attr-defined]
    return c


def client_ip_of(client: TestClient) -> str:
    return client.ip  # type: ignore[attr-defined]


@pytest.fixture
def client(migrated_db: None) -> Iterator[TestClient]:
    with make_client() as c:
        yield c


@pytest.fixture
def clients(migrated_db: None) -> Iterator[Callable[[], TestClient]]:
    """Factory for several independent clients (separate cookie jars) in one test."""
    with ExitStack() as stack:
        yield lambda **kw: stack.enter_context(make_client(**kw))


def record_cookie(client: TestClient) -> str | None:
    token = client.cookies.get(COOKIE)
    record_secret("session_token", token)
    return token


def totp_code(secret: str, at: datetime | None = None) -> str:
    code = totp_code_at(secret, at or datetime.now(UTC))
    record_secret("totp_code", code)
    return code


def activation_token_from(url: str) -> str:
    parsed = urlparse(url)
    assert parsed.query == "", "the activation token must not travel in a query string"
    token = parse_qs(parsed.fragment)["token"][0]
    record_secret("activation_token", token)
    return token


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
