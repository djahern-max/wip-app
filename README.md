# Job cost & WIP platform

Multi-tenant job cost and work-in-progress reporting for construction and landscape
contractors, operated by a CPA practice. Design: [docs/BLUEPRINT.md](docs/BLUEPRINT.md).
Build order: [ROADMAP.md](ROADMAP.md). Standing rules for contributors and for Claude
Code: [CLAUDE.md](CLAUDE.md). Runbooks: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Layout

```
backend/     FastAPI + SQLAlchemy 2.0 + Alembic (Python 3.12)
  app/core/      config, db session + tenant context, auth (sessions, principal),
                 authz (role capabilities), crypto, audit writers, product constant
  app/tenancy/   firm, tenant, user, firm_membership, membership, session models; RLS + append-only helpers
  app/auth/      service: login, TOTP, recovery, activation links; admin: users, tenants, memberships
  app/audit/     audit_log, firm_audit_log models
  app/api/       routers (health, auth, session, admin, audit) and allow-list response schemas
  alembic/       migrations (run as app_owner)
  scripts/       create_user.py: bootstrap the practice, create users, issue activation links, reset TOTP
  tests/         pytest (probe routes live here, not in the app); fixtures/ and golden/ hold anonymized Rye Beach data
frontend/    React 18 + Vite, plain JavaScript
db/init/     role + database bootstrap for Postgres 16
docs/        BLUEPRINT, OPERATIONS, DECISIONS
```

## Run locally

Prerequisites: Docker, Python 3.12, Node 20.

```sh
make db-up               # Postgres 16 on localhost:5433; creates app_owner, app_rw, wip, wip_test
make backend-install     # venv at backend/.venv
cp .env.example .env     # then generate CRYPTO_KEYS (see the file); the API refuses to start without it
make migrate             # alembic upgrade head, as app_owner
# first user (no signup): see docs/OPERATIONS.md "Bootstrap the practice"
make api                 # http://localhost:8000/api/health
cd frontend && npm install && npm run dev   # http://localhost:5173 (proxies /api)
```

Tests and lint (need the compose database):

```sh
make test
make lint
```

The test suite migrates `wip_test` to head, seeds two firms, four tenants and one
user per role, and proves that every table carrying `tenant_id` has row-level
security enabled and forced (and no policy beyond the allow-listed ones), that the
application role cannot read or write across tenants, that a session with no tenant
context sees nothing, the full role matrix, TOTP and lockout behaviour, session
rotation and expiry, append-only audit tables, and that no secret reaches a log line.

## Database roles

| Role | Used by | Privileges |
|---|---|---|
| `app_owner` | Alembic, seed scripts | Owns the schema. `CREATEDB` (for scratch test databases). No superuser. |
| `app_rw` | The API and the worker | DML on tables created by `app_owner` via default privileges. Not the owner, `NOSUPERUSER`, `NOBYPASSRLS`. |

Because every tenant table has `FORCE ROW LEVEL SECURITY`, the policy applies to
`app_owner` too: any script that touches tenant rows must set tenant context.

## Authentication, roles, tenant context (F02, F02.1)

E-mail + password (argon2id), TOTP mandatory for `firm_admin` / `firm_staff`,
server-side sessions in the `session` table carried by an `HttpOnly; Secure;
SameSite=Lax` cookie (only the SHA-256 of the id is stored). Every state-changing
request needs the `X-Requested-With` header (CSRF) and, from a browser, an `Origin`
equal to `APP_BASE_URL`. The active tenant lives in the session; nothing from the
request selects it.

Firm authority is a row in `firm_membership` (D-15). A firm user's row in a tenant
is an *entry row* (`membership.role` NULL); the role that applies inside a tenant
is computed by exactly one function, `app.core.auth.effective_role`, and nothing
else reads `membership.role`. Accounts are activated only through an admin-issued
one-time link (D-16) that sets the password and, for firm users, enrols TOTP in the
same flow; the CLI issues links and never sets passwords.

```python
from app.core.auth import TenantSession, VerifiedPrincipal
from app.core.authz import can_view_pay_rates
from app.core.db import tenant_session

# Worker task or script: tenant_id is an explicit argument.
with tenant_session(engine, tenant_id) as session:
    session.execute(select(Membership))      # only this tenant's rows

# Request handler: one transaction per request; the principal dependency sets
# app.user_id and app.tenant_id (from the session row) on it.
@router.get("/things")
def list_things(session: TenantSession): ...

# Role checks are named capabilities, never inline role lists.
@router.get("/rates", dependencies=[Depends(can_view_pay_rates)])
def rates(principal: VerifiedPrincipal): ...
```

`set_config('app.tenant_id', :id, true)` (the parameterized form of `SET LOCAL`)
runs inside the request transaction; the setting expires with the transaction, so
pooled connections carry nothing over. `app.user_id` is set the same way and lets a
user read their own `membership` rows before a tenant is chosen (D-11). Audit rows
(`audit_log` per tenant, `firm_audit_log` per firm) are written in the same
transaction as the action and the tables are insert-only by trigger (D-12, D-13).

## Adding a tenant-scoped table

1. Add the model in the right `app/<area>/models.py` with
   `tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenant.id"), nullable=False)`
   and an index (or unique constraint) whose **first** column is `tenant_id`.
2. Create the migration: `cd backend && alembic revision -m "add widget"`.
3. In `upgrade()`, after `op.create_table(...)` and the index, call the helper:

   ```python
   from app.tenancy.rls import enable_tenant_rls

   op.create_table("widget", ..., sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                   sa.ForeignKey("tenant.id"), nullable=False), ...)
   op.create_index("ix_widget_tenant_id", "widget", ["tenant_id"])
   enable_tenant_rls("widget")
   ```

   The helper runs `ENABLE` and `FORCE ROW LEVEL SECURITY` and creates the
   `tenant_isolation` policy
   (`tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`).
4. `downgrade()` drops the table (dropping the table drops its policy). If the
   downgrade keeps the table, call `disable_tenant_rls("widget")` instead.
5. `make test`. `tests/test_rls.py` enumerates every table with a `tenant_id`
   column and fails if RLS is not enabled and forced, the policy is missing, or no
   index leads with `tenant_id`. Do not weaken that test.

Migrations never name `app_rw`; its access comes from the default privileges set
in `db/init/01_roles.sh`.

## Adding a source kind (F03)

A source kind is a `SourceKind` registered in `app/integrations/base.py` (or in the
integration's own module, imported from there): a name, the accepted extensions,
`parse(stream) -> Iterable[RawItem | RejectedItem]`, and the `source` name written to
`raw_record.source`. The import pipeline (`app/ingest/imports.py`) stores and checksums
the file, runs `parse`, and hands each `RawItem` to `store_raw`; a bad row is a
`RejectedItem` (counted, the rest still loads), and a `parse` that raises fails the
batch. Money in payloads is `Decimal`; a `float` anywhere in a payload is refused at
the engine. Test-only kinds live under `tests/` (see `tests/csv_source.py`).
