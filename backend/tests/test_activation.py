"""Accounts activate only through an admin-issued one-time link (D-16; F02.1):
no password at creation, single use, expiry, the firm-user flow that enrols TOTP
in the same session, the 15-minute enrolment-only session, link supersession,
admin TOTP reset, and session invalidation on every credential change."""

import json
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from app.api.auth import INVALID_CREDENTIALS, INVALID_LINK
from app.core.crypto import get_keyring
from app.core.security import sha256_hex
from app.tenancy.models import User, UserSession
from scripts import create_user as cli
from tests.conftest import (
    COOKIE,
    CSRF,
    Seed,
    SeedUser,
    activation_token_from,
    make_client,
    password_login,
    record_cookie,
    totp_code,
)
from tests.leaks import record_secret
from tests.test_auth import firm_events

GATED = ["/api/session/tenants", "/api/admin/users", "/api/firm-audit"]


def create_user(admin_c: TestClient, prefix: str = "new") -> tuple[uuid.UUID, str, str]:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.test"
    r = admin_c.post(
        "/api/admin/users", json={"email": email, "display_name": prefix}, headers=CSRF
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "email", "display_name", "activation_url"}
    return uuid.UUID(body["id"]), email, activation_token_from(body["activation_url"])


def new_password(tag: str) -> str:
    pw = f"activated-{tag}-password-1"
    record_secret("password", pw)
    return pw


def activate(c: TestClient, token: str, pw: str):
    r = c.post("/api/auth/activate", json={"token": token, "new_password": pw}, headers=CSRF)
    record_cookie(c)
    return r


# --- client user ------------------------------------------------------------------------------


def test_new_user_has_no_password_and_the_link_works_once(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    admin_c = login_as("firm_admin")
    uid, email, token = create_user(admin_c)
    with rw_engine.connect() as conn:
        assert conn.execute(select(User.password_hash).where(User.id == uid)).scalar_one() is None
    su = SeedUser(
        id=uid, email=email, password="whatever-whatever-1", totp_secret=None, recovery_codes=()
    )
    with make_client() as anon:
        r = password_login(anon, su)
        assert (r.status_code, r.json()) == (401, {"detail": INVALID_CREDENTIALS})
        pw = new_password("client")
        r = activate(anon, token, pw)
        assert (r.status_code, r.json()) == (200, {"next": "login"}), r.text
        assert COOKIE not in anon.cookies
        second = activate(anon, token, pw)
        assert (second.status_code, second.json()) == (400, {"detail": INVALID_LINK})
        assert password_login(anon, SeedUser(uid, email, pw, None, ())).status_code == 200
    changed = firm_events(rw_engine, uid, "password_changed")
    assert changed[-1].detail == {"via": "activation_link"}
    created = firm_events(rw_engine, uid, "user_created")
    assert created[-1].detail == {"via": "api"} and created[-1].firm_id == seed.firm_id
    # Expired link: same generic response.
    uid2, _, token2 = create_user(admin_c, "exp")
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE \"user\" SET activation_expires_at = now() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": uid2},
        )
    with make_client() as anon:
        r = activate(anon, token2, new_password("exp"))
        assert (r.status_code, r.json()) == (400, {"detail": INVALID_LINK})


def test_failed_redemption_is_persisted_without_the_token(
    client: TestClient, rw_engine: Engine
) -> None:
    token = "not-a-real-token-" + uuid.uuid4().hex
    record_secret("activation_token", token)
    r = activate(client, token, new_password("bad"))
    assert r.status_code == 400
    rows = [
        e
        for e in firm_events(rw_engine, None, "login_failure")
        if e.entity_type == "activation_link"
    ]
    assert rows and rows[-1].detail == {"step": "link", "reason": "invalid_or_expired"}
    assert rows[-1].firm_id is None and rows[-1].ip == client.ip  # type: ignore[attr-defined]
    with rw_engine.connect() as conn:
        details = conn.execute(text("SELECT detail::text FROM firm_audit_log")).scalars().all()
    assert not any(token in d or sha256_hex(token) in d for d in details if d)


