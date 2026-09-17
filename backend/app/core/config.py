"""Configuration via environment (BLUEPRINT §12). No secrets in the repo."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo keeps one .env at its root (README); the API and Alembic run from backend/.
# Later files override earlier ones, so a .env in the working directory still wins.
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # Application role: not the table owner, no BYPASSRLS.
    database_url: str = "postgresql+psycopg://app_rw:app_rw_dev@localhost:5433/wip"
    # Migration role: owns the schema. Only Alembic uses it.
    database_owner_url: str = "postgresql+psycopg://app_owner:app_owner_dev@localhost:5433/wip"

    # Where the React app is served. Used only to build one-time password-reset links.
    app_base_url: str = "http://localhost:5173"

    # Sessions (F02, D-10). The cookie is always HttpOnly, SameSite=Lax, path /.
    # ``Secure`` is configurable only for a browser that does not treat
    # http://localhost as a secure context; production keeps the default.
    session_cookie_name: str = "sid"
    session_cookie_secure: bool = True
    session_idle_minutes: int = 60
    session_absolute_hours: int = 12

    # Login throttle: this many failures inside the window lock the account for the window.
    login_lockout_attempts: int = 5
    login_lockout_minutes: int = 15
    password_reset_ttl_hours: int = 24

    # Application-layer encryption (BLUEPRINT §11; app/core/crypto.py).
    # CRYPTO_KEYS="kid:base64key[,kid:base64key…]", each key 32 random bytes.
    # The active key encrypts; every listed key can decrypt, so keys rotate by
    # adding the new key, switching the active id, and dropping the old key
    # only after every row that used it has been re-encrypted.
    crypto_keys: str = ""
    crypto_active_key_id: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
