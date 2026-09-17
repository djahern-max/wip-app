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

## Authentication and sessions (F02, F02.1)

Settings (environment; see `.env.example`). **Required, no default**: `DATABASE_URL`,
`CRYPTO_KEYS`, `CRYPTO_ACTIVE_KEY_ID`; the API exits at start-up naming the missing
variable (never its value). `DATABASE_OWNER_URL` is required only where Alembic runs,
and it is all Alembic reads: the migration process needs neither `DATABASE_URL` nor the
encryption keys, so do not give them to it. `ENV_FILE` points at the env file (default:
repo-root `.env`). The test suite reads no env file and generates its own throwaway key
ring per run; it needs only `TEST_DATABASE_URL` / `TEST_DATABASE_OWNER_URL` (defaults
match `db/init/01_roles.sh`).

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_IDLE_MINUTES` | 60 | A session unused for this long is ended. |
| `SESSION_ABSOLUTE_HOURS` | 12 | A session ends this long after login regardless of use. |
| `SESSION_COOKIE_SECURE` | true | `Secure` flag on the cookie. Keep `true`; Chrome and Firefox accept Secure cookies on `http://localhost`. |
| `SESSION_COOKIE_NAME` | sid | Cookie name. |
| `SESSION_TOUCH_SECONDS` | 60 | `last_seen_at` is written (in its own short transaction) only when at least this stale. |
| `ENROL_SESSION_TTL_MINUTES` | 15 | Lifetime of the enrolment-only session a firm user's activation link opens (D-16). |
| `LOGIN_LOCKOUT_ATTEMPTS` / `LOGIN_LOCKOUT_MINUTES` | 5 / 15 | Failures inside the window lock the **account** for the window. |
| `IP_THROTTLE_FAILURES` / `IP_THROTTLE_MINUTES` | 20 / 15 | Failures from one **IP** inside the window return 429 for the rest of it. |
| `TRUSTED_PROXY_COUNT` | 0 | Reverse proxies in front of the API (below). |
| `ACTIVATION_LINK_TTL_HOURS` | 72 | Lifetime of a one-time activation link. |
| `APP_BASE_URL` | http://localhost:5173 | Prefix of activation links, and the only `Origin` allowed on state-changing browser requests. |
| `CRYPTO_KEYS`, `CRYPTO_ACTIVE_KEY_ID` | (none) | Encryption key ring; see below. |

Every `POST`/`PUT`/`PATCH`/`DELETE` under `/api/` must carry the header
`X-Requested-With` (any value), and a browser request must carry an `Origin` equal to
`APP_BASE_URL`; a request without `Origin` (curl, the CLI) passes. Preflights and
foreign-origin requests get 403 with no `Access-Control-*` headers. A script:

```sh
curl -c jar -b jar -H 'X-Requested-With: cli' -H 'Content-Type: application/json' \
  -d '{"email":"you@firm.test","password":"…"}' http://localhost:8000/api/auth/login
```

### Client IP and `TRUSTED_PROXY_COUNT`

The IP written to audit rows and used for the throttle is the socket address unless
`TRUSTED_PROXY_COUNT` is N > 0, in which case it is the N-th address from the right
of `X-Forwarded-For` (the address the outermost trusted proxy saw). With the header
missing or shorter than N entries, the socket address is used. Set N to the number of
proxies you control in front of the API and no more: with N = 0 a forged
`X-Forwarded-For` has no effect; with N too high a client can choose its own bucket.

### Bootstrap the practice (no tenant needed)

There is no signup and the CLI never sets a password: every account is activated
through a one-time link (D-16). From `backend/` with `.env` in place (`DATABASE_URL`
is the `app_rw` role; migrations already applied):

```sh
.venv/bin/python scripts/create_user.py bootstrap --firm-name "Your CPA Practice" \
    --email you@firm.test --display-name "Your Name"
```

This creates the firm, the user, and their `firm_admin` row in `firm_membership`
(D-15), and prints the activation link **once, to stdout** (it is never logged).
Open the link: set the password, scan the QR code (or type the key), confirm a
code, save the ten recovery codes. Only then is the account usable. Signed in, the
admin creates the first tenant and their own entry row:

```sh
# as the signed-in firm_admin (cookie jar from the login above)
curl -b jar -H 'X-Requested-With: cli' -H 'Content-Type: application/json' \
  -d '{"name":"Rye Beach Landscaping","slug":"rye-beach"}' http://localhost:8000/api/admin/tenants
curl -b jar -H 'X-Requested-With: cli' -H 'Content-Type: application/json' \
  -d '{"user_id":"<your user id>"}' http://localhost:8000/api/admin/tenants/<tenant id>/memberships
```

`POST /api/admin/tenants` creates the bare tenant row in the caller's firm (the firm
never comes from the request); tenant configuration is F04. A membership request
with no `role` writes an **entry row** for a firm user; a client user needs a client
role. Every step writes to `firm_audit_log` or the tenant's `audit_log`.

### Users, entry rows, firm memberships

- Create a user: `POST /api/admin/users {email, display_name}` → the response carries
  `activation_url`; or `scripts/create_user.py create-user --email … --display-name …`
  with `--firm-role firm_staff --entry <slug>…` for a firm user, or
  `--membership <slug>:client_pm` for a client user.
