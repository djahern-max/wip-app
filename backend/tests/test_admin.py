"""firm_admin administration: users, memberships, resets (F02)."""

import uuid
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.core.db import tenant_session
from app.tenancy.models import Membership, Role, User
from tests.conftest import CSRF, Seed, SeedUser, make_client, password_login
from tests.leaks import record_secret
from tests.test_auth import firm_events, tenant_events


def _token_from(url: str) -> str:
    token = parse_qs(urlparse(url).query)["token"][0]
    record_secret("reset_token", token)
    return token


def test_create_user_without_password_issues_reset_link(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    admin = login_as("firm_admin")
    email = f"new-{uuid.uuid4().hex[:8]}@example.test"
    r = admin.post(
        "/api/admin/users", json={"email": email.upper(), "display_name": "New"}, headers=CSRF
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == email  # normalised
    url = body["password_reset_url"]
    assert url.startswith("https://app.example.test/reset-password?token=")
    uid = uuid.UUID(body["id"])
    created = firm_events(rw_engine, uid, "user_created")
    assert created[-1].actor_user_id == seed.users["firm_admin"].id
    assert created[-1].actor_role == Role.firm_admin
    assert created[-1].firm_id == seed.firm_id
    assert created[-1].detail == {"email": email, "via": "api", "password_set": False}
    assert firm_events(rw_engine, uid, "password_reset_issued")
    # Complete the reset (unauthenticated), then log in with the new password.
    new_password = "brand-new-password-1"
    record_secret("password", new_password)
    with make_client() as anon:
        r = anon.post(
            "/api/auth/password/reset",
            json={"token": _token_from(url), "new_password": new_password},
            headers=CSRF,
        )
        assert r.status_code == 204, r.text
        # The link is one-time.
        r = anon.post(
            "/api/auth/password/reset",
            json={"token": _token_from(url), "new_password": new_password},
            headers=CSRF,
        )
        assert r.status_code == 400
        su = SeedUser(
            id=uid, email=email, password=new_password, totp_secret=None, recovery_codes=()
        )
        assert password_login(anon, su).status_code == 200
    changed = firm_events(rw_engine, uid, "password_changed")
    assert changed[-1].detail == {"via": "reset_link"}
    # Duplicate e-mail is refused.
    r = admin.post("/api/admin/users", json={"email": email, "display_name": "Dup"}, headers=CSRF)
    assert r.status_code == 409


def test_create_user_with_password(login_as: Callable[..., TestClient], seed: Seed) -> None:
    admin = login_as("firm_admin")
    email = f"pw-{uuid.uuid4().hex[:8]}@example.test"
    pw = "a-password-of-twelve"
    record_secret("password", pw)
    r = admin.post(
        "/api/admin/users",
        json={"email": email, "display_name": "P", "password": pw},
        headers=CSRF,
    )
    assert r.status_code == 201 and "password_reset_url" not in r.json()
    r = admin.post(
        "/api/admin/users",
        json={"email": "x@example.test", "display_name": "P", "password": "short"},
        headers=CSRF,
    )
    assert r.status_code == 422


def test_password_reset_for_existing_user_ends_their_sessions(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    victim = login_as("client_viewer")
    assert victim.get("/api/session/me").status_code == 200
    admin = login_as("firm_admin")
    r = admin.post(
        f"/api/admin/users/{seed.users['client_viewer'].id}/password-reset", headers=CSRF
    )
    assert r.status_code == 200
    _token_from(r.json()["password_reset_url"])
    assert victim.get("/api/session/me").status_code == 401
    issued = firm_events(rw_engine, seed.users["client_viewer"].id, "password_reset_issued")
    assert issued[-1].actor_user_id == seed.users["firm_admin"].id
    # The old password still works until the link is used (F02: no e-mail, admin hands it over).
    with make_client() as c:
        assert password_login(c, seed.users["client_viewer"]).status_code == 200


def test_self_password_change(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    su = seed.users["client_admin"]
    c = login_as("client_admin")
    r = c.post(
        "/api/auth/password/change",
        json={"current_password": "not-it-not-it-not-it", "new_password": "something-longer-1"},
        headers=CSRF,
    )
    assert r.status_code == 401
    new_password = "changed-by-myself-1"
    record_secret("password", new_password)
    r = c.post(
        "/api/auth/password/change",
        json={"current_password": su.password, "new_password": new_password},
        headers=CSRF,
    )
    assert r.status_code == 204
    assert c.get("/api/session/me").status_code == 200  # own session kept
    with make_client() as fresh:
        assert password_login(fresh, su).status_code == 401
        assert password_login(fresh, su, password=new_password).status_code == 200
        # restore the seed password for other tests
        r = fresh.post(
            "/api/auth/password/change",
            json={"current_password": new_password, "new_password": su.password},
            headers=CSRF,
        )
        assert r.status_code == 204
    changed = firm_events(rw_engine, su.id, "password_changed")
    assert changed[-1].detail == {"via": "self"}


def test_totp_reset_forces_re_enrolment(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    su = seed.users["scratch"]
    # Give scratch a firm role in tenant B and an enrolment, then reset it.
    with tenant_session(owner_engine, seed.tenant_b) as s:
        s.execute(select(Membership))  # RLS context
        s.query(Membership).filter(Membership.user_id == su.id).delete()
        s.add(Membership(tenant_id=seed.tenant_b, user_id=su.id, role=Role.firm_staff))
    victim = make_client()
    with victim:
        password_login(victim, su)
        r = victim.post("/api/auth/totp/enrol", headers=CSRF)
        record_secret("totp_secret", r.json()["secret"])
        admin = login_as("firm_admin")
        r = admin.post(f"/api/admin/users/{su.id}/totp-reset", headers=CSRF)
        assert r.status_code == 204
        assert victim.get("/api/session/me").status_code == 401  # sessions ended
    with rw_engine.connect() as conn:
        enc, enrolled = conn.execute(
            select(User.totp_secret_enc, User.totp_enrolled_at).where(User.id == su.id)
        ).one()
    assert enc is None and enrolled is None
    reset = firm_events(rw_engine, su.id, "totp_reset")
    assert reset[-1].actor_role == Role.firm_admin and reset[-1].detail == {"via": "admin"}
    with tenant_session(owner_engine, seed.tenant_b) as s:
        s.query(Membership).filter(Membership.user_id == su.id).delete()


def test_membership_lifecycle_in_a_tenant_without_members(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    """The firm_admin holds no membership in tenant_new; firm authority is enough."""
    admin = login_as("firm_admin")
    scratch = seed.users["scratch"]
    base = f"/api/admin/tenants/{seed.tenant_new}/memberships"
    r = admin.post(base, json={"user_id": str(scratch.id), "role": "client_pm"}, headers=CSRF)
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "client_pm"
    r = admin.post(base, json={"user_id": str(scratch.id), "role": "client_pm"}, headers=CSRF)
    assert r.status_code == 409
    r = admin.put(f"{base}/{scratch.id}", json={"role": "client_admin"}, headers=CSRF)
    assert (r.status_code, r.json()["role"]) == (200, "client_admin")
    # The user can now enter the new tenant.
    with make_client() as c:
        password_login(c, scratch)
        assert c.get("/api/session/me").json()["active_tenant_id"] == str(seed.tenant_new)
    r = admin.delete(f"{base}/{scratch.id}", headers=CSRF)
    assert r.status_code == 204
    r = admin.delete(f"{base}/{scratch.id}", headers=CSRF)
    assert r.status_code == 404
    actions = [
        (e.action, e.detail)
        for e in tenant_events(rw_engine, seed.tenant_new, "membership_created")
        + tenant_events(rw_engine, seed.tenant_new, "membership_role_changed")
        + tenant_events(rw_engine, seed.tenant_new, "membership_removed")
        if e.detail.get("user_id") == str(scratch.id)
    ]
    assert [a for a, _ in actions] == [
        "membership_created",
        "membership_role_changed",
        "membership_removed",
    ]
    assert actions[1][1] == {
        "user_id": str(scratch.id),
        "old_role": "client_pm",
        "new_role": "client_admin",
    }
    # The admin's own active tenant is untouched by acting on another tenant.
    assert admin.get("/api/session/me").json()["active_tenant_id"] == str(seed.tenant_a)
    assert admin.get("/api/_probe/rows").json() == ["a-row"]


def test_tenant_of_another_firm_is_not_found(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    admin = login_as("firm_admin")
    scratch = seed.users["scratch"]
    r = admin.post(
        f"/api/admin/tenants/{seed.tenant_c}/memberships",
        json={"user_id": str(scratch.id), "role": "client_pm"},
        headers=CSRF,
    )
    assert r.status_code == 404
    r = admin.post(
        f"/api/admin/tenants/{uuid.uuid4()}/memberships",
        json={"user_id": str(scratch.id), "role": "client_pm"},
        headers=CSRF,
    )
    assert r.status_code == 404
    r = admin.post(
        f"/api/admin/tenants/{seed.tenant_a}/memberships",
        json={"user_id": str(uuid.uuid4()), "role": "client_pm"},
        headers=CSRF,
    )
    assert r.status_code == 404


def test_list_users_of_active_tenant(login_as: Callable[..., TestClient], seed: Seed) -> None:
    admin = login_as("firm_admin")
    rows = admin.get("/api/admin/users").json()
    emails = {r["email"] for r in rows}
    assert seed.users["client_pm"].email in emails
    assert seed.users["client_admin_b"].email not in emails  # tenant B only
    assert all(
        set(r) == {"user_id", "email", "display_name", "role", "totp_enrolled"} for r in rows
    )
    assert not any("password" in k or "secret" in k for r in rows for k in r)
    admin.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_b)}, headers=CSRF)
    emails_b = {r["email"] for r in admin.get("/api/admin/users").json()}
    assert seed.users["client_admin_b"].email in emails_b
    assert seed.users["client_pm"].email not in emails_b
