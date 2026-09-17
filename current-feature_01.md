# current-feature.md

## F01 · Project skeleton & tenant isolation
**Roadmap phase**: A · **Blueprint refs**: §3 (principles 8, 10), §11, §12 · **Decisions**: none required
**Status**: done (2026-09-17)

### Goal
A running FastAPI + Postgres + React skeleton in which tenant isolation is enforced by the database and proven by an automated test, so every later feature inherits it for free.

### In scope
- Repo layout per BLUEPRINT §12 (backend, frontend, docs, tests/fixtures, tests/golden).
- Config via environment; local dev with docker compose (Postgres 16).
- Two DB roles: `app_owner` (migrations) and `app_rw` (application; not owner, no BYPASSRLS).
- Tables: `firm`, `tenant`, `user`, `membership`, plus one throwaway tenant-scoped table `_rls_probe` used only by tests.
- Migration helper `enable_tenant_rls(table_name)`: adds policy `tenant_id = current_setting('app.tenant_id')::uuid`, `ENABLE` and `FORCE ROW LEVEL SECURITY`.
- Request-scoped session dependency that opens a transaction and runs `SET LOCAL app.tenant_id`.
- Health endpoint; minimal React shell that calls it.
- CI: lint, tests, migration up/down.

### Out of scope
Auth and roles (F02), worker queue (F03), any integration, any domain table.

### Acceptance criteria
- [x] `alembic upgrade head` then `downgrade base` both succeed on an empty database.
- [x] Test enumerates every table with a `tenant_id` column and fails if RLS is not enabled **and** forced.
- [x] Test: with tenant A context, rows for tenant B are invisible via ORM and via raw SQL on the `app_rw` connection.
- [x] Test: with no tenant context set, tenant tables return zero rows and inserts fail.
- [x] Test: `app_rw` cannot `ALTER TABLE … DISABLE ROW LEVEL SECURITY`.
- [x] Product name appears in exactly one backend and one frontend constant.
- [x] README section: how to run locally, how to create a migration for a tenant table using the helper.

### Plan (Claude Code fills in before coding)

**Acceptance criteria restated**
1. Alembic `upgrade head` and `downgrade base` both succeed on an empty database (verified by a test that creates a scratch database).
2. A test lists every table with a `tenant_id` column and fails unless RLS is enabled *and* forced, a `tenant_isolation` policy exists, and an index leads with `tenant_id` (CLAUDE.md hard rule).
3. With tenant A context, tenant B rows are invisible via ORM and via raw SQL on the `app_rw` connection.
4. With no tenant context, tenant tables return zero rows and inserts fail.
5. `app_rw` cannot `ALTER TABLE … DISABLE ROW LEVEL SECURITY`.
6. Product name lives in exactly one backend constant and one frontend constant (guarded by a test).
7. README covers local run and the tenant-table migration recipe.

**Files**
- `docs/BLUEPRINT.md` (moved from repo root to match §12 and CLAUDE.md), `docs/OPERATIONS.md`, `docs/DECISIONS.md` (header only)
- `docker-compose.yml`, `db/init/01_roles.sh` (creates `app_owner`, `app_rw`, databases `wip` and `wip_test`, default privileges)
- `backend/pyproject.toml`, `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`, `backend/alembic/versions/0001_baseline.py`
- `backend/app/core/{config,db,product}.py`, `backend/app/tenancy/{models,rls}.py`, `backend/app/api/health.py`, `backend/app/main.py`
- `backend/tests/{conftest,test_rls,test_migrations,test_health,test_product_name}.py`, `backend/tests/{fixtures,golden}/.gitkeep`
- `frontend/{package.json,vite.config.js,index.html}`, `frontend/src/{main.jsx,App.jsx,product.js}`
- `.github/workflows/ci.yml`, `README.md`, `.env.example`, `.gitignore`, `Makefile`

**Dependencies** (all named by CLAUDE.md / §12 except the two marked)
- backend: fastapi, uvicorn, sqlalchemy>=2.0, alembic, psycopg[binary] (driver), pydantic-settings (*config via environment*), dev: pytest, httpx (*FastAPI TestClient*), ruff
- frontend: react, react-dom, vite, @vitejs/plugin-react

**Tables added** (all named in the brief): `firm`, `tenant`, `user`, `membership`, `_rls_probe`. `firm`, `tenant`, `user` are not tenant-scoped (§7). `membership` and `_rls_probe` carry `tenant_id` and go through `enable_tenant_rls`.

**Design notes**
- RLS policy uses `NULLIF(current_setting('app.tenant_id', true), '')::uuid` so an unset or cleared setting yields NULL (zero rows, inserts rejected) instead of a cast error.
- Tenant context is set with `set_config('app.tenant_id', :tid, true)` (== `SET LOCAL`) inside the request transaction. Until F02 provides auth, the request dependency reads the tenant id from an `X-Tenant-Id` header; F02 replaces that.
- `app_rw` privileges come from `ALTER DEFAULT PRIVILEGES FOR ROLE app_owner`, so migrations never name the app role.
- Test DB defaults to `wip_test` on the compose Postgres (host port 5433, since 5432 is in use locally).

**Open questions**: none blocking. Package names avoid the product name so the "exactly one constant" rule holds.

### Discovered (do not fix here)
- **F02**: `require_tenant_id` in `backend/app/core/db.py` reads `X-Tenant-Id` from the request header. This is a placeholder with no authentication; F02 must derive the tenant from the session + membership and delete the header path.
- **F02**: `firm`, `tenant`, `user` carry no RLS (not tenant-scoped per §7) and are readable by `app_rw` with no context. Access control for them is a role check, which is F02.
- **F03**: `tenant_session` is what worker tasks should use with an explicit `tenant_id`; nothing enforces that yet because there is no worker.
- **Tooling**: Starlette warns that `httpx` with `TestClient` is deprecated in favour of `httpx2`. Harmless now; revisit when pinning dependencies for deployment (F23).
- **Verification**: acceptance was demonstrated with the two seeded test tenants. No Rye Beach fixture applies to F01 (infrastructure only), consistent with CLAUDE.md workflow rule 5.

### Close-out
- [x] CHANGELOG entry written
- [x] ROADMAP status flipped
- [x] OPERATIONS.md: local setup + role creation documented
