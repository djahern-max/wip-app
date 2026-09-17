"""firm_admin administration: users, memberships, tenants (F02; F02.1)."""

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.tenancy.models import Role, Tenant
from tests.conftest import CSRF, Seed, activation_token_from, make_client, password_login
from tests.test_auth import firm_events, tenant_events


def test_create_user_issues_an_activation_link_and_refuses_duplicates(
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
    assert body["activation_url"].startswith("https://app.example.test/activate#token=")
    activation_token_from(body["activation_url"])
    uid = uuid.UUID(body["id"])
    created = firm_events(rw_engine, uid, "user_created")
    assert created[-1].actor_user_id == seed.users["firm_admin"].id
    assert created[-1].actor_role == Role.firm_admin
    assert created[-1].firm_id == seed.firm_id
    assert firm_events(rw_engine, uid, "activation_link_issued")
    r = admin.post("/api/admin/users", json={"email": email, "display_name": "Dup"}, headers=CSRF)
    assert r.status_code == 409
    # A password in the body is not accepted any more (D-16): extra field ignored,
    # the account still has no password.
    r = admin.post(
        "/api/admin/users",
        json={
            "email": f"p-{uuid.uuid4().hex[:6]}@example.test",
            "display_name": "P",
            "password": "a-password-of-twelve",
        },
        headers=CSRF,
    )
    assert r.status_code == 201 and "password" not in r.json()


def test_membership_lifecycle_in_a_tenant_without_members(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    """The firm_admin holds no entry row in tenant_new; firm authority is enough."""
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
    by_email = {r["email"]: r for r in rows}
    assert seed.users["client_pm"].email in by_email
    assert by_email[seed.users["firm_staff"].email]["role"] == "firm_staff"  # effective role
    assert by_email[seed.users["orphan"].email]["role"] is None  # orphan entry row
    assert seed.users["client_admin_b"].email not in by_email  # tenant B only
    assert all(
        set(r) == {"user_id", "email", "display_name", "role", "totp_enrolled"} for r in rows
    )
    assert not any("password" in k or "secret" in k for r in rows for k in r)
    admin.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_b)}, headers=CSRF)
    emails_b = {r["email"] for r in admin.get("/api/admin/users").json()}
    assert seed.users["client_admin_b"].email in emails_b
    assert seed.users["client_pm"].email not in emails_b


def test_create_tenant_takes_the_firm_from_the_principal(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    admin = login_as("firm_admin")
    slug = f"new-co-{uuid.uuid4().hex[:6]}"
    r = admin.post(
        "/api/admin/tenants",
        json={"name": "  New Co  ", "slug": slug.upper(), "firm_id": str(seed.other_firm_id)},
        headers=CSRF,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["firm_id"] == str(seed.firm_id)  # the body's firm_id is ignored
    assert (body["name"], body["slug"]) == ("New Co", slug)
    with rw_engine.connect() as conn:
        t = conn.execute(select(Tenant).where(Tenant.slug == slug)).one()
    assert t.firm_id == seed.firm_id
    created = [
        e
        for e in firm_events(rw_engine, None, "tenant_created")
        if e.entity_id == body["tenant_id"]
    ]
    assert created and created[-1].firm_id == seed.firm_id
    assert created[-1].detail == {"name": "New Co", "slug": slug}
    assert created[-1].actor_user_id == seed.users["firm_admin"].id
    # Bare row only: no entry row for the admin yet.
    assert (
        admin.post(
            "/api/session/tenant", json={"tenant_id": body["tenant_id"]}, headers=CSRF
        ).status_code
        == 403
    )
    r = admin.post(
        f"/api/admin/tenants/{body['tenant_id']}/memberships",
        json={"user_id": str(seed.users["firm_admin"].id)},
        headers=CSRF,
    )
    assert r.status_code == 201
    assert (
        admin.post(
            "/api/session/tenant", json={"tenant_id": body["tenant_id"]}, headers=CSRF
        ).json()["role"]
        == "firm_admin"
    )
    # Duplicate slug and a bad slug.
    assert (
        admin.post("/api/admin/tenants", json={"name": "X", "slug": slug}, headers=CSRF).status_code
        == 409
    )
    assert (
        admin.post(
            "/api/admin/tenants", json={"name": "X", "slug": "Bad Slug!"}, headers=CSRF
        ).status_code
        == 422
    )
