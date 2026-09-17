"""Application-layer encryption: AES-256-GCM with a key ring (BLUEPRINT §11).

The key id is stored next to the ciphertext so keys can rotate: the active key
encrypts, every configured key decrypts. F02 uses this for TOTP secrets; F03
reuses it for OAuth tokens. Keys come from the environment (``CRYPTO_KEYS``,
``CRYPTO_ACTIVE_KEY_ID``), never from the repo.

Blob layout: 12-byte random nonce || ciphertext || 16-byte GCM tag.
Error messages never contain key material or plaintext.
"""

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

KEY_BYTES = 32
NONCE_BYTES = 12


class CryptoError(Exception):
    """Configuration or decryption failure. Safe to surface; carries no secret."""


@dataclass(frozen=True)
class Keyring:
    keys: dict[str, bytes]
    active_key_id: str

    def __post_init__(self) -> None:
        if not self.keys:
            raise CryptoError("no encryption keys configured (CRYPTO_KEYS)")
        if self.active_key_id not in self.keys:
            raise CryptoError("CRYPTO_ACTIVE_KEY_ID is not one of the configured key ids")
        for kid, key in self.keys.items():
            if len(key) != KEY_BYTES:
                raise CryptoError(f"key {kid!r} must be {KEY_BYTES} bytes")

    @classmethod
    def parse(cls, spec: str, active_key_id: str) -> "Keyring":
        keys: dict[str, bytes] = {}
        for item in (part.strip() for part in spec.split(",")):
            if not item:
                continue
            kid, sep, b64 = item.partition(":")
            if not sep or not kid or not b64:
                raise CryptoError("CRYPTO_KEYS entries must look like key_id:base64key")
            try:
                keys[kid] = base64.b64decode(b64, validate=True)
            except ValueError as exc:
                raise CryptoError(f"key {kid!r} is not valid base64") from exc
        return cls(keys=keys, active_key_id=active_key_id)

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> tuple[str, bytes]:
        """Return ``(key_id, blob)``. Store both; ``aad`` binds the blob to its row."""
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self.keys[self.active_key_id]).encrypt(nonce, plaintext, aad)
        return self.active_key_id, nonce + sealed

    def decrypt(self, key_id: str, blob: bytes, aad: bytes = b"") -> bytes:
        key = self.keys.get(key_id)
        if key is None:
            raise CryptoError(f"no key configured for key id {key_id!r}")
        if len(blob) <= NONCE_BYTES:
            raise CryptoError("ciphertext too short")
        try:
            return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
        except InvalidTag as exc:
            raise CryptoError("decryption failed (wrong key, tampered data, or wrong aad)") from exc


def get_keyring() -> Keyring:
    s = get_settings()
    return Keyring.parse(s.crypto_keys, s.crypto_active_key_id)
