"""Firm authority is a row in ``firm_membership`` (D-15; F02.1 acceptance): entry
rows, invariants, last-admin guard with the firm row locked, the orphan rule, the
two-phase removal (owner answer A) with a simulated failure and a retry, and the
bootstrap on an empty database."""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import IntegrityError

from app.auth import admin
from app.core.audit import RequestMeta
from app.core.crypto import get_keyring
from app.core.db import tenant_session, untenanted_session
from app.core.security import hash_password, new_totp_secret
from app.tenancy.models import FirmMembership, Membership, Role, Tenant, User
from scripts import create_user as cli
from tests.conftest import (
    CSRF,
    Seed,
    SeedUser,
    activation_token_from,
    alembic_config,
    full_login,
    make_client,
    password_login,
    reset_totp_counter,
    totp_code,
)
from tests.leaks import record_secret
from tests.test_auth import firm_events, tenant_events


def make_firm_user(
    owner_engine: Engine,
    *,
    firm_id: uuid.UUID,
    role: Role,
    tenants: tuple[uuid.UUID, ...] = (),
    totp: bool = True,
) -> SeedUser:
    """A firm user with password (+ TOTP) seeded directly, for tests that must not
    disturb the shared seed users."""
    uid = uuid.uuid4()
    key = f"fm-{uid.hex[:8]}"
    pw = f"pw-{key}-correct-horse-battery"
    record_secret("password", pw)
    secret = None
    with untenanted_session(owner_engine) as s:
        u = User(
            id=uid,
            email=f"{key}@example.test",
            display_name=key,
            password_hash=hash_password(pw),
            password_changed_at=datetime.now(UTC),
        )
        if totp:
            secret = new_totp_secret()
            record_secret("totp_secret", secret)
            u.totp_key_id, u.totp_secret_enc = get_keyring().encrypt(secret.encode(), aad=uid.bytes)
            u.totp_enrolled_at = datetime.now(UTC)
        s.add(u)
        s.flush()
        s.add(FirmMembership(firm_id=firm_id, user_id=uid, role=role))
    for tid in tenants:
        with tenant_session(owner_engine, tid) as s:
            s.add(Membership(tenant_id=tid, user_id=uid, role=None))
    return SeedUser(
        id=uid, email=f"{key}@example.test", password=pw, totp_secret=secret, recovery_codes=()
    )


def login_firm_user(c: TestClient, su: SeedUser, owner_engine: Engine) -> dict:
    assert password_login(c, su).status_code == 200
    reset_totp_counter(owner_engine, su.id)
    r = c.post("/api/auth/totp/verify", json={"code": totp_code(su.totp_secret)}, headers=CSRF)
    assert r.status_code == 200, r.text
    return c.get("/api/session/me").json()


# --- roles come only from firm_membership ---------------------------------------------------


def test_firm_staff_is_firm_staff_in_both_tenants_from_entry_rows(
    login_as: Callable[..., TestClient], seed: Seed, owner_engine: Engine
) -> None:
    su = seed.users["firm_staff"]
    with owner_engine.connect() as conn:
        roles = conn.execute(
            select(Membership.role).where(Membership.user_id == su.id)
        ).scalars()  # owner without tenant context reads nothing: FORCE RLS
        assert list(roles) == []
    for tid in (seed.tenant_a, seed.tenant_b):
        with tenant_session(owner_engine, tid) as s:
            assert (
                s.execute(select(Membership.role).where(Membership.user_id == su.id)).scalar_one()
                is None
            )  # entry row
    c = login_as("firm_staff", tenant=None)
    for tid in (seed.tenant_b, seed.tenant_a):
        r = c.post("/api/session/tenant", json={"tenant_id": str(tid)}, headers=CSRF)
        assert r.json() == {"active_tenant_id": str(tid), "role": "firm_staff"}
        assert c.get("/api/session/me").json()["role"] == "firm_staff"
    assert {t["role"] for t in c.get("/api/session/tenants").json()} == {"firm_staff"}


