# OPERATIONS

Runbooks. Updated as features land (BLUEPRINT §15.6).

## Local setup (F01)

1. `make db-up` starts Postgres 16 in Docker on `localhost:5433` (5432 is left for
   any local Postgres). On first start `db/init/01_roles.sh` runs and creates:
   - roles `app_owner` (schema owner, `CREATEDB`) and `app_rw` (application,
     `NOSUPERUSER NOBYPASSRLS`, not owner);
   - databases `wip` (dev) and `wip_test` (pytest), owned by `app_owner`;
   - default privileges so `app_rw` gets `SELECT/INSERT/UPDATE/DELETE` on every
     table `app_owner` creates and `USAGE/SELECT` on sequences.
   Dev passwords default to `app_owner_dev` / `app_rw_dev`; override with
   `APP_OWNER_PASSWORD` / `APP_RW_PASSWORD` in the environment before the first
   `docker compose up`. To re-run the bootstrap, `docker compose down -v` (destroys
   local data) then `make db-up`.
2. `make backend-install`, `cp .env.example .env`, `make migrate`.
3. `make api` and, in another shell, `cd frontend && npm install && npm run dev`.

## Role creation on a managed Postgres (staging/production)

Run `db/init/01_roles.sh` once against the server with a superuser or the
managed-service admin role:

```sh
APP_OWNER_PASSWORD=… APP_RW_PASSWORD=… APP_DATABASES=wip \
PGHOST=… PGPORT=… POSTGRES_USER=doadmin PGPASSWORD=… db/init/01_roles.sh
```

Then set `DATABASE_OWNER_URL` (app_owner) for the migration step and
`DATABASE_URL` (app_rw) for the API and worker. Verify the application role:

```sql
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('app_owner','app_rw');
-- both must be f / f
```

## Migrations

- Apply: `cd backend && alembic upgrade head` with `DATABASE_OWNER_URL` set.
- Roll back one: `alembic downgrade -1`. Every migration is reversible or says why
  not in its docstring.
- New tenant-scoped table: follow "Adding a tenant-scoped table" in `README.md`.
  The CI test fails if the RLS helper was not called.

## Checking tenant isolation by hand

```sql
-- as app_rw
BEGIN;
SELECT set_config('app.tenant_id', '<tenant uuid>', true);
SELECT count(*) FROM membership;      -- this tenant only
COMMIT;
SELECT count(*) FROM membership;      -- 0: no context outside the transaction
```

## Authentication and sessions (F02)