# --- firm user: password → TOTP → recovery codes in one flow ----------------------------------


def make_firm_user_via_api(
    admin_c: TestClient, seed: Seed, prefix: str = "staff"
) -> tuple[uuid.UUID, str, str]:
    uid, email, token = create_user(admin_c, prefix)
    r = admin_c.post(
        "/api/admin/firm-memberships",
        json={"user_id": str(uid), "role": "firm_staff"},
        headers=CSRF,
    )
    assert r.status_code == 201, r.text
    r = admin_c.post(
        f"/api/admin/tenants/{seed.tenant_a}/memberships", json={"user_id": str(uid)}, headers=CSRF
    )
    assert r.status_code == 201, r.text
    return uid, email, token


def test_firm_user_link_flow_enrols_totp_before_any_usable_session(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    admin_c = login_as("firm_admin")
    uid, email, token = make_firm_user_via_api(admin_c, seed)
    pw = new_password("firm")
    with make_client() as c:
        r = activate(c, token, pw)
        assert (r.status_code, r.json()) == (200, {"next": "totp_enrol"}), r.text
        assert COOKIE in c.cookies
        me = c.get("/api/session/me").json()
        assert (me["totp"], me["firm_role"], me["active_tenant_id"]) == (
            "enrol_required",
            "firm_staff",
            None,
        )
        for path in GATED:
            assert c.get(path).status_code == 403, path
        assert (
            c.post(
                "/api/session/tenant", json={"tenant_id": str(seed.tenant_a)}, headers=CSRF
            ).status_code
            == 403
        )
        # The enrolment-only session lasts 15 minutes; a normal one 12 hours.
        with rw_engine.connect() as conn:
            created, expires = conn.execute(
                select(UserSession.created_at, UserSession.expires_at).where(
                    UserSession.user_id == uid
                )
            ).one()
        assert (expires - created).total_seconds() == 15 * 60
        # Meanwhile a password login for this firm user is the generic failure.
        with make_client() as other:
            r = password_login(other, SeedUser(uid, email, pw, None, ()))
            assert (r.status_code, r.json()) == (401, {"detail": INVALID_CREDENTIALS})
            assert COOKIE not in other.cookies
        r = c.post("/api/auth/totp/enrol", headers=CSRF)
        assert r.status_code == 200, r.text
        secret = r.json()["secret"]
        record_secret("totp_secret", secret)
        r = c.post("/api/auth/totp/enrol/confirm", json={"code": totp_code(secret)}, headers=CSRF)
        assert r.status_code == 200, r.text
        codes = r.json()["recovery_codes"]
        assert len(codes) == 10
        for code in codes:
            record_secret("recovery_code", code)
        assert r.json()["totp"] == "ok"
        record_cookie(c)
        with rw_engine.connect() as conn:
            created, expires = conn.execute(
                select(UserSession.created_at, UserSession.expires_at).where(
                    UserSession.user_id == uid
                )
            ).one()
        assert (expires - created).total_seconds() >= 11 * 3600
        assert [t["role"] for t in c.get("/api/session/tenants").json()] == ["firm_staff"]
        # Now the normal login path works: password + TOTP.
        with make_client() as again:
            r = password_login(again, SeedUser(uid, email, pw, secret, ()))
            assert (r.status_code, r.json()["totp"]) == (200, "verify_required")
    success = firm_events(rw_engine, uid, "login_success")
    assert success and success[-1].detail["method"] == "password+totp"


def test_enrolment_only_session_expires_after_fifteen_minutes(
    login_as: Callable[..., TestClient], seed: Seed, owner_engine: Engine
) -> None:
    admin_c = login_as("firm_admin")
    _uid, _email, token = make_firm_user_via_api(admin_c, seed, "ttl")
    normal = login_as("client_viewer")
    with make_client() as c:
        assert activate(c, token, new_password("ttl")).status_code == 200
        assert c.get("/api/session/me").status_code == 200
        for cl in (c, normal):
            with owner_engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE session SET expires_at = expires_at - interval '16 minutes' "
                        "WHERE token_hash = :h"
                    ),
                    {"h": sha256_hex(cl.cookies.get(COOKIE))},
                )
        assert c.get("/api/session/me").status_code == 401  # 15-minute lifetime is up
        assert normal.get("/api/session/me").status_code == 200  # 12 hours is not