def test_nothing_in_membership_can_raise_a_firm_role(
    login_as: Callable[..., TestClient], seed: Seed, owner_engine: Engine
) -> None:
    su = seed.users["firm_staff"]
    # The database refuses a firm role in membership.role, even for the owner.
    with pytest.raises(IntegrityError, match="ck_membership_role_client"):
        with tenant_session(owner_engine, seed.tenant_a) as s:
            s.execute(
                text("UPDATE membership SET role = 'firm_admin' WHERE user_id = :u"), {"u": su.id}
            )
    # And the API refuses it for a firm user's row (409) and for anyone (422).
    admin_c = login_as("firm_admin")
    r = admin_c.put(
        f"/api/admin/tenants/{seed.tenant_a}/memberships/{su.id}",
        json={"role": "client_admin"},
        headers=CSRF,
    )
    assert r.status_code == 409
    r = admin_c.put(
        f"/api/admin/tenants/{seed.tenant_a}/memberships/{seed.users['client_pm'].id}",
        json={"role": "firm_admin"},
        headers=CSRF,
    )
    assert r.status_code == 422


def test_firm_user_needs_an_entry_row_then_switch_is_audited(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    su = seed.users["firm_staff"]
    staff = login_as("firm_staff", tenant=None)
    r = staff.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_new)}, headers=CSRF)
    assert r.status_code == 403
    admin_c = login_as("firm_admin")
    base = f"/api/admin/tenants/{seed.tenant_new}/memberships"
    r = admin_c.post(base, json={"user_id": str(su.id)}, headers=CSRF)  # role omitted: entry
    assert (r.status_code, r.json()["role"]) == (201, None), r.text
    r = staff.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_new)}, headers=CSRF)
    assert r.json() == {"active_tenant_id": str(seed.tenant_new), "role": "firm_staff"}
    events = [
        (e.action, e.detail)
        for e in tenant_events(rw_engine, seed.tenant_new, "membership_created")
        + tenant_events(rw_engine, seed.tenant_new, "tenant_enter")
        if e.detail.get("user_id") == str(su.id) or e.actor_user_id == su.id
    ]
    assert [a for a, _ in events] == ["membership_created", "tenant_enter"]
    assert events[0][1]["role"] == "entry"
    assert admin_c.delete(f"{base}/{su.id}", headers=CSRF).status_code == 204


