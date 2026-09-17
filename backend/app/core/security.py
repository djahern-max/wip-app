"""Credential primitives (F02, D-10): argon2id passwords, TOTP (RFC 6238),
recovery codes, opaque tokens. Pure functions: no database, no logging.

Nothing in this module may log or embed a password, secret, code, or token in an
exception message.
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from functools import lru_cache

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
TOTP_STEP_SECONDS = 30
TOTP_WINDOW_STEPS = 1  # ±1 step
TOTP_DIGITS = 6
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
_RECOVERY_LENGTH = 10

# argon2id with the library defaults (RFC 9106 second recommended parameters).
_hasher = PasswordHasher()


# --- passwords ------------------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@lru_cache
def _dummy_hash() -> str:
    """A hash of a random value, verified when there is no real hash to check so an
    unknown account costs the same time as a wrong password."""
    return _hasher.hash(secrets.token_urlsafe(32))


def verify_password(password_hash: str | None, password: str) -> bool:
    """True if ``password`` matches. Always performs one argon2 verification, even
    when the account has no password (``None``) so timing does not reveal that."""
    if password_hash is None:
        try:
            _hasher.verify(_dummy_hash(), password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --- opaque tokens (session ids, reset links) --------------------------------------


def new_token() -> str:
    """256 random bits, URL-safe. Only its SHA-256 is ever stored."""
    return secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- TOTP -----------------------------------------------------------------------------------------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, account: str, issuer: str) -> str:
    return pyotp.TOTP(secret, interval=TOTP_STEP_SECONDS, digits=TOTP_DIGITS).provisioning_uri(
        name=account, issuer_name=issuer
    )


def totp_code_at(secret: str, at: datetime) -> str:
    """The code valid at ``at``. Used by tests and the bootstrap CLI, never by a route."""
    return pyotp.TOTP(secret, interval=TOTP_STEP_SECONDS, digits=TOTP_DIGITS).at(at)


def match_totp(secret: str, code: str, at: datetime) -> int | None:
    """Return the time-step counter that ``code`` matches within ±1 step of ``at``,
    or ``None``. The caller stores the counter and rejects any later code whose
    counter is not greater than the stored one, so a code cannot be replayed."""
    code = code.strip().replace(" ", "").replace("-", "")
    if not (code.isdigit() and len(code) == TOTP_DIGITS):
        return None
    totp = pyotp.TOTP(secret, interval=TOTP_STEP_SECONDS, digits=TOTP_DIGITS)
    for offset in range(-TOTP_WINDOW_STEPS, TOTP_WINDOW_STEPS + 1):
        candidate = at + timedelta(seconds=offset * TOTP_STEP_SECONDS)
        if totp.verify(code, for_time=candidate, valid_window=0):
            return totp.timecode(candidate)
    return None


# --- recovery codes -------------------------------------------------------------------------------


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """``XXXXX-XXXXX`` from a 32-symbol alphabet: 50 bits each. High-entropy random
    values, so a plain SHA-256 is an adequate stored form."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")


def hash_recovery_code(code: str) -> str:
    return sha256_hex(normalize_recovery_code(code))