- Firm roles: `GET/POST /api/admin/firm-memberships`, `PUT/DELETE
  /api/admin/firm-memberships/{user_id}`. A user holds either a firm role (and entry
  rows) or client roles, never both in one firm; the API refuses the mix. The last
  `firm_admin` cannot be removed or demoted. Any change to a firm membership ends the
  user's sessions.
- Removing a firm membership commits first (row, audit, sessions), then removes the
  user's entry rows one tenant per transaction. If that is interrupted, the user
  already has no access anywhere (an entry row without a firm membership grants
  nothing); repeat the `DELETE` to finish the cleanup.

### Activation links (password reset, first login)

1. A `firm_admin` calls `POST /api/admin/users/{user_id}/activation-link` (or the CLI:
   `scripts/create_user.py issue-link --email …`). The response is a one-time link,
   `APP_BASE_URL/activate#token=…`, valid `ACTIVATION_LINK_TTL_HOURS`. The token is in the
   URL fragment, which browsers never send to a server, so it stays out of access logs;
   the page posts it in the request body and clears the address bar. Hand it over
   directly; **no e-mail is sent**. Issuing a link ends the user's sessions and voids
   any earlier unredeemed link; the old password keeps working until the link is used.
2. Redeeming sets the password. For a firm user without TOTP it opens a 15-minute
   enrolment-only session in which the app enrols TOTP and shows the recovery codes;
   the account is unusable until that completes (a password login for a firm user
   without TOTP is the generic failure). If the flow is abandoned, issue a new link.
3. Audit: `activation_link_issued`, then `password_changed` (`via: activation_link`).
   A failed redemption is a `login_failure` (step `link`) with no firm and never the
   token.

A user changes their own password with `POST /api/auth/password/change`; this ends
every session, the caller's included.

### TOTP reset

`POST /api/admin/users/{user_id}/totp-reset` (or `scripts/create_user.py reset-totp
--email …`) clears the secret and recovery codes, ends the user's sessions, and
returns a new activation link that enrols TOTP again. Audit: `totp_reset`, then
`activation_link_issued`. A user who still has recovery codes can sign in with one
(`POST /api/auth/totp/recover`, each once, audited as `recovery_code_used`) and ask
for a reset.

### Lockout and throttle

Five failed password or code attempts inside 15 minutes lock the account for 15
minutes (`account_locked`). Twenty failures from one IP inside 15 minutes throttle
the IP for the rest of the window: one `ip_throttled` row is written and further
attempts from that IP get 429 and write nothing. Both expire on their own. To lift an
account lock early, as `app_owner`:

```sql
UPDATE "user" SET locked_until = NULL, failed_login_count = 0 WHERE email = '…';
```

An IP throttle cannot be lifted early without deleting audit rows, which the tables
refuse; wait for the window.

### Owner browser pass (closes the F02 and F02.1 frontend criteria)

With `make api` and `npm run dev` running: `scripts/create_user.py create-user
--email … --display-name … --firm-role firm_staff --entry rye-beach` → open the printed
link in the browser → set the password → scan the QR code → confirm → save the
recovery codes → sign in with password and code → switch tenants → sign out. To
repeat with an existing user: `scripts/create_user.py issue-link --email …`.

### Encryption key rotation

`CRYPTO_KEYS` is a comma-separated ring `key_id:base64key` of 32-byte keys;
`CRYPTO_ACTIVE_KEY_ID` names the key that encrypts new values. Every key in the
ring can decrypt, and each ciphertext carries its key id. F02 encrypts TOTP secrets;
F03 will use the same ring for OAuth tokens. No key material is ever committed:
`.env.example` holds placeholders and CI fails if `.env` is tracked.

1. Generate: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
2. Add the new key to `CRYPTO_KEYS` (keep the old one) and point
   `CRYPTO_ACTIVE_KEY_ID` at it. Restart the API. New enrolments use the new key;
   existing rows still decrypt with the old key.
3. Re-encrypt existing rows under the new key (a maintenance task; for TOTP secrets
   the practical route is a TOTP reset per user, or wait for the re-encrypt
   command that F03 adds with token storage).
4. Only when no row references the old key id
   (`SELECT count(*) FROM "user" WHERE totp_key_id = 'old'`), remove it from the ring.
   Removing it earlier makes those rows unreadable (the app raises a clear error,
   it does not silently fail).

### Reading the audit tables

- `GET /api/firm-audit` (firm roles): logins, lockouts, throttles, TOTP and password
  events, user and tenant creation, firm membership changes. Keyed by `firm_id`; rows
  with no firm (unknown e-mail, failed link redemption, IP throttle) are shown to
  `firm_admin` only. An unknown e-mail is stored as `email_sha256`, never in clear.
- `GET /api/audit` (firm roles and `client_admin`; active tenant): `tenant_enter` and
  membership changes for that tenant (entry rows appear with `role: entry`).
- Both tables are insert-only by trigger (D-13): `UPDATE`, `DELETE`, `TRUNCATE`
  fail for every role, including `app_owner`. There is no supported way to edit
  them; a wrong row is corrected by a later row.
- Database errors are logged as SQLSTATE plus the server's primary message only; the
  engine hides bound parameters, so no row value reaches a log or an error body.
