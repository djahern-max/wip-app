"""Configuration via environment (BLUEPRINT §12). No secrets in the repo.

Secrets have no defaults (F02.1): ``DATABASE_URL``, ``CRYPTO_KEYS`` and
``CRYPTO_ACTIVE_KEY_ID`` must be set or the process refuses to start with a message
that names the variable and never its value. ``DATABASE_OWNER_URL`` is optional
here and required only by ``alembic/env.py``, so the API process never has to hold
the owner password.

The env file defaults to the repo-root ``.env`` (README); ``ENV_FILE`` points at a
different file, or at a path that does not exist to disable file loading.
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    # --- required (no default; secrets) -----------------------------------------
    # Application role: not the table owner, no BYPASSRLS.
    database_url: str
    # Application-layer encryption (BLUEPRINT §11; app/core/crypto.py).
    # CRYPTO_KEYS="kid:base64key[,kid:base64key…]", each key 32 random bytes.
    # The active key encrypts; every listed key can decrypt, so keys rotate by
    # adding the new key, switching the active id, and dropping the old key
    # only after every row that used it has been re-encrypted.
    crypto_keys: str
    crypto_active_key_id: str

    # Migration role: owns the schema. Only Alembic uses it (required there).
    database_owner_url: str | None = None

    # Where the React app is served. Used to build one-time activation links and
    # as the only origin allowed to make state-changing browser requests.
    app_base_url: str = "http://localhost:5173"

    # Sessions (F02, D-10). The cookie is always HttpOnly, SameSite=Lax, path /.
    # ``Secure`` is configurable only for a browser that does not treat
    # http://localhost as a secure context; production keeps the default.
    session_cookie_name: str = "sid"
    session_cookie_secure: bool = True
    session_idle_minutes: int = 60
    session_absolute_hours: int = 12
    # last_seen_at is written only when it is at least this stale (own transaction).
    session_touch_seconds: int = 60
    # Enrolment-only session created by link redemption for a firm user (D-16).
    enrol_session_ttl_minutes: int = 15

    # Account lockout: this many failures inside the window lock the account for the window.
    login_lockout_attempts: int = 5
    login_lockout_minutes: int = 15
    # Per-IP throttle (F02.1): this many failures from one IP inside the window
    # return 429 for the rest of the window and stop writing audit rows.
    ip_throttle_failures: int = 20
    ip_throttle_minutes: int = 15
    # Number of reverse proxies in front of the API. 0 = trust the socket address
    # only; N = take the N-th address from the right of X-Forwarded-For.
    trusted_proxy_count: int = 0
    # One-time activation link (D-16).
    activation_link_ttl_hours: int = 72


def _env_file() -> str | None:
    path = os.environ.get("ENV_FILE", str(DEFAULT_ENV_FILE))
    return path if Path(path).is_file() else None


class MissingSettings(SystemExit):
    """Raised (as a process exit) when a required variable is absent."""


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings(_env_file=_env_file())
    except ValidationError as exc:
        missing = sorted(
            str(err["loc"][0]).upper() for err in exc.errors() if err["type"] == "missing"
        )
        if missing:
            raise MissingSettings(
                "missing required environment variable(s): " + ", ".join(missing)
            ) from None
        raise MissingSettings(
            "invalid configuration: " + ", ".join(str(e["loc"][0]).upper() for e in exc.errors())
        ) from None
