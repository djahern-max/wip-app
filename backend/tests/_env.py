"""Environment for the application under test. Imported first by ``conftest``.

Settings are read lazily (``get_settings`` is cached on first call), so setting
the variables at import time is enough; ``conftest`` clears the cache afterwards.
"""

import base64
import os

OWNER_URL = os.environ.get(
    "TEST_DATABASE_OWNER_URL",
    "postgresql+psycopg://app_owner:app_owner_dev@localhost:5433/wip_test",
)
RW_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://app_rw:app_rw_dev@localhost:5433/wip_test",
)

# Two fixed keys so the rotation test can switch the active id while both stay
# configured. Test-only values.
TEST_KEYS: dict[str, str] = {
    "test0": base64.b64encode(bytes(range(32))).decode(),
    "test1": base64.b64encode(bytes(range(32, 64))).decode(),
}
ACTIVE_KEY_ID = "test1"

os.environ["DATABASE_URL"] = RW_URL
os.environ["CRYPTO_KEYS"] = ",".join(f"{k}:{v}" for k, v in TEST_KEYS.items())
os.environ["CRYPTO_ACTIVE_KEY_ID"] = ACTIVE_KEY_ID
os.environ.setdefault("SESSION_COOKIE_SECURE", "true")
os.environ.setdefault("APP_BASE_URL", "https://app.example.test")
