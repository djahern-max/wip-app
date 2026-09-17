"""Login, lockout, TOTP gate, enrolment, replay, recovery codes (F02, D-10)."""

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from app.api.auth import INVALID_CREDENTIALS, LOCKED
from app.audit.models import AuditLog, FirmAuditLog
from app.core.db import tenant_session, untenanted_session
from app.tenancy.models import User
from tests.conftest import (
    CSRF,
    Seed,
    make_client,
    password_login,
    record_cookie,
    reset_totp_counter,
    totp_code,
)
from tests.leaks import record_secret


def firm_events(engine: Engine, user_id: uuid.UUID | None, action: str) -> list[FirmAuditLog]:
    with untenanted_session(engine) as s:
        stmt = select(FirmAuditLog).where(FirmAuditLog.action == action)
        if user_id is not None:
            stmt = stmt.where(FirmAuditLog.entity_id == str(user_id))
        rows = s.execute(stmt.order_by(FirmAuditLog.occurred_at)).scalars().all()
        s.expunge_all()
    return rows


def tenant_events(engine: Engine, tenant_id: uuid.UUID, action: str) -> list[AuditLog]:
    with tenant_session(engine, tenant_id) as s:
        rows = (
            s.execute(
                select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.occurred_at)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
    return rows


# --- password step ------------------------------------------------------------------------


def test_unknown_email_and_wrong_password_are_indistinguishable(
    client: TestClient, seed: Seed
) -> None:
    unknown = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.test", "password": "whatever-whatever"},
        headers=CSRF,
    )
    wrong = password_login(client, seed.users["client_pm"], password="wrong-wrong-wrong")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": INVALID_CREDENTIALS}
    assert "set-cookie" not in unknown.headers and "set-cookie" not in wrong.headers


def test_failed_login_writes_audit_rows(client: TestClient, seed: Seed, rw_engine: Engine) -> None:
    su = seed.users["client_viewer"]
    before = len(firm_events(rw_engine, su.id, "login_failure"))
    password_login(client, su, password="not-the-password")
    rows = firm_events(rw_engine, su.id, "login_failure")
    assert len(rows) == before + 1
    assert rows[-1].actor_user_id == su.id
    assert rows[-1].firm_id == seed.firm_id
    assert rows[-1].detail["step"] == "password"
    unknown = firm_events(rw_engine, None, "login_failure")
    client.post(
        "/api/auth/login",
        json={"email": "ghost@example.test", "password": "whatever-whatever"},
        headers=CSRF,
    )
    ghost = [r for r in firm_events(rw_engine, None, "login_failure") if r.entity_id is None]
    assert len(ghost) >= 1 and ghost[-1].detail == {
        "step": "password",
        "reason": "unknown_email",
        "email": "ghost@example.test",
    }
    assert len(firm_events(rw_engine, None, "login_failure")) == len(unknown) + 1


