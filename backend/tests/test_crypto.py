"""AES-256-GCM key ring with rotation (BLUEPRINT §11; F02 uses it for TOTP secrets)."""

import base64
import os
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.core.config import get_settings
from app.core.crypto import CryptoError, Keyring
from app.tenancy.models import User
from tests._env import TEST_KEYS
from tests.conftest import CSRF, Seed, password_login, reset_totp_counter, totp_code

K1 = base64.b64encode(os.urandom(32)).decode()
K2 = base64.b64encode(os.urandom(32)).decode()


def test_roundtrip_and_key_id() -> None:
    ring = Keyring.parse(f"k1:{K1}", "k1")
    kid, blob = ring.encrypt(b"hello", aad=b"row-1")
    assert kid == "k1"
    assert b"hello" not in blob
    assert ring.decrypt(kid, blob, aad=b"row-1") == b"hello"


def test_nonce_is_fresh_per_call() -> None:
    ring = Keyring.parse(f"k1:{K1}", "k1")
    assert ring.encrypt(b"x")[1] != ring.encrypt(b"x")[1]


def test_rotation_keeps_old_ciphertext_readable() -> None:
    old = Keyring.parse(f"k1:{K1}", "k1")
    kid, blob = old.encrypt(b"secret")
    rotated = Keyring.parse(f"k1:{K1},k2:{K2}", "k2")
    assert rotated.decrypt(kid, blob) == b"secret"
    assert rotated.encrypt(b"new")[0] == "k2"
    # Dropping the old key before re-encrypting makes old rows unreadable: loudly.
    dropped = Keyring.parse(f"k2:{K2}", "k2")
    with pytest.raises(CryptoError, match="no key configured"):
        dropped.decrypt(kid, blob)


def test_tamper_and_aad_mismatch_fail() -> None:
    ring = Keyring.parse(f"k1:{K1}", "k1")
    kid, blob = ring.encrypt(b"secret", aad=b"a")
    with pytest.raises(CryptoError, match="decryption failed"):
        ring.decrypt(kid, blob, aad=b"b")
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(CryptoError, match="decryption failed"):
        ring.decrypt(kid, tampered, aad=b"a")


@pytest.mark.parametrize(
    ("spec", "active", "message"),
    [
        ("", "k1", "no encryption keys"),
        (f"k1:{K1}", "k2", "not one of the configured"),
        ("k1:AAAA", "k1", "must be 32 bytes"),
        ("k1", "k1", "key_id:base64key"),
        ("k1:not-base64!", "k1", "not valid base64"),
    ],
)
def test_bad_configuration_is_rejected(spec: str, active: str, message: str) -> None:
    with pytest.raises(CryptoError, match=message):
        Keyring.parse(spec, active)


def test_error_messages_carry_no_key_material() -> None:
    try:
        Keyring.parse(f"k1:{K1}", "k2")
    except CryptoError as exc:
        assert K1 not in str(exc)


# --- TOTP secret at rest -------------------------------------------------------------------


def test_totp_secret_is_ciphertext_with_key_id(seed: Seed, owner_engine: Engine) -> None:
    su = seed.users["firm_admin"]
    with owner_engine.connect() as conn:
        enc, kid = conn.execute(
            select(User.totp_secret_enc, User.totp_key_id).where(User.id == su.id)
        ).one()
    assert kid == "test1"
    assert su.totp_secret.encode() not in enc
    ring = Keyring.parse(",".join(f"{k}:{v}" for k, v in TEST_KEYS.items()), "test0")
    assert ring.decrypt(kid, enc, aad=su.id.bytes).decode() == su.totp_secret


def test_totp_still_verifies_after_active_key_rotates(
    seed: Seed,
    owner_engine: Engine,
    clients: Callable[[], TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active key moves to test0; test1 (which encrypted the seed) stays configured."""
    monkeypatch.setenv("CRYPTO_ACTIVE_KEY_ID", "test0")
    get_settings.cache_clear()
    try:
        c = clients()
        su = seed.users["rotate_me"]
        assert password_login(c, su).status_code == 200
        reset_totp_counter(owner_engine, su.id)
        r = c.post("/api/auth/totp/verify", json={"code": totp_code(su.totp_secret)}, headers=CSRF)
        assert (r.status_code, r.json()["totp"]) == (200, "ok")
    finally:
        monkeypatch.setenv("CRYPTO_ACTIVE_KEY_ID", "test1")
        get_settings.cache_clear()