Settings (environment; see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_IDLE_MINUTES` | 60 | A session unused for this long is ended. |
| `SESSION_ABSOLUTE_HOURS` | 12 | A session ends this long after login regardless of use. |
| `SESSION_COOKIE_SECURE` | true | `Secure` flag on the cookie. Keep `true`; Chrome and Firefox accept Secure cookies on `http://localhost`. |
| `SESSION_COOKIE_NAME` | sid | Cookie name. |
| `LOGIN_LOCKOUT_ATTEMPTS` / `LOGIN_LOCKOUT_MINUTES` | 5 / 15 | Failures inside the window lock the account for the window. |
| `PASSWORD_RESET_TTL_HOURS` | 24 | Lifetime of a one-time reset link. |
| `APP_BASE_URL` | http://localhost:5173 | Prefix of reset links. |
| `CRYPTO_KEYS`, `CRYPTO_ACTIVE_KEY_ID` | (none) | Encryption key ring; see below. |

Every `POST`/`PUT`/`PATCH`/`DELETE` under `/api/` must carry the header
`X-Requested-With` (any value). The React client sends it; a script must too:

```sh
curl -c jar -b jar -H 'X-Requested-With: cli' -H 'Content-Type: application/json' \
  -d '{"email":"you@firm.test","password":"…"}' http://localhost:8000/api/auth/login
```

### Bootstrap the first `firm_admin`

There is no signup. On a fresh database, from `backend/` with `.env` in place
(`DATABASE_URL` is the `app_rw` role; migrations already applied):

```sh
printf '%s\n' 'a-long-passphrase-here' > /tmp/pw && chmod 600 /tmp/pw
.venv/bin/python scripts/create_user.py --email you@firm.test --display-name "Your Name" \
    --create-tenant rye-beach:"Rye Beach Landscaping" --firm-name "Your CPA Practice" \
    --membership rye-beach:firm_admin --password-stdin < /tmp/pw
rm /tmp/pw
```

`--create-tenant` creates the firm (if none exists) and the tenant (if the slug is
new). Firm authority is derived from memberships (see the F02 plan in the feature
brief), so the first admin needs at least one `firm_admin` membership. At first
login the admin is asked to enrol TOTP (QR code or manual key) and is shown ten
recovery codes once. Every step writes to `firm_audit_log` with `detail.via = "cli"`.

Further users are created by a `firm_admin` in the app (`POST /api/admin/users`),
or with the same script without `--create-tenant`.

### Password reset

1. Normal path: a `firm_admin` calls `POST /api/admin/users/{user_id}/password-reset`
   (or creates the user without a password). The response contains a one-time link
   (`APP_BASE_URL/reset-password?token=…`, valid `PASSWORD_RESET_TTL_HOURS`). Hand it
   to the user out of band; **no e-mail is sent in F02**. Issuing the link ends the
   user's sessions; the old password keeps working until the link is used. Audit:
   `password_reset_issued`, then `password_changed` (`via: reset_link`).
2. Last resort (a `firm_admin` locked out): `scripts/create_user.py --email … --reset-password`
   sets the password directly and ends all sessions. Audit: `password_changed` (`via: cli`).

A user changes their own password with `POST /api/auth/password/change`.

### TOTP reset

1. Normal path: `POST /api/admin/users/{user_id}/totp-reset` by a `firm_admin`.
   Clears the secret and recovery codes, ends the user's sessions; the user enrols
   again at next login. Audit: `totp_reset` (`via: admin`).
2. Last resort: `scripts/create_user.py --email … --reset-totp`. Audit: `totp_reset` (`via: cli`).

If a user has lost the authenticator but still has recovery codes, they can sign in
with one (`POST /api/auth/totp/recover`; each code works once, audited as
`recovery_code_used`) and then ask for a reset.

### Lockout

Five failed password or code attempts inside 15 minutes lock the account for 15
minutes (`account_locked` in `firm_audit_log`). The lock expires on its own. To lift
it early, as `app_owner`:

```sql
UPDATE "user" SET locked_until = NULL, failed_login_count = 0 WHERE email = '…';
```

### Encryption key rotation

`CRYPTO_KEYS` is a comma-separated ring `key_id:base64key` of 32-byte keys;
`CRYPTO_ACTIVE_KEY_ID` names the key that encrypts new values. Every key in the
ring can decrypt, and each ciphertext carries its key id. F02 encrypts TOTP secrets;
F03 will use the same ring for OAuth tokens.

1. Generate: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
2. Add the new key to `CRYPTO_KEYS` (keep the old one) and point
   `CRYPTO_ACTIVE_KEY_ID` at it. Restart the API. New enrolments use the new key;
   existing rows still decrypt with the old key.
3. Re-encrypt existing rows under the new key (a maintenance task; for TOTP secrets
   in F02 the practical route is a TOTP reset per user, or wait for the re-encrypt
   command that F03 adds with token storage).
4. Only when no row references the old key id
   (`SELECT count(*) FROM "user" WHERE totp_key_id = 'old'`), remove it from the ring.
   Removing it earlier makes those rows unreadable (the app raises a clear error,
   it does not silently fail).

### Reading the audit tables

- `GET /api/firm-audit` (firm roles): logins, lockouts, TOTP and password events,
  user creation. Keyed by `firm_id`; rows with no derivable firm (unknown e-mail) have
  `firm_id = NULL`.
- `GET /api/audit` (firm roles and `client_admin`; active tenant): `tenant_enter` and
  membership changes for that tenant.
- Both tables are insert-only by trigger (D-13): `UPDATE`, `DELETE`, `TRUNCATE`
  fail for every role, including `app_owner`. There is no supported way to edit
  them; a wrong row is corrected by a later row.