def test_sixth_failure_inside_window_is_locked(
    client: TestClient, seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    su = seed.users["lockme"]
    for _ in range(5):
        r = password_login(client, su, password="wrong-wrong-wrong")
        assert (r.status_code, r.json()) == (401, {"detail": INVALID_CREDENTIALS})
    sixth = password_login(client, su, password="wrong-wrong-wrong")
    assert (sixth.status_code, sixth.json()) == (429, {"detail": LOCKED})
    # The right password is refused too while locked.
    assert password_login(client, su).status_code == 429
    locked = firm_events(rw_engine, su.id, "account_locked")
    assert len(locked) == 1 and locked[0].actor_user_id == su.id
    assert "until" in locked[0].detail
    with owner_engine.connect() as conn:
        assert (
            conn.execute(select(User.locked_until).where(User.id == su.id)).scalar_one() is not None
        )
    # Lock expiry: move it into the past and the account works again.
    with untenanted_session(owner_engine) as s:
        s.execute(
            text("UPDATE \"user\" SET locked_until = now() - interval '1 minute' WHERE id = :id"),
            {"id": su.id},
        )
    assert password_login(client, su).status_code == 200


def test_login_success_audit_and_auto_tenant(
    client: TestClient, seed: Seed, rw_engine: Engine
) -> None:
    su = seed.users["client_pm"]  # exactly one membership → selected at login
    r = password_login(client, su)
    assert r.status_code == 200
    body = r.json()
    assert body["active_tenant_id"] == str(seed.tenant_a)
    assert body["role"] == "client_pm"
    assert body["totp"] == "ok"
    success = firm_events(rw_engine, su.id, "login_success")
    assert success and success[-1].detail["method"] == "password"
    assert success[-1].detail["active_tenant_id"] == str(seed.tenant_a)
    entered = [
        e
        for e in tenant_events(rw_engine, seed.tenant_a, "tenant_enter")
        if e.actor_user_id == su.id
    ]
    assert entered and entered[-1].actor_role.value == "client_pm"
    assert entered[-1].detail == {"via": "auto"}


def test_password_is_required_to_be_twelve_chars_on_change(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    c = login_as("client_viewer")
    r = c.post(
        "/api/auth/password/change",
        json={"current_password": seed.users["client_viewer"].password, "new_password": "short"},
        headers=CSRF,
    )
    assert r.status_code == 422


# --- TOTP gate ------------------------------------------------------------------------------

GATED = [
    ("GET", "/api/session/tenants"),
    ("POST", "/api/session/tenant"),
    ("GET", "/api/_probe/rows"),
    ("GET", "/api/_probe/cap/view_reports"),
    ("GET", "/api/admin/users"),
    ("GET", "/api/firm-audit"),
]


def _gated_statuses(c: TestClient, seed: Seed) -> dict[str, int]:
    out = {}
    for method, path in GATED:
        r = c.request(method, path, json={"tenant_id": str(seed.tenant_a)}, headers=CSRF)
        out[f"{method} {path}"] = r.status_code
    return out


def test_firm_role_without_totp_enrolled_reaches_only_enrolment(
    client: TestClient, seed: Seed
) -> None:
    su = seed.users["firm_nototp"]
    r = password_login(client, su)
    assert (r.status_code, r.json()["totp"]) == (200, "enrol_required")
    assert _gated_statuses(client, seed) == {k: 403 for k in _gated_statuses(client, seed)}
    r = client.get("/api/session/tenants")
    assert r.json() == {"detail": "enrol_required"}
    # Reachable: whoami, enrolment start, logout.
    assert client.get("/api/session/me").status_code == 200
    r = client.post("/api/auth/totp/enrol", headers=CSRF)
    assert r.status_code == 200
    record_secret("totp_secret", r.json()["secret"])
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 204


def test_firm_role_password_only_session_is_gated_the_same(client: TestClient, seed: Seed) -> None:
    su = seed.users["firm_admin"]
    r = password_login(client, su)
    assert (r.status_code, r.json()["totp"]) == (200, "verify_required")
    statuses = _gated_statuses(client, seed)
    assert statuses == {k: 403 for k in statuses}
    assert client.get("/api/session/tenants").json() == {"detail": "verify_required"}
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 204


def test_client_role_without_totp_is_not_gated(client: TestClient, seed: Seed) -> None:
    password_login(client, seed.users["client_viewer"])
    assert client.get("/api/session/tenants").status_code == 200


# --- enrolment ------------------------------------------------------------------------------


def test_totp_enrolment_flow(client: TestClient, seed: Seed, rw_engine: Engine) -> None:
    su = seed.users["enrol_me"]
    password_login(client, su)
    before = record_cookie(client)
    r = client.post("/api/auth/totp/enrol", headers=CSRF)
    assert r.status_code == 200
    secret = r.json()["secret"]
    record_secret("totp_secret", secret)
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert secret in r.json()["otpauth_uri"]
    # A wrong code does not enrol.
    r = client.post("/api/auth/totp/enrol/confirm", json={"code": "000000"}, headers=CSRF)
    assert r.status_code == 400
    r = client.post("/api/auth/totp/enrol/confirm", json={"code": totp_code(secret)}, headers=CSRF)
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    for code in codes:
        record_secret("recovery_code", code)
    assert r.json()["totp"] == "ok"
    assert record_cookie(client) != before  # rotated at verification
    # Now a full session: the gate opens.
    assert client.get("/api/session/tenants").status_code == 200
    assert client.get("/api/_probe/rows").status_code == 200
    # Recovery codes are stored hashed; the secret is stored encrypted.
    with rw_engine.connect() as conn:
        hashes, enc = conn.execute(
            select(User.recovery_code_hashes, User.totp_secret_enc).where(User.id == su.id)
        ).one()
    assert len(hashes) == 10 and not set(hashes) & set(codes)
    assert secret.encode() not in enc
    enrolled = firm_events(rw_engine, su.id, "totp_enrolled")
    assert enrolled and enrolled[-1].detail == {"recovery_codes_issued": 10}
    # Enrolling again is refused until a firm_admin resets.
    assert client.post("/api/auth/totp/enrol", headers=CSRF).status_code == 409


# --- replay and recovery ----------------------------------------------------------------------


def test_totp_code_cannot_be_used_twice(seed: Seed, owner_engine: Engine) -> None:
    su = seed.users["replay_me"]
    reset_totp_counter(owner_engine, su.id)
    code = totp_code(su.totp_secret)
    with make_client() as first, make_client() as second:
        password_login(first, su)
        password_login(second, su)
        r1 = first.post("/api/auth/totp/verify", json={"code": code}, headers=CSRF)
        r2 = second.post("/api/auth/totp/verify", json={"code": code}, headers=CSRF)
    assert r1.status_code == 200
    assert (r2.status_code, r2.json()) == (401, {"detail": "invalid code"})


def test_recovery_code_works_once_and_is_audited(seed: Seed, rw_engine: Engine) -> None:
    su = seed.users["recover_me"]
    code = su.recovery_codes[0]
    with make_client() as first, make_client() as second, make_client() as third:
        password_login(first, su)
        r = first.post("/api/auth/totp/recover", json={"code": code.lower()}, headers=CSRF)
        assert (r.status_code, r.json()["totp"]) == (200, "ok")
        assert first.get("/api/session/tenants").status_code == 200
        password_login(second, su)
        r = second.post("/api/auth/totp/recover", json={"code": code}, headers=CSRF)
        assert r.status_code == 401
        password_login(third, su)
        r = third.post("/api/auth/totp/recover", json={"code": su.recovery_codes[1]}, headers=CSRF)
        assert r.status_code == 200
    used = firm_events(rw_engine, su.id, "recovery_code_used")
    assert [u.detail["remaining"] for u in used] == [9, 8]
    success = firm_events(rw_engine, su.id, "login_success")
    assert success[-1].detail["method"] == "password+recovery"