def test_client_role_for_firm_user_and_firm_membership_for_client_user_are_rejected(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    admin_c = login_as("firm_admin")
    r = admin_c.post(
        f"/api/admin/tenants/{seed.tenant_new}/memberships",
        json={"user_id": str(seed.users["firm_staff"].id), "role": "client_pm"},
        headers=CSRF,
    )
    assert (r.status_code, r.json()) == (
        409,
        {"detail": "user holds a firm role; an entry row carries no role"},
    )
    r = admin_c.post(
        "/api/admin/firm-memberships",
        json={"user_id": str(seed.users["client_pm"].id), "role": "firm_staff"},
        headers=CSRF,
    )
    assert (r.status_code, r.json()) == (
        409,
        {"detail": "user holds a client role in a tenant of this firm"},
    )
    # A firm role in membership, or a client role in firm_membership: 422.
    r = admin_c.post(
        f"/api/admin/tenants/{seed.tenant_new}/memberships",
        json={"user_id": str(seed.users["scratch"].id), "role": "firm_staff"},
        headers=CSRF,
    )
    assert r.status_code == 422
    r = admin_c.post(
        "/api/admin/firm-memberships",
        json={"user_id": str(seed.users["scratch"].id), "role": "client_pm"},
        headers=CSRF,
    )
    assert r.status_code == 422
    # An entry row for a client user (no firm_membership): 422.
    r = admin_c.post(
        f"/api/admin/tenants/{seed.tenant_new}/memberships",
        json={"user_id": str(seed.users["scratch"].id)},
        headers=CSRF,
    )
    assert r.status_code == 422


# --- last firm_admin --------------------------------------------------------------------------


def test_last_firm_admin_cannot_be_removed_or_demoted_and_writes_are_atomic(
    seed: Seed, owner_engine: Engine, rw_engine: Engine, clients: Callable[..., TestClient]
) -> None:
    with untenanted_session(owner_engine) as s:
        from app.tenancy.models import Firm

        firm = Firm(name="Solo Practice")
        s.add(firm)
        s.flush()
        firm_id = firm.id
    boss = make_firm_user(owner_engine, firm_id=firm_id, role=Role.firm_admin)
    c = clients()
    login_firm_user(c, boss, owner_engine)
    r = c.put(f"/api/admin/firm-memberships/{boss.id}", json={"role": "firm_staff"}, headers=CSRF)
    assert (r.status_code, r.json()) == (409, {"detail": "the last firm_admin cannot be demoted"})
    r = c.delete(f"/api/admin/firm-memberships/{boss.id}", headers=CSRF)
    assert r.status_code == 409  # own row
    # Another admin removing the last admin: refused too.
    with untenanted_session(owner_engine) as s:
        assert (
            s.execute(select(FirmMembership).where(FirmMembership.user_id == boss.id))
            .scalar_one()
            .role
            is Role.firm_admin
        )
    # Create staff → audit row; role change → audit row; each in the same transaction.
    r = c.post(
        "/api/admin/users",
        json={"email": f"s-{uuid.uuid4().hex[:6]}@example.test", "display_name": "S"},
        headers=CSRF,
    )
    sid = r.json()["id"]
    activation_token_from(r.json()["activation_url"])
    r = c.post(
        "/api/admin/firm-memberships", json={"user_id": sid, "role": "firm_staff"}, headers=CSRF
    )
    assert r.status_code == 201, r.text
    fm_id = r.json()["firm_membership_id"]
    r = c.put(f"/api/admin/firm-memberships/{sid}", json={"role": "firm_admin"}, headers=CSRF)
    assert r.status_code == 200
    r = c.delete(f"/api/admin/firm-memberships/{sid}", headers=CSRF)
    assert r.status_code == 204
    actions = [
        e.action
        for e in firm_events(rw_engine, None, "firm_membership_created")
        + firm_events(rw_engine, None, "firm_membership_role_changed")
        + firm_events(rw_engine, None, "firm_membership_removed")
        if e.entity_id == fm_id
    ]
    assert actions == [
        "firm_membership_created",
        "firm_membership_role_changed",
        "firm_membership_removed",
    ]
    # Atomicity: a failure after the write rolls back the row and its audit row.
    before = len(firm_events(rw_engine, None, "firm_membership_created"))
    with pytest.raises(RuntimeError, match="simulated"):
        with untenanted_session(owner_engine) as s:
            admin.create_firm_membership(
                s,
                None,
                firm_id=firm_id,
                user=s.get(User, uuid.UUID(sid)),
                role=Role.firm_staff,
                meta=RequestMeta(),
            )
            raise RuntimeError("simulated failure after the write")
    with untenanted_session(rw_engine) as s:
        assert (
            s.execute(
                select(FirmMembership).where(FirmMembership.user_id == uuid.UUID(sid))
            ).first()
            is None
        )
    assert len(firm_events(rw_engine, None, "firm_membership_created")) == before


def test_concurrent_demotions_leave_exactly_one_firm_admin(
    seed: Seed, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner answer to call 4: the firm row is locked FOR UPDATE before counting, so
    two simultaneous demotions of the only two admins serialise: one succeeds, the
    other counts one remaining admin and is refused."""
    with untenanted_session(owner_engine) as s:
        from app.tenancy.models import Firm

        firm = Firm(name="Two Admins LLP")
        s.add(firm)
        s.flush()
        firm_id = firm.id
    p = make_firm_user(owner_engine, firm_id=firm_id, role=Role.firm_admin)
    q = make_firm_user(owner_engine, firm_id=firm_id, role=Role.firm_admin)

    real_count = admin._count_firm_admins

    def slow_count(db, fid):
        n = real_count(db, fid)
        time.sleep(0.4)  # widen the window between count and update
        return n

    monkeypatch.setattr(admin, "_count_firm_admins", slow_count)

    cp, cq = make_client(), make_client()
    cp.__enter__()
    cq.__enter__()
    try:
        login_firm_user(cp, p, owner_engine)
        login_firm_user(cq, q, owner_engine)
        barrier = threading.Barrier(2)
        results: dict[str, int] = {}

        def demote(c: TestClient, target: SeedUser, label: str) -> None:
            barrier.wait()
            r = c.put(
                f"/api/admin/firm-memberships/{target.id}",
                json={"role": "firm_staff"},
                headers=CSRF,
            )
            results[label] = r.status_code

        t1 = threading.Thread(target=demote, args=(cp, q, "p_demotes_q"))
        t2 = threading.Thread(target=demote, args=(cq, p, "q_demotes_p"))
        t1.start()
        t2.start()
        t1.join(20)
        t2.join(20)
    finally:
        cp.__exit__(None, None, None)
        cq.__exit__(None, None, None)
    assert sorted(results.values()) == [200, 409], results
    with untenanted_session(owner_engine) as s:
        admins = (
            s.execute(
                select(FirmMembership).where(
                    FirmMembership.firm_id == firm_id, FirmMembership.role == Role.firm_admin
                )
            )
            .scalars()
            .all()
        )
    assert len(admins) == 1


# --- orphan rule ------------------------------------------------------------------------------


def test_orphan_entry_row_grants_no_access(
    client: TestClient, seed: Seed, owner_engine: Engine
) -> None:
    su = seed.users["orphan"]
    with tenant_session(owner_engine, seed.tenant_a) as s:
        assert (
            s.execute(select(Membership.role).where(Membership.user_id == su.id)).scalar_one()
            is None
        )
    with untenanted_session(owner_engine) as s:
        assert (
            s.execute(select(FirmMembership).where(FirmMembership.user_id == su.id)).first() is None
        )
    r = password_login(client, su)
    assert r.status_code == 200  # no firm_membership: a plain password login
    body = r.json()
    assert body["active_tenant_id"] is None and body["role"] is None
    assert client.get("/api/session/tenants").json() == []
    r = client.post("/api/session/tenant", json={"tenant_id": str(seed.tenant_a)}, headers=CSRF)
    assert r.status_code == 403
    assert client.get("/api/_probe/rows").status_code == 400


# --- removal: two phases, harmless failure, retry (owner answer A) -----------------------------


def test_remove_firm_membership_commits_first_then_cleans_entry_rows_and_is_retryable(
    login_as: Callable[..., TestClient],
    seed: Seed,
    owner_engine: Engine,
    rw_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    clients: Callable[..., TestClient],
) -> None:
    leaver = make_firm_user(
        owner_engine,
        firm_id=seed.firm_id,
        role=Role.firm_staff,
        tenants=(seed.tenant_a, seed.tenant_b),
    )
    victim = clients()
    login_firm_user(victim, leaver, owner_engine)
    assert victim.get("/api/session/me").status_code == 200

    real = admin._remove_entry_row
    calls: list[uuid.UUID] = []

    def flaky(engine, *, tenant_id, **kw):
        calls.append(tenant_id)
        if len(calls) == 2:
            raise RuntimeError("simulated failure after the first tenant")
        return real(engine, tenant_id=tenant_id, **kw)

    monkeypatch.setattr(admin, "_remove_entry_row", flaky)
    admin_c = login_as("firm_admin")
    with pytest.raises(RuntimeError, match="simulated"):
        admin_c.delete(f"/api/admin/firm-memberships/{leaver.id}", headers=CSRF)

    # Phase 1 committed: row gone, audited, sessions ended.
    with untenanted_session(rw_engine) as s:
        assert (
            s.execute(select(FirmMembership).where(FirmMembership.user_id == leaver.id)).first()
            is None
        )
    removed = [
        e
        for e in firm_events(rw_engine, None, "firm_membership_removed")
        if e.detail.get("user_id") == str(leaver.id)
    ]
    assert len(removed) == 1 and removed[0].detail["entry_rows_to_remove"] == 2
    assert victim.get("/api/session/me").status_code == 401
    # One entry row cleaned, one orphan left: the user has no access anywhere.
    remaining = []
    for tid in (seed.tenant_a, seed.tenant_b):
        with tenant_session(owner_engine, tid) as s:
            if s.execute(select(Membership).where(Membership.user_id == leaver.id)).first():
                remaining.append(tid)
    assert len(remaining) == 1
    again = clients()
    me = login_firm_user(again, leaver, owner_engine)  # password + TOTP still verify
    assert me["active_tenant_id"] is None and me["firm_role"] is None
    assert again.get("/api/session/tenants").json() == []
    for tid in (seed.tenant_a, seed.tenant_b):
        assert (
            again.post(
                "/api/session/tenant", json={"tenant_id": str(tid)}, headers=CSRF
            ).status_code
            == 403
        )

    # Retry completes the cleanup.
    monkeypatch.setattr(admin, "_remove_entry_row", real)
    assert (
        admin_c.delete(f"/api/admin/firm-memberships/{leaver.id}", headers=CSRF).status_code == 204
    )
    for tid in (seed.tenant_a, seed.tenant_b):
        with tenant_session(owner_engine, tid) as s:
            assert (
                s.execute(select(Membership).where(Membership.user_id == leaver.id)).first() is None
            )
        gone = [
            e
            for e in tenant_events(rw_engine, tid, "membership_removed")
            if e.detail.get("user_id") == str(leaver.id)
        ]
        assert len(gone) == 1 and gone[0].detail["role"] == "entry"
    # Nothing left to do: 404.
    assert (
        admin_c.delete(f"/api/admin/firm-memberships/{leaver.id}", headers=CSRF).status_code == 404
    )


def test_removing_a_firm_membership_ends_sessions_and_change_does_too(
    login_as: Callable[..., TestClient],
    seed: Seed,
    owner_engine: Engine,
    clients: Callable[..., TestClient],
) -> None:
    su = make_firm_user(owner_engine, firm_id=seed.firm_id, role=Role.firm_staff)
    c = clients()
    login_firm_user(c, su, owner_engine)
    admin_c = login_as("firm_admin")
    r = admin_c.put(
        f"/api/admin/firm-memberships/{su.id}", json={"role": "firm_admin"}, headers=CSRF
    )
    assert r.status_code == 200
    assert c.get("/api/session/me").status_code == 401
    login_firm_user(c, su, owner_engine)
    assert admin_c.delete(f"/api/admin/firm-memberships/{su.id}", headers=CSRF).status_code == 204
    assert c.get("/api/session/me").status_code == 401


# --- bootstrap on an empty database ------------------------------------------------------------


def test_bootstrap_creates_firm_and_admin_without_a_tenant(
    scratch_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty database → firm + firm_admin (no tenant) → activate → TOTP → create a
    tenant → own entry row → enter it. The CLI prints the link once, to stdout."""
    command.upgrade(alembic_config(scratch_db_url), "head")
    engine = create_engine(scratch_db_url)
    try:
        args = cli.build_parser().parse_args(
            [
                "bootstrap",
                "--firm-name",
                "Bootstrap CPA",
                "--email",
                "Boss@Firm.test",
                "--display-name",
                "Boss",
            ]
        )
        cli.cmd_bootstrap(args, engine)
        out = capsys.readouterr().out
        url = next(line for line in out.splitlines() if line.startswith("https://"))
        token = activation_token_from(url)
        assert out.count(token) == 1
        with untenanted_session(engine) as s:
            assert s.execute(select(Tenant)).first() is None
            fm = s.execute(select(FirmMembership)).scalar_one()
            assert fm.role is Role.firm_admin
            user = s.get(User, fm.user_id)
            assert user.email == "boss@firm.test"
            assert (
                s.execute(select(User.password_hash).where(User.id == user.id)).scalar_one() is None
            )
            user_id, bootstrap_firm_id = user.id, fm.firm_id
        with make_client() as c:
            c.app.state.engine.dispose()
            c.app.state.engine = engine  # point the app at the scratch database
            pw = "bootstrap-password-1234"
            record_secret("password", pw)
            r = c.post(
                "/api/auth/activate", json={"token": token, "new_password": pw}, headers=CSRF
            )
            assert (r.status_code, r.json()) == (200, {"next": "totp_enrol"}), r.text
            assert c.get("/api/session/me").json()["totp"] == "enrol_required"
            secret = c.post("/api/auth/totp/enrol", headers=CSRF).json()["secret"]
            record_secret("totp_secret", secret)
            r = c.post(
                "/api/auth/totp/enrol/confirm", json={"code": totp_code(secret)}, headers=CSRF
            )
            assert r.status_code == 200, r.text
            for code in r.json()["recovery_codes"]:
                record_secret("recovery_code", code)
            r = c.post(
                "/api/admin/tenants",
                json={"name": "Rye Beach Landscaping", "slug": "rye-beach"},
                headers=CSRF,
            )
            assert r.status_code == 201, r.text
            tid = r.json()["tenant_id"]
            r = c.post(
                f"/api/admin/tenants/{tid}/memberships",
                json={"user_id": str(user_id)},
                headers=CSRF,
            )
            assert r.status_code == 201, r.text
            r = c.post("/api/session/tenant", json={"tenant_id": tid}, headers=CSRF)
            assert r.json() == {"active_tenant_id": tid, "role": "firm_admin"}
            rows = c.get("/api/firm-audit").json()
            assert {
                "user_created",
                "activation_link_issued",
                "firm_membership_created",
                "tenant_created",
            } <= {r["action"] for r in rows}
            # firm_id came from the principal: the tenant is in the bootstrap firm.
            assert {r["firm_id"] for r in rows if r["action"] == "tenant_created"} == {
                str(bootstrap_firm_id)
            }
    finally:
        engine.dispose()


def test_full_login_helper_still_works_for_all_seed_roles(
    seed: Seed, owner_engine: Engine, clients: Callable[..., TestClient]
) -> None:
    for key in ("firm_admin", "firm_staff", "client_admin", "client_pm", "client_viewer"):
        me = full_login(clients(), seed, key, owner_engine, seed.tenant_a)
        assert me["role"] == key