def test_new_link_invalidates_the_earlier_one(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    admin_c = login_as("firm_admin")
    uid, _email, first = create_user(admin_c, "twice")
    r = admin_c.post(f"/api/admin/users/{uid}/activation-link", headers=CSRF)
    assert r.status_code == 200
    second = activation_token_from(r.json()["activation_url"])
    with make_client() as anon:
        r = activate(anon, first, new_password("first"))
        assert (r.status_code, r.json()) == (400, {"detail": INVALID_LINK})
        assert activate(anon, second, new_password("second")).status_code == 200


def test_admin_totp_reset_reissues_a_link_and_the_old_factors_stop_working(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    admin_c = login_as("firm_admin")
    uid, email, token = make_firm_user_via_api(admin_c, seed, "reset")
    pw = new_password("reset")
    with make_client() as c:
        activate(c, token, pw)
        secret = c.post("/api/auth/totp/enrol", headers=CSRF).json()["secret"]
        record_secret("totp_secret", secret)
        r = c.post("/api/auth/totp/enrol/confirm", json={"code": totp_code(secret)}, headers=CSRF)
        codes = r.json()["recovery_codes"]
        for code in codes:
            record_secret("recovery_code", code)
        assert c.get("/api/session/me").status_code == 200
        r = admin_c.post(f"/api/admin/users/{uid}/totp-reset", headers=CSRF)
        assert r.status_code == 200, r.text
        new_link = activation_token_from(r.json()["activation_url"])
        assert c.get("/api/session/me").status_code == 401  # sessions ended
    su = SeedUser(uid, email, pw, secret, tuple(codes))
    with make_client() as c:
        # Password login is refused generically (firm user, no TOTP); so no session
        # exists in which an old code or recovery code could even be tried.
        r = password_login(c, su)
        assert (r.status_code, r.json()) == (401, {"detail": INVALID_CREDENTIALS})
        assert (
            c.post(
                "/api/auth/totp/verify", json={"code": totp_code(secret)}, headers=CSRF
            ).status_code
            == 401
        )
        assert (
            c.post("/api/auth/totp/recover", json={"code": codes[0]}, headers=CSRF).status_code
            == 401
        )
    with rw_engine.connect() as conn:
        enc, enrolled, hashes = conn.execute(
            select(User.totp_secret_enc, User.totp_enrolled_at, User.recovery_code_hashes).where(
                User.id == uid
            )
        ).one()
    assert enc is None and enrolled is None and hashes is None
    reset = firm_events(rw_engine, uid, "totp_reset")
    assert (
        reset[-1].detail == {"via": "admin"}
        and reset[-1].actor_user_id == seed.users["firm_admin"].id
    )
    with make_client() as c:
        pw2 = new_password("reset2")
        assert activate(c, new_link, pw2).json() == {"next": "totp_enrol"}
        secret2 = c.post("/api/auth/totp/enrol", headers=CSRF).json()["secret"]
        record_secret("totp_secret", secret2)
        r = c.post("/api/auth/totp/enrol/confirm", json={"code": totp_code(secret2)}, headers=CSRF)
        assert r.status_code == 200
        for code in r.json()["recovery_codes"]:
            record_secret("recovery_code", code)
        assert c.get("/api/session/tenants").status_code == 200


# --- enrolment idempotency (F02.1 fix) -----------------------------------------------------------


def start_enrolment(c: TestClient) -> dict:
    r = c.post("/api/auth/totp/enrol", headers=CSRF)
    assert r.status_code == 200, r.text
    record_secret("totp_secret", r.json()["secret"])
    return r.json()


def test_enrol_twice_returns_the_same_pending_secret_and_the_first_code_confirms(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    """A refresh, a double click, or React StrictMode's doubled mount effect calls
    enrol twice; the screen may show either response."""
    admin_c = login_as("firm_admin")
    uid, _email, token = make_firm_user_via_api(admin_c, seed, "idem")
    with make_client() as c:
        assert activate(c, token, new_password("idem")).json() == {"next": "totp_enrol"}
        first = start_enrolment(c)
        second = start_enrolment(c)
        assert second == first
        # Pending, and stored like the enrolled secret: ciphertext under the key ring.
        with rw_engine.connect() as conn:
            enc, key_id, enrolled = conn.execute(
                select(User.totp_secret_enc, User.totp_key_id, User.totp_enrolled_at).where(
                    User.id == uid
                )
            ).one()
        assert enrolled is None and key_id is not None
        assert first["secret"].encode() not in bytes(enc)
        plain = get_keyring().decrypt(key_id, bytes(enc), aad=uid.bytes).decode()
        assert plain == first["secret"]
        r = c.post(
            "/api/auth/totp/enrol/confirm",
            json={"code": totp_code(first["secret"])},
            headers=CSRF,
        )
        assert r.status_code == 200, r.text
        for code in r.json()["recovery_codes"]:
            record_secret("recovery_code", code)
        record_cookie(c)
        # Confirmed: the same ciphertext is now the enrolled secret; enrol refuses.
        with rw_engine.connect() as conn:
            enc_after, enrolled = conn.execute(
                select(User.totp_secret_enc, User.totp_enrolled_at).where(User.id == uid)
            ).one()
        assert enrolled is not None and bytes(enc_after) == bytes(enc)
        assert c.post("/api/auth/totp/enrol", headers=CSRF).status_code == 409


def test_concurrent_enrol_calls_return_the_same_secret(
    login_as: Callable[..., TestClient], seed: Seed, owner_engine: Engine
) -> None:
    """Deterministic race: the test holds the user row lock until both requests are
    waiting on it, then lets go. Without ``FOR UPDATE`` in the service both requests
    read "nothing pending" before either writes, and the secrets differ."""
    admin_c = login_as("firm_admin")
    uid, _email, token = make_firm_user_via_api(admin_c, seed, "race")
    with make_client() as c, make_client() as twin:
        assert activate(c, token, new_password("race")).json() == {"next": "totp_enrol"}
        twin.cookies.set(COOKIE, c.cookies.get(COOKIE))  # same session, second connection
        with ThreadPoolExecutor(max_workers=2) as pool:
            with owner_engine.connect() as holder:
                holder.execute(text('SELECT 1 FROM "user" WHERE id = :id FOR UPDATE'), {"id": uid})
                futures = [pool.submit(start_enrolment, client) for client in (c, twin)]
                deadline = time.monotonic() + 15
                with owner_engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as watcher:
                    while True:
                        waiting = watcher.execute(
                            text(
                                # pg_locks, not wait_event: a non-superuser sees no
                                # wait_event for another role's backend.
                                "SELECT count(DISTINCT l.pid) FROM pg_locks l "
                                "JOIN pg_stat_activity a ON a.pid = l.pid "
                                "WHERE NOT l.granted AND a.datname = current_database()"
                            )
                        ).scalar_one()
                        if waiting >= 2 or time.monotonic() > deadline:
                            break
                        time.sleep(0.05)
                holder.rollback()  # release; the two requests now run one after the other
            results = [f.result(timeout=30) for f in futures]
        assert waiting >= 2, "both enrol requests must queue on the user row lock"
        assert results[0] == results[1]
        r = c.post(
            "/api/auth/totp/enrol/confirm",
            json={"code": totp_code(results[0]["secret"])},
            headers=CSRF,
        )
        assert r.status_code == 200, r.text
        for code in r.json()["recovery_codes"]:
            record_secret("recovery_code", code)
        record_cookie(c)


def pending_secret_enc(engine: Engine, uid: uuid.UUID) -> bytes | None:
    with engine.connect() as conn:
        enc, enrolled = conn.execute(
            select(User.totp_secret_enc, User.totp_enrolled_at).where(User.id == uid)
        ).one()
    assert enrolled is None
    return None if enc is None else bytes(enc)


def test_abandoned_pending_secret_is_never_shown_to_a_later_enrolment(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    """Cleared by: issuing a new link, redeeming a link, an admin TOTP reset."""
    admin_c = login_as("firm_admin")
    uid, _email, token = make_firm_user_via_api(admin_c, seed, "abandon")
    seen: list[str] = []
    with make_client() as c:
        assert activate(c, token, new_password("abandon-1")).json() == {"next": "totp_enrol"}
        seen.append(start_enrolment(c)["secret"])  # and walks away
    assert pending_secret_enc(rw_engine, uid) is not None

    # Issue-link clears it at once, before anyone redeems the new link.
    r = admin_c.post(f"/api/admin/users/{uid}/activation-link", headers=CSRF)
    assert r.status_code == 200, r.text
    token = activation_token_from(r.json()["activation_url"])
    assert pending_secret_enc(rw_engine, uid) is None
    with make_client() as c:
        assert activate(c, token, new_password("abandon-2")).json() == {"next": "totp_enrol"}
        seen.append(start_enrolment(c)["secret"])

    # Admin TOTP reset clears a pending secret as it clears an enrolled one.
    r = admin_c.post(f"/api/admin/users/{uid}/totp-reset", headers=CSRF)
    assert r.status_code == 200, r.text
    token = activation_token_from(r.json()["activation_url"])
    assert pending_secret_enc(rw_engine, uid) is None
    with make_client() as c:
        assert activate(c, token, new_password("abandon-3")).json() == {"next": "totp_enrol"}
        seen.append(start_enrolment(c)["secret"])
    assert len(set(seen)) == 3


def test_link_redemption_clears_a_pending_secret(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine, owner_engine: Engine
) -> None:
    """Redemption clears on its own account, not only because issuing did: a pending
    secret that appears between issue and redemption is gone afterwards."""
    admin_c = login_as("firm_admin")
    uid, _email, token = make_firm_user_via_api(admin_c, seed, "redeem")
    with make_client() as c:
        assert activate(c, token, new_password("redeem-1")).json() == {"next": "totp_enrol"}
        start_enrolment(c)
    stale = pending_secret_enc(rw_engine, uid)
    with owner_engine.connect() as conn:
        key_id = conn.execute(select(User.totp_key_id).where(User.id == uid)).scalar_one()
    r = admin_c.post(f"/api/admin/users/{uid}/activation-link", headers=CSRF)
    token = activation_token_from(r.json()["activation_url"])
    with owner_engine.begin() as conn:  # put the stale pending secret back
        conn.execute(
            text('UPDATE "user" SET totp_secret_enc = :enc, totp_key_id = :kid WHERE id = :id'),
            {"enc": stale, "kid": key_id, "id": uid},
        )
    with make_client() as c:
        assert activate(c, token, new_password("redeem-2")).json() == {"next": "totp_enrol"}
        assert pending_secret_enc(rw_engine, uid) is None


def test_client_user_pending_secret_does_not_survive_into_a_later_login(
    clients: Callable[[], TestClient], seed: Seed, rw_engine: Engine
) -> None:
    """Optional TOTP for a client user is started from a normal session, so the
    "later enrolment session" is the next password login."""
    su = seed.users["client_admin_b"]
    first_c, later_c = clients(), clients()
    assert password_login(first_c, su).status_code == 200
    abandoned = start_enrolment(first_c)["secret"]
    assert password_login(later_c, su).status_code == 200
    assert pending_secret_enc(rw_engine, su.id) is None
    assert start_enrolment(later_c)["secret"] != abandoned
    # Leave the seed user as found: nothing pending.
    assert password_login(clients(), su).status_code == 200
    assert pending_secret_enc(rw_engine, su.id) is None


def test_no_route_lets_a_firm_user_enrol_with_a_password_alone(
    client: TestClient, seed: Seed
) -> None:
    su = seed.users["firm_nototp"]
    r = password_login(client, su)
    wrong = password_login(client, su, password="definitely-not-the-password")
    assert r.status_code == wrong.status_code == 401
    assert r.json() == wrong.json() == {"detail": INVALID_CREDENTIALS}
    assert COOKIE not in client.cookies
    assert client.post("/api/auth/totp/enrol", headers=CSRF).status_code == 401


# --- sessions end on every credential change ------------------------------------------------


def test_password_change_ends_every_session_including_the_callers(
    login_as: Callable[..., TestClient], seed: Seed, rw_engine: Engine
) -> None:
    su = seed.users["client_admin"]
    first, second = login_as("client_admin"), login_as("client_admin")
    r = first.post(
        "/api/auth/password/change",
        json={"current_password": "not-it-not-it-not-it", "new_password": "something-longer-1"},
        headers=CSRF,
    )
    assert r.status_code == 401
    pw = new_password("self")
    r = first.post(
        "/api/auth/password/change",
        json={"current_password": su.password, "new_password": pw},
        headers=CSRF,
    )
    assert r.status_code == 204 and "set-cookie" in r.headers
    assert first.get("/api/session/me").status_code == 401
    assert second.get("/api/session/me").status_code == 401
    with make_client() as fresh:
        assert password_login(fresh, su).status_code == 401
        assert password_login(fresh, su, password=pw).status_code == 200
        r = fresh.post(  # restore the seed password for other tests
            "/api/auth/password/change",
            json={"current_password": pw, "new_password": su.password},
            headers=CSRF,
        )
        assert r.status_code == 204
    assert firm_events(rw_engine, su.id, "password_changed")[-1].detail == {"via": "self"}


def test_link_issue_and_redemption_end_sessions_but_membership_removal_keeps_them(
    login_as: Callable[..., TestClient], seed: Seed
) -> None:
    victim_su = seed.users["client_admin_b"]
    victim = login_as("client_admin_b", tenant=seed.tenant_b)
    admin_c = login_as("firm_admin")
    r = admin_c.post(f"/api/admin/users/{victim_su.id}/activation-link", headers=CSRF)
    token = activation_token_from(r.json()["activation_url"])
    assert victim.get("/api/session/me").status_code == 401  # issuing ends sessions
    victim = login_as("client_admin_b", tenant=seed.tenant_b)  # old password still works
    with make_client() as anon:
        assert (
            activate(anon, token, victim_su.password).status_code == 200
        )  # same password, new hash
    assert victim.get("/api/session/me").status_code == 401  # redemption ends sessions
    # Removing a membership leaves the session valid with no active tenant.
    victim = login_as("client_admin_b", tenant=seed.tenant_b)
    r = admin_c.delete(
        f"/api/admin/tenants/{seed.tenant_b}/memberships/{victim_su.id}", headers=CSRF
    )
    assert r.status_code == 204
    me = victim.get("/api/session/me")
    assert (me.status_code, me.json()["active_tenant_id"]) == (200, None)
    r = admin_c.post(
        f"/api/admin/tenants/{seed.tenant_b}/memberships",
        json={"user_id": str(victim_su.id), "role": "client_admin"},
        headers=CSRF,
    )
    assert r.status_code == 201


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_issue_link_prints_the_link_once_and_the_link_works(
    seed: Seed, capsys, owner_engine: Engine
) -> None:
    su = seed.users["scratch"]
    cli.main(["issue-link", "--email", su.email.upper()])
    out = capsys.readouterr().out
    url = next(line for line in out.splitlines() if line.startswith("https://"))
    token = activation_token_from(url)
    assert out.count(token) == 1
    with make_client() as anon:
        r = activate(anon, token, su.password)  # keep the seed password
        assert (r.status_code, r.json()) == (200, {"next": "login"}), r.text
    # Audit: via cli, actor None.
    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT detail::text, actor_user_id FROM firm_audit_log "
                "WHERE action = 'activation_link_issued' AND entity_id = :e "
                "ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"e": str(su.id)},
        ).one()
    assert json.loads(row[0])["via"] == "cli" and row[1] is None
