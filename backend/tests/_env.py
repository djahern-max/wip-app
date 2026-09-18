"""Environment for the application under test. Hermetic: nothing here comes from the
developer's machine except the two TEST_DATABASE_* URLs.

``tests/__init__.py`` imports this module, so Python runs it before ``conftest`` or
any test module, whatever order an import sorter gives their imports. It must run
before the application is imported: ``app.main`` builds the app (and ``Settings``)
at import, and a ``Settings`` built before ENV_FILE is set below would load the
repo-root ``.env``. The guard refuses that order instead of letting it pass silently.
"""

import base64
import os
import sys
import tempfile

if "app.core.config" in sys.modules:
    raise RuntimeError("tests._env must be imported before the application package")

OWNER_URL = os.environ.get(
    "TEST_DATABASE_OWNER_URL",
    "postgresql+psycopg://app_owner:app_owner_dev@localhost:5433/wip_test",
)
RW_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://app_rw:app_rw_dev@localhost:5433/wip_test",
)

# A throwaway key ring, generated for this run and never written anywhere. Two keys
# so the rotation test can switch the active id while both stay configured.
TEST_KEYS: dict[str, str] = {
    kid: base64.b64encode(os.urandom(32)).decode() for kid in ("test0", "test1")
}
ACTIVE_KEY_ID = "test1"

# F03: a throwaway local object store per run, outside the repo.
OBJECT_STORE_DIR = tempfile.mkdtemp(prefix="wip-object-store-")

# Never read the developer's .env: a path that does not exist disables file loading.
ENVIRONMENT: dict[str, str] = {
    "ENV_FILE": "/nonexistent/.env.for-tests",
    "DATABASE_URL": RW_URL,
    "CRYPTO_KEYS": ",".join(f"{k}:{v}" for k, v in TEST_KEYS.items()),
    "CRYPTO_ACTIVE_KEY_ID": ACTIVE_KEY_ID,
    "SESSION_COOKIE_SECURE": "true",
    "APP_BASE_URL": "https://app.example.test",
    "OBJECT_STORE": "local",
    "LOCAL_OBJECT_STORE_DIR": OBJECT_STORE_DIR,
    "WORKER_POLL_SECONDS": "0.2",
}
os.environ.update(ENVIRONMENT)
