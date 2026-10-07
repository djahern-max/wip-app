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
APP_OWNER_PASSWORD=… APP_RW_PASSWORD=… APP_DATABASES=wip PG_MAINTENANCE_DB=defaultdb \
PGSSLMODE=require PGHOST=… PGPORT=25060 POSTGRES_USER=doadmin PGPASSWORD=… db/init/01_roles.sh
```

`PG_MAINTENANCE_DB` is the database the script connects to for the role and `CREATE
DATABASE` statements: DigitalOcean Managed Postgres has no `postgres` database, only
`defaultdb`; Docker and CI keep the default `postgres`.

Then set `DATABASE_OWNER_URL` (app_owner) for the migration step and
`DATABASE_URL` (app_rw) for the API and worker. For jobcost.dev the exact command, the
`sslmode`, and the Postgres 16 `SET ROLE` note are under "Production (jobcost.dev)".
Verify the application role:

```sql
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('app_owner','app_rw');
-- both must be f / f
```

## Migrations

- Apply: `cd backend && alembic upgrade head` with `DATABASE_OWNER_URL` set.
- Roll back one: `alembic downgrade -1`. Every migration is reversible or says why
  not in its docstring.
- **Downgrades are destructive and guarded** (F03 close-out). `alembic downgrade`
  (and `make downgrade`) runs only when `ALLOW_DESTRUCTIVE_DOWNGRADE=1` is set **and**
  the target database name is not in `PROTECTED_DATABASE_NAMES` (comma-separated;
  default `wip`, the local dev database; set it to the production database name in
  production). Otherwise it exits non-zero naming the database, before connecting,
  and changes nothing. Migration round-trip checks (`upgrade head → downgrade base →
  upgrade head`) run only against a scratch database: the test suite's
  `scratch_db_url` (`wip_mig_*`) and `wip_test`, and CI's `wip_scratch`. Never against
  `wip` or production. To check by hand: `createdb`-style scratch via
  `db/init/01_roles.sh` with `APP_DATABASES=wip_scratch`, then
  `ALLOW_DESTRUCTIVE_DOWNGRADE=1 DATABASE_OWNER_URL=…/wip_scratch alembic downgrade base`.
- **What the guard does not cover.** The append-only triggers (D-13) stop `UPDATE`,
  `DELETE` and `TRUNCATE` for every role, but they do not protect against `DROP TABLE`
  (or a downgrade that drops a table) by the schema owner, `app_owner`. The protection
  against that is backups (F23), not a trigger.
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

### Rebuild local dev data

After a wipe (an empty `wip` database at `alembic upgrade head`), from `backend/` with
`.env` in place and `make api` **not** required. Every command goes through the same
audited admin service as the API (actor `cli`); none sets a password (D-16): each
user gets a one-time activation link printed once, to open in the browser.

```sh
# 0. schema
.venv/bin/alembic upgrade head

# 1. the firm and its first firm_admin (prints the admin's activation link)
.venv/bin/python scripts/create_user.py bootstrap --firm-name "Your CPA Practice" \
    --email you@firm.test --display-name "Your Name"

# 2. the client tenant
.venv/bin/python scripts/create_user.py create-tenant --name "Rye Beach Landscaping" --slug rye-beach

# 3. the admin's own entry row in that tenant (D-15)
.venv/bin/python scripts/create_user.py add-entry --email you@firm.test --tenant rye-beach

# 4. a firm_staff user with an entry row in rye-beach (prints their activation link)
.venv/bin/python scripts/create_user.py create-user --email dane@firm.test --display-name "Dane" \
    --firm-role firm_staff --entry rye-beach
```

Then open each link (`make web` running): password → QR code → confirm → recovery
codes. A lost link: `scripts/create_user.py issue-link --email …`. The local object
store (`.object-store/`) is separate from the database; it can be emptied at any time,
and a batch whose object is missing will report `object not found` on download.

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
repeat with an existing user: `scripts/create_user.py issue-link --email …`. Reloading
the enrolment page shows the same QR code and key (the pending secret is reused until
it is confirmed); a new link always starts from a new secret.

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
3. Re-encrypt existing rows under the new key: `make reencrypt` (F03;
   `backend/scripts/reencrypt.py`, `--dry-run` to count only). It covers
   `user.totp_secret_enc` and the token columns of `connection`, one transaction per
   tenant for the tenant table, prints counts by key id before and after and never a
   value, and is idempotent (a second run reports 0 rows). It exits 1, leaving the
   rows as they are, if any blob cannot be decrypted (the key that sealed it is not
   in the ring): add that key and run again. Run it with the API and worker
   environment (`DATABASE_URL`, both keys in `CRYPTO_KEYS`, the new key active).
4. Only when the "after" counts show zero rows on the old key id, remove it from
   the ring and restart the API and the worker. Removing it earlier makes those rows
   unreadable (the app raises a clear error, it does not silently fail).

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

## Ingestion: worker, object store, imports (F03)

### Running the worker

`make worker` (`python -m app.worker`) runs one worker process with the same
environment as the API (`DATABASE_URL`, `CRYPTO_KEYS`, `CRYPTO_ACTIVE_KEY_ID`,
`OBJECT_STORE` and its settings). It exits at start-up naming any missing variable.
Run one or more; they share the queue safely (`FOR UPDATE SKIP LOCKED`). Stop with
SIGTERM or Ctrl-C; a task in flight finishes, then the loop ends.

The worker reads the tenant list, and **for each tenant** opens one transaction
with that tenant's context and claims at most one due task (D-19). `LISTEN` on the
`wip_tasks` channel wakes it as soon as the API enqueues something; polling every
`WORKER_POLL_SECONDS` (default 5) is the fallback. Log lines carry
`task=… kind=… tenant=… attempt=n/m` and the outcome; never a payload, a filename or
a token.

| Variable | Default | Meaning |
|---|---|---|
| `WORKER_POLL_SECONDS` | 5 | Poll interval when no `NOTIFY` arrives. |
| `WORKER_LEASE_SECONDS` | 300 | A claimed task is leased for this long. No heartbeat: a task must finish inside its lease or split its work. The worker logs a warning when a task runs past 80% of the lease. |
| `TASK_MAX_ATTEMPTS` | 5 | After this many failed attempts the task is `failed`. |
| `TASK_BACKOFF_BASE_SECONDS` / `TASK_BACKOFF_CAP_SECONDS` | 30 / 900 | Delay before the retry after attempt *n*: `base × 2^(n−1)`, capped (30 s, 1, 2, 4 min, …). |

### What a stuck or failed task looks like, and how to requeue it

`task` is a tenant table: read it with tenant context, as the application does.

```sql
BEGIN;
SELECT set_config('app.tenant_id', '<tenant uuid>', true);
SELECT id, kind, status, attempts, max_attempts, run_after, locked_by, locked_until, last_error
FROM task WHERE status IN ('queued', 'running', 'failed') ORDER BY created_at DESC;
COMMIT;
```

- **`running` with `locked_until` in the past**: the worker died or the task outlived
  its lease. Nothing to do: the next pass reclaims it (`attempts` increments), or
  marks it `failed` if `attempts` already equals `max_attempts`. A worker that finishes
  a task after losing its lease writes nothing and logs `outcome=lease lost`.
- **`queued` with `run_after` in the future**: a retry waiting out its backoff.
  `last_error` is the exception type (or SQLSTATE and message for a database error),
  never file content.
- **`failed`**: `attempts = max_attempts`, an expired lease at the limit, or a
  permanent failure (no retry). For `import.process_batch` the two cases look
  different: a **parse error** (`last_error` = `parse failed: <ExceptionType>`,
  `attempts = 1`) marks the batch `failed`, and a failed batch is final; it never
  goes back to `processing`, so fix the file and upload it as a new batch (a
  requeued task for a failed batch fails again with `batch already failed`). A
  **transient error** (opening the object, talking to the database; `last_error`
  is the exception type or SQLSTATE, `import_batch.error` ends `(will retry)`) leaves
  the batch `received` and retries with backoff; if it still reaches
  `max_attempts`, fix the cause (object store, database) and requeue:

```sql
BEGIN;
SELECT set_config('app.tenant_id', '<tenant uuid>', true);
UPDATE task SET status = 'queued', attempts = 0, run_after = now(),
       locked_by = NULL, locked_until = NULL, last_error = NULL
WHERE id = '<task uuid>' AND status = 'failed';
SELECT pg_notify('wip_tasks', '<tenant uuid>');
COMMIT;
```

- **An import batch that stays at Processing** (`import_batch.status = 'processing'`
  while its task is `queued` or `failed`, not `running`): since 2026-09-19 no
  exception in `import.process_batch` leaves a batch there; it returns to `received`
  with `error` ending `(will retry)`. A batch can still be left at `processing` by a
  worker that was killed mid-task (the lease expires and the next attempt picks it
  up) or by a database that was unreachable when the status was written back. If the
  task's `last_error` is `LookupError`, the worker process does not know the batch's
  source kind: the module is missing from `SOURCE_KIND_MODULES`
  (`app/integrations/base.py`) or the worker is running older code than the API.
  Restart the worker (it does not reload code; `make api` does), then requeue the
  task as below. The task's `dedupe_key` is the batch id. The next run sets the
  batch's status itself; never edit `import_batch.status` by hand.

Re-processing an import batch is safe: identical payloads write nothing (D-20).
The partial unique index on `(tenant_id, kind, dedupe_key)` covers only `queued`
and `running` rows, so requeueing a `failed` row never conflicts.

### Object store

`OBJECT_STORE=local` (development, tests, CI) keeps objects under
`LOCAL_OBJECT_STORE_DIR` (default: repo-root `.object-store`, gitignored; CI fails
if anything under it is tracked). `OBJECT_STORE=s3` is DigitalOcean Spaces through
`boto3`: `SPACES_ENDPOINT_URL`, `SPACES_REGION`, `SPACES_BUCKET`,
`SPACES_ACCESS_KEY_ID`, `SPACES_SECRET_ACCESS_KEY` have no defaults and the process
exits naming a missing one. Objects are private; every key starts
`tenant/{tenant_id}/` and the store builds that prefix itself. Downloads from S3 are
a redirect to a signed URL valid for `SIGNED_URL_TTL_SECONDS` (default 60); the local
store streams the file.

Before the first deployment (D-21): create the Space and an access key, set the
five variables, then upload one file through the Imports page and download it, and
record the date and the bucket name here.

First manual upload/download against Spaces (D-21): **done 2026-09-22**, bucket
`jobcost-files` (NYC3), one file uploaded through Imports and downloaded by signed
URL. The same check found that the chart-of-accounts parse failed against Spaces
(`UnsupportedOperation`: boto3's body is not seekable); `S3ObjectStore.open` now
spools the object to a temporary file so every source kind reads a seekable stream,
as it does from the local store.

### Imports

- `POST /api/imports` (multipart: `source_kind`, `file`) for `firm_admin`,
  `firm_staff`, `client_admin` in the active tenant. Files over `MAX_UPLOAD_BYTES`
  (default 25 MB) and extensions the source kind does not accept are refused before
  anything is stored. The object is written before the `import_batch` row commits
  (raw before normalized); the row, the `import.process_batch` task and the
  `import_uploaded` audit row land in one transaction.
- The same bytes for the same tenant and source kind return the existing batch with
  `duplicate: true` (HTTP 200, audit `import_duplicate`); no second row, object or task.
- `GET /api/imports`, `GET /api/imports/{id}`, `GET /api/imports/{id}/download`
  (audit `import_downloaded`). A batch of another tenant is 404.
- Statuses: `received` → `processing` → `loaded` | `loaded_with_issues`
  (`rows_rejected` > 0; the rest loaded) | `nothing_loaded` ("Nothing loaded": rows
  were found and none could be read; an end state, not a failure, no follow-on; fix
  the file and upload it as a new batch) | `failed` (`parse` raised; final, no retry;
  upload the corrected file as a new batch). A transient failure puts the batch back
  to `received` with `error` ending `(will retry)` while the task backs off. F03 ships one production source kind,
  `unparsed_file`, which stores and checksums the file and yields no records; LMN
  and isolved parsers register their kinds in F06/F11.
- Source data lives in `raw_record` (insert-only, versioned per external id, D-20)
  and in the object store; never in an audit row or a task payload.
- What a person sees vs. what you read (D-22, F03.1): the API's `status_label`,
  `source_label` and `message` are the words on screen; `status`, `source_kind` and
  `error_detail` (= the `import_batch.error` column: exception type, `(will retry)`)
  are the machine values for this runbook. Upload refusals show one plain sentence;
  the machine reason (limit name, exception type) is logged as
  `upload refused request_id=… : …`, and the request id is the response's
  `X-Request-Id` header.

## New client set-up (F07.3)

Home shows this list for the active company, computed each time it is read, with "Done"
or "Not done" on each line, one sentence, and a link to the action that completes it;
the first not-done line with an action is the page's one primary action. Nothing is
stored: a step is done when the data says so. The order below is the order on Home
(owner, 2026-10-04: Policy before Burden rates). Before the list, the tenant itself:
`scripts/create_user.py create-tenant --name … --slug …`, then `add-entry` for the firm
admin and `create-user --entry <slug>` for each firm_staff user (see "Rebuild local dev
data" above for the exact commands).

| # | Step | Done when | Not done: what Home says and where it sends you |
|---|---|---|---|
| 1 | QuickBooks | A connection is `connected` and a backfill has succeeded (no `backfill_needed` or `needs_reconnect` attention). | "Connect to QuickBooks." → Connections (`POST /api/qbo/connect`, firm_admin). After a reconnect or a stale copy: the Connections sentence, "Run a backfill on Connections." Shown to roles that can view connections. |
| 2 | Suggestion rules | At least one active `account_suggest_rule`. | "No suggestion rules are loaded…": the firm loads the client's rules file with `scripts/load_suggest_rules.py` or `PUT /api/config/suggest-rules` ("Editing suggestion rules" below). No link: there is no screen. |
| 3 | Chart of accounts | Active `gl_account` rows exist. | "Upload the chart of accounts." → Imports ("Loading a chart of accounts" below). |
| 4 | Account mapping | No active account lacks a confirmed mapping (the Accounts page's unmapped count). | "n of m accounts to confirm; k have a suggestion." → Configuration, Accounts: "Confirm all suggestions", then map the rest by hand. A chart loaded before the rules leaves every account unmapped with no suggestion; load the rules and "Re-run suggestions". |
| 5 | Policy | `timezone`, `wip_basis`, `deposit_identification` and `fuel_surcharge_treatment` decided (the keys a feature reads today; `REQUIRED_POLICY_KEYS` in `app/domain/home/checklist.py`, extended by each feature that starts reading a key; F08 added the two item keys). | "n of 4 policy keys needed now are not decided: …" → Configuration, Policy (firm_admin): choose the time zone from the list (Eastern, Central, Mountain, Arizona, Pacific, Alaska, Hawaii); tick the cost categories in the WIP basis; tick the deposit items and the fuel surcharge items with the rate, or record that the company charges no fuel surcharge (F08; the items must exist in QuickBooks and a backfill must have run, "The Sold Jobs Board" below); the reference is optional (F04.1). Two keys (`small_job_threshold`, `fiscal_year_start_month`) wait for their features. Done reads with a note of the keys that wait for their features. |
| 6 | Burden rates | Every active division with a cost-code digit has a rate in force on the company's today (a tenant-wide row counts for all). Done with a note when Labor Burden is not in a decided WIP basis. | While the basis is undecided: "Burden rates are set after the WIP basis is decided." → Policy. Else: "No burden rate in force today for EX, SNOW." → Configuration, Burden rates ("Burden rates" below). |

Below the list Home shows the jobs: "n sold estimates to review" (→ Review sold
estimates) or "Upload an estimate" when the company has none; one line per job that is
not closed or cancelled with its status and the first thing it needs (work areas to
confirm → the job; needs its QuickBooks project → the job; a Sold job with no link is
backlog, D-35, and needs nothing); and the tracked QuickBooks rows with no job (D-37,
→ Customers). Client roles see the jobs part with links only to pages they can open and
no checklist. The order of job needs is `JOB_NEEDS` in `app/domain/home/checklist.py`;
F08 onward append to it.

## Tenant configuration (F04)

### Loading a chart of accounts

1. Load the tenant's suggestion rules first (once; below). Without rules a chart
   loads with every account unmapped and no suggestions.
2. Imports page → Source "Chart of accounts" → choose the `.csv` (columns
   `account_no,account_name,ledger_type`) or `.xlsx` (the owner's workbook layout:
   title row, section headings, a legend at the bottom; those rows are skipped one by
   one and counted as rejected) → Upload file. Supported layout: the chart is the
   **first sheet** of a workbook (other sheets are not read); account number, account
   name and ledger type are three neighbouring columns in that order, starting in any
   column (the owner's workbook keeps column A empty). The starting column is taken
   from the header row ("No.", "Account", "Account number", …, with at least two
   filled cells) or, without a header, from the first row that starts with an account
   number. Account numbers may be number cells or text; merged cells are not needed
   and a number and a name combined in one cell are not read. The Source stays as
   chosen for the company until the browser is closed. The batch reaches "Loaded. Updating
   accounts…" then "Loaded. Accounts updated." after a Refresh. The follow-on task is
   `config.normalize_chart` (`task.dedupe_key = config.normalize_chart:<batch id>`);
   "Loaded, but the accounts could not be updated." means it failed: the machine
   detail is in the batch's `error_detail` and the task's `last_error` (requeue as in
   the worker runbook).
   **"Nothing loaded"** with "No accounts could be read from this file.": nothing
   was changed. It is an end state, not a failure: the task succeeded and is not
   retried, the batch never starts the follow-on, and the normalizer never marks
   accounts inactive from a file that holds no account. What to do next: check the
   Source, the sheet order and the layout above, fix the file, and upload it as a new
   batch (the same bytes are one batch and are not processed again; there is nothing
   to requeue). A batch that ended this way before 2026-09-20 shows "Loaded with
   issues" with 0 rows loaded and the same sentence; it is left as it is.
   **"Not applied"** with "Accounts not updated, because a newer chart of accounts was
   uploaded after this file.": only the tenant's newest chart batch (by upload time) is
   applied. An older batch processed late (a retry that waited, a requeued task) is
   stored and counted but changes no account, mapping or suggestion
   (`import_batch.followup_outcome = superseded`). Nothing to do.
3. Configuration → Accounts: the unmapped count is at the top. Correct a suggestion
   with Edit (Save, or Save and confirm), confirm one row with Confirm, or "Confirm all
   suggestions" (a confirmation step follows; one audit row per account). Only
   **confirmed** mappings are used by later features.
4. A revised chart (same page, new file): renamed accounts keep their mapping, new
   accounts get a suggestion, accounts missing from the newer file are marked inactive
   (never deleted; raw history keeps every version). The same file uploaded again is one
   batch and changes nothing.

Command line, from `backend/` with `.env`:

```sh
.venv/bin/python scripts/load_suggest_rules.py --tenant rye-beach \
    --file tests/fixtures/rye_beach/account_suggest_rules.json --suggest
```

### Editing suggestion rules

Rules are data (`account_suggest_rule`), ordered, first match wins. A rule is a
regular expression over the whole account-number string (any length), plus what it
sets: a division (explicit code, or `division_from_digit` = a 1-based position in the
number looked up in `division.code_digit`), a cost category (explicit name, or
`cost_category_from_slot` = the last two characters as the slot), and `in_job_cost`.
A derivation that finds nothing means the rule does not match and the next is tried;
no match at all means **no suggestion**, and the account is listed as unmapped. Rye
Beach's rules are `backend/tests/fixtures/rye_beach/account_suggest_rules.json` (the
JSON explains each rule). To change them: edit a copy of the JSON, load it with the
script above (it replaces the tenant's rules; an invalid pattern or one over 100
characters refuses the whole load naming the rule; the load is audited
`suggest_rules_loaded` with before and after), then re-run suggestions from the
Accounts page ("Re-run suggestions") or with `--suggest`. Suggestions never change a
confirmed mapping. `PUT /api/config/suggest-rules` takes the same JSON.

### Setting a policy key

Configuration → Policy (firm_admin only): each key shows its value or "Not decided",
who decided, when, and the reference. Decide or Change → value, and a reference if
there is one (optional since F04.1: where the decision is written down, e.g. D-04 or
the engagement letter) → Record decision. Who and when are always recorded. Keys set
today: `timezone` (a list of the United States zones in plain words; a zone off the
list set through the API is shown by its name and kept), `fiscal_year_start_month`,
`wip_basis` (tick the cost categories counted in both cost to date and EAC; nothing is
pre-ticked), `deposit_identification` (F08, D-02: tick every QuickBooks item a deposit
invoice is written on, at least one; the list is the company's items as the sync holds
them, inactive ones marked) and `fuel_surcharge_treatment` (F08, D-39: tick every
fuel surcharge item and give the rate as a fraction, 0.0500 for 5.00%; the rate may be
left for later, F08 does not read it and F08.1 requires it; or tick "This company
charges no fuel surcharge", which is a decision, not a default). An item id the company
does not hold is refused in words. Keys that wait for their features show why in place
of "Decide" and refuse a PUT with the same sentence (409): `small_job_threshold` (D-08
open; the WIP schedule). A value already stored on a waiting key is still shown with
who and when. The feature that first reads a key removes the sentence (`waiting` in
`POLICY_KEYS`) and gives the key its control. **No key has a default**: a feature that
needs an undecided key stops with a message naming it rather than assuming a value.
Every change is audited `policy_set` with before and after; a blank reference is
stored as the empty string.

### Divisions, cost categories, cost codes, burden rates

- Divisions: Configuration → Divisions (firm roles). The code digit is unique per
  tenant. Deactivating keeps the row and its mappings.
- Cost categories: the D-23 list is seeded per tenant the first time configuration is
  read (audited `cost_categories_seeded`); rename or deactivate only. Adding one needs a
  new decision.
- Cost codes: read-only grid; "no account" = nothing mapped to that division × category.
- Burden rates: a fraction (0.3250 = 32.50%), from a date, optionally to an exclusive
  end date, per division or whole company; overlapping periods for the same division
  are refused; the rate in force on a date is the division's own, else the whole-company
  rate. See "Burden rates (F06.1, D-05, D-34)" below.
- Every list here (Accounts, Divisions, Cost categories, Burden rates) hides inactive
  rows by default. "Show inactive (n)" above the table shows them; the choice lasts
  while the page is open and is not stored. Nothing is deleted: Deactivate keeps the
  row and it drops out of the default view.

### Burden rates (F06.1, D-05, D-34)

What a row is: the labor burden rate for one division (or the whole company), as a
fraction of wages with four decimals (0.1959 = 19.59%), in force from a date and, if
given, up to an exclusive end date. It covers employer FICA, FUTA, state unemployment and
payroll surcharges, workers' compensation at each employee's assigned class, and the
employer share of health, dental, vision, life and disability (D-05). The controller
works it out on the burden worksheet and names the worksheet in the Basis field. Rye
Beach from 2026-01-01: LS 0.2136, EX 0.1959, GC 0.2207, SNOW 0.2061 (the SNOW row's
Basis also carries the contingent 0.1713 at class 9402, used nowhere; D-34).

Entering one: Configuration → Burden rates (firm roles), division, from date, rate as a
fraction. A change (a new worksheet, an audit, a renewal, a benefit bill) is a **new
row from its date, never an edit**. Rows cannot be edited, and two active rows for one
division may not overlap, so to change a rate from a date:

1. Deactivate the row in force.
2. Add it again with the same rate, its original from date and an end date equal to the
   new row's from date (the end date is exclusive).
3. Add the new row from that date.

Estimates dated before the change keep the old rate; later ones get the new rate. When an
audit changes a class or refunds premium, the same steps apply from the settlement date
(D-34).

What the estimate shows: on the Estimates detail, a Burden column per work area and
two cost columns, As estimated (the cost lines exactly as loaded; they tie to the
estimating system) and With burden (Labor Burden computed from the rate). EAC in the
WIP basis is the burdened figure, with the as-estimated EAC beside it in small text.
Burden is computed each time the estimate is read and never stored. The rate is read on
the estimate date; a blank date means the date the version was received, in the
company's time zone (Configuration → Policy).

"Not computed": Labor Burden and EAC in the WIP basis read "Not computed" when no rate
covers a division's labor on that date (`EST_NO_BURDEN_RATE`: add the missing row), or
when the estimate has no date and the company's time zone is not set
(`EST_NO_BURDEN_DATE`: fill `estimate_date` in the file, or set the time zone). Both
warnings appear only when Labor Burden (slot 20) is in the company's WIP basis.

## Estimates (F06, D-32)

### The estimate template
The platform's own template is the one estimate source. The blank file is
`docs/templates/estimate_template.xlsx` (regenerate it with
`cd backend && .venv/bin/python scripts/make_estimate_template.py`; the Read me text
lives in that script). Three data sheets, found by name; a *Read me* sheet is ignored:

| Sheet | Columns (header names are the contract; extra columns are ignored) |
|---|---|
| Estimates | `estimate_id, estimator, client, jobsite, name, status, price, estimate_date` |
| Work areas | `estimate_id, order, kept, name, price, notes` |
| Estimate costs | `estimate_id, order, cost_code, hours, amount, notes` |

- `estimate_id` is the estimating system's id (LMN: EST…), matched and updated by id,
  never by name. `status` is Pending, Sold or Lost. `price` is the control total: the
  kept work areas must sum to it. `kept` is Y or N (N = omitted; in LMN a ticked
  Keep/Omit box). `order` is the work area's number, its identity across versions and
  the "#n" on invoice lines (D-26): never renumber. `cost_code` is the tenant's code
  (division digit + slot, D-23: 210 = EX Labor); two rows with the same code under one
  work area are summed. Money to the cent; a numeric cell is read exactly and rounded
  half up.
- Copying from LMN: on the estimate's Workareas + Pricing tab, select the rows and
  paste name and price into *Work areas*; type `estimate_id`, `order` and `kept` (a
  ticked box does not copy). Cost lines come from the item detail, one line per code.
- The same three sheets may be uploaded as three `.csv` files, one per sheet (the header
  says which), in any order: rows for an estimate that has no Estimates row yet are
  held raw and applied when it arrives.

### Uploading and what happens
- Imports page, source "Estimate template" (`POST /api/imports`,
  `source_kind=estimate_template`; roles `firm_admin`, `firm_staff`, `client_admin`).
  The pipeline stores the file (raw first), parses it into one raw record per estimate
  and sheet (`raw_record.source = template`; entities `estimate`, `work_areas`,
  `cost_lines`; a changed sheet is a new raw version, D-20), then the follow-on task
  `estimates.normalize` writes `estimate`, `estimate_version`, `estimate_work_area` and
  `estimate_cost`. Every accepted upload that changes anything is a new version (a full
  snapshot); earlier versions are kept.
- Re-uploading the same bytes is the F03 duplicate; a re-export with the same contents
  ends "Loaded. Nothing changed: this file was already loaded." (`followup_outcome =
  unchanged`). An estimate absent from a newer file is left as it was.
- Rows the file could not load are listed under the batch on the Imports page, one
  sentence each (`import_batch.issues`; the API's `issues`), and the rest of the file
  loads. Fix the file and upload it again; the corrected rows update by id.
- Baseline (D-01): the first version with work areas received while the estimate is
  Sold; when a later upload marks it Sold, the latest version with work areas at that
  moment. Later versions are compared to it by `order`.
- Audit: `estimate_created`, `estimate_updated` (changed fields, version), and
  `estimate_version_created`, one row per estimate touched.

### The Estimates page
Every role reads it (`GET /api/estimates`, `GET /api/estimates/{id}`). The list shows
price, versions and what needs attention; the detail shows the latest version's work
areas with their cost (the sum of their lines), cost by cost category with the D-04
treatment from the `wip_basis` policy ("Not decided" until a `firm_admin` sets it), the
EAC in the basis, the version list, and the attention sentences.

### What each message means
| Code (in the API; never shown alone) | The sentence says | What to do |
|---|---|---|
| `EST_NO_ID` | A row has no estimate id and was not loaded (its cells are kept with the batch). | Give it the id in the estimating system; upload again. |
| `EST_UNKNOWN_STATUS` | The status word is not Pending, Sold or Lost; loaded without a status. | Correct the word; upload again. |
| `EST_ZERO_SOLD` | Sold at 0.00. | Give it its price, or mark it Lost. |
| `EST_PRICE_MISMATCH` | Kept work-area prices do not sum to the estimate price. | Check the kept flags and prices. |
| `EST_UNIT_PRICED` | A kept work area reads as a rate (per day / hour / load). | D-24: a time-and-materials job, or a fixed price. |
| `EST_NO_CATEGORY_SPLIT` | A kept, priced work area has no cost lines; the sold estimate stays off the WIP schedule (D-04). | Add the cost lines on *Estimate costs*. |
| `EST_UNKNOWN_COST_CODE` | A cost code is not on the company's grid; the line is loaded without a category. | Correct the code (Configuration → Cost codes). |
| `EST_COST_LINE_ON_OMITTED` | Cost lines under an omitted or 0.00 work area; left out of every total. | Remove them, or keep the work area. |
| `EST_BURDEN_LINE` | A Labor Burden line (cost code n20) on a kept work area: shown as estimated, left out of the with-burden figures and EAC (D-05). | Remove the line from the file; burden comes from the rate. |
| `EST_NO_BURDEN_RATE` | No burden rate covers a division's labor on the estimate's date; burden and EAC in the basis are not computed. | Add the rate on Configuration → Burden rates. |
| `EST_NO_BURDEN_DATE` | The estimate has no date and the company's time zone is not set, so the received date cannot be read. | Fill `estimate_date` and upload again, or set the time zone. |
| `EST_DEDUCTIVE_CHANGE` | A baseline original work area is now omitted (D-01). | Confirm the deduction, or restore the work area. |
| `EST_WORK_AREA_RENUMBERED` | The name at an order number differs from the baseline. | Check the order column; work areas keep their numbers. |
| (no code) | A row was not loaded: kept not Y/N, a value not a number, a duplicate, a cost line for a work area not in the file, an id not yet on any Estimates sheet. | Fix the row; upload again. |

The per-estimate sentences are computed when the estimate is read (pure generators in
`app/domain/estimates/exceptions.py`); F09 persists them. File-level ones live on the
batch.

## Jobs (F07, D-03)

A **job** is the unit everything after F07 reports on. It carries its estimates (each
with a role) and its **aliases**: the ids other systems know it by. Matching is by alias,
never by name. Names, addresses and jobsites only produce labelled suggestions; a person
makes every job, attachment and link, and each one writes one audit row naming the rows
it touched. Nothing is ever written to QuickBooks.

### Reviewing sold estimates
Jobs → **Review sold estimates (n)** shows every Sold estimate that is on no job, one at a
time (pending and lost estimates never appear). Roles `firm_admin`, `firm_staff`,
`client_admin` act; every role can read.
- **New job** (the default, D-03): choose the division (the one carrying the most
  estimated cost is preselected; an estimate without cost lines suggests none, and a job
  needs one), keep or change the name. The job starts Sold, fixed price, and takes the
  estimate as its `original` plus the alias `lmn_estimate` = the estimate id.
- **Attach** to a job, with a role: `change_order` (an addition to that job's scope that
  the customer treats as one contract, D-03) or `ignored` (superseded or sold in error; a
  reason in the note is required; it never returns to the queue and counts for nothing).
  `original` is accepted only by a job that has none. Suggested jobs are listed first
  with the reason: *same customer* (a QuickBooks project named with this estimate's id
  belongs to that job's customer), *by client name only*, *by name only* (same jobsite or
  estimate name). Any other job can be chosen from the list. A pool never takes an
  estimate.
- **Skip** moves to the next estimate and writes nothing. The **Estimate** select at the
  top of the queue lists every waiting estimate (id, name, price) and jumps to the one
  chosen; it writes nothing either. "Sold, not yet reviewed" on an estimate's detail opens
  the queue on that estimate; if it is no longer waiting (already a job, or no longer
  sold) the queue opens at its first entry and says so in one sentence.
- **Detach** (on the job) returns an estimate to the queue and removes its estimate id
  from the job. The original cannot be detached while other estimates are attached.

The rule the reviewer applies (D-03): one job per distinct scope that management tracks
as a unit, priced against its own budget, billed on its own. Turley is two jobs;
DeVellis/Mukherjee is one job with EST6281138 attached as a change order once it sells.

### Work-area kinds and the contract figures (D-01)
On the job detail, each kept work area of the original estimate shows its kind:
*suggested* (from the "CO:" / "C/O" name prefix or from appearing after the baseline)
until someone confirms it as Original or Change order. The Kind column reads "Original
(suggested)" or "Change order (suggested)" until then, and "Original, confirmed" or
"Change order, confirmed" after. The **Action** column says what a click does: an
unconfirmed row has two separate controls, **Confirm original** and **Confirm change
order**; a confirmed row has one, **Change to change order** or **Change to original**.
**Confirm all as suggested (n)** above the table confirms every unconfirmed kept work
area at its suggestion in one press, each one as its own audit row (the suggestion is the
platform's, the confirmation is the person's, D-01); already-confirmed rows are not
touched, an omitted row is never confirmed, and a second press with nothing left writes
nothing. Figures are computed each time the job is read, never stored:
- **Revised contract**: the kept work areas confirmed as original. Nothing counts before
  it is confirmed ("n work areas to confirm" shows beside it). An original estimate with
  no work areas loaded counts its price ("No work areas loaded").
- **Unapproved change orders**: work areas confirmed as change orders, and every kept
  work area of an estimate attached as a change order (its price when it has no work
  areas), without an approval that applies. They stay outside the contract until the
  project manager approves them (F07.4, D-42, below); **Approved change orders** then
  join the **Revised contract** = original contract + approved change orders.
- **EAC in the WIP basis**: the sum of each attached estimate's own figure (burdened,
  F06.1), ignored estimates excluded; "Not computed" if any of them is not computed.
- Time and materials: no revised contract (D-24). Pool: no contract and no EAC (D-30).

A confirmation survives a re-upload when the work area keeps both its order number and
its name; a renamed or new work area is suggested again and needs confirming.

### Approving a change order (F07.4, D-42, D-44)
A change-order work area (confirmed as one on the original estimate, or any kept work
area of an estimate attached as a change order) is outside the revised contract until a
person records that the customer agreed. The platform records approval; it does not
obtain it: sending the change order and collecting the signature happen outside it.
- **Who**: roles `client_pm` and `firm_admin`, under their own login. `client_admin`,
  `firm_staff` and `client_viewer` do not approve. Give the project manager a login on
  the droplet (the display name is what the approval shows, "approved 2026-09-14 by
  <display name>"):
  ```sh
  cd /opt/wip/backend
  sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/create_user.py create-user \
      --email <project manager's e-mail> --display-name "<project manager's name>" \
      --membership rye-beach:client_pm
  ```
  It prints the activation link once, to stdout only; hand it over directly. The link
  sets the password; TOTP is offered and not required for a client role. A lost link:
  `scripts/create_user.py issue-link --email <e-mail>`.
- **The evidence key**: Configuration → Policy → **Change order evidence** (`firm_admin`):
  "None required" or "A reference is required". No default; approving is refused in one
  sentence while it is undecided. Rye Beach: None required (D-42).
- **Approve**: on the job, the work-area table's Approval column reads "Change order, not
  approved"; **Approve** opens a small form in the row: the date the customer agreed
  (required, not after today), who at the customer agreed, a reference to the evidence
  (required when the key says so), a note; **Record approval** writes one row and one
  `change_order_approved` audit row, and the row reads "Change order, approved <date> by
  <name>". Approved change orders join the revised contract at the price approved; EAC
  does not move (D-44: the cost of an unapproved change order is expected whether or not
  its price is agreed). A change order priced 0.00 may be approved; it moves no figure.
- **Withdraw approval** asks for the reason (required) and writes one row and one
  `change_order_approval_withdrawn` audit row; the work area is unapproved again.
  Approving again is a new row. **Approval history** below the table lists every
  approval and withdrawal, newest first; nothing in it is ever edited.
- **A later version of the estimate**: an approval is for the work area at its price that
  day. If a later version changes the work area's price or name, omits it or drops it,
  the approval ends for good, the work area is unapproved again, and the job's attention
  and Home say so (`CO_APPROVAL_NOT_CARRIED`, naming the approved price and date and what
  the version did) until a person approves it again (a new row at the new price) or
  withdraws the ended approval with a reason. A version that restores the name and price
  does not revive it. A version that leaves the work area as it was carries the approval.
- **Kinds**: an approved change order cannot be changed to original until its approval is
  withdrawn (the refusal says so). "Confirm all as suggested" never approves anything.
- **The unapproved change orders list**: Jobs → **Unapproved change orders (n)**: every
  kept, confirmed, unapproved change order on a sold, in progress or substantially
  complete job, with the job, estimate, estimator, work area, price, the days since the
  work area first appeared (the received date of the first version carrying it under that
  name) and a note when an earlier approval no longer applies; a total. Every role reads
  it; each row opens its job. Closed and cancelled jobs are absent.
- Rye Beach, 6115758: nine of the eleven priced change orders are the ledge removal days
  the owner expects to move to a time-and-materials job (D-24); the owner decides with the
  project manager before any of them is approved.

### Correcting Sold on
A job's **Sold on** is the original estimate's date when the file carried one, else the
day the job was made, shown with "(set when created)". The roles that manage jobs correct
it in the job's edit form (the "Sold on" date field, then "Save changes"); the change is
part of the job's `job_updated` audit row with before and after, and the note goes away
once a person has set the date (the audit log is how that is known; no column holds it).
A date after today in the company's time zone is refused in one sentence.

### Linking a job to QuickBooks
The job detail's QuickBooks section lists **suggested rows**, each with why:
*estimate id in name* (the §13.2 convention: `6366990 …` or `EST6366990 …`), *customer
name* (the row's customer matches the estimate's client once generic words such as
"client", "LLC", "Inc", "and" are dropped), *address* (a street number and street name in
both; compass words are skipped, so "378 East Dunbarton" matches "378 E Dunbarton"),
*customer name and address*. Suggestions come from active projects and sub-customers not
yet linked. For anything else (a pool, a project named without the id), **search** by
name: any active, unlinked QuickBooks row. **Link** is made by the row's id; the job's
customer becomes the row's parent (or the row itself when it has none). A job may have
several QuickBooks rows; one QuickBooks row belongs to at most one job (a second link is
refused: unlink it from the other job first). The link also **tracks** the row (D-37, below)
in the same action and audit row. **Unlink** removes the link; unlinking the
last one clears the job's customer; the row stays tracked until someone untracks it.

### When a QuickBooks project is created (D-35)
- A construction or excavation job (fixed price or T&M) gets its QuickBooks project when
  the **first money moves**: a deposit, the first invoice or the first cost. Not when the
  estimate sells, and no "potential" projects for sold estimates with no activity.
- Until then the job exists only here, at status **Sold**; it counts in backlog and
  nowhere else. The Jobs page shows it as "Not yet: sold, no money moved (backlog)" and
  raises nothing.
- When the project is created, link it to the job and set the job **In progress** in the
  same sitting: on the job's QuickBooks section the box "When linking, set the job In
  progress" is ticked for a Sold job, and the link writes one audit row with both.
- An In progress or Substantially complete job with no link raises `JOB_NO_LEDGER_LINK`.
  Closed and Cancelled jobs never do: jobs finished before go-live have no project.
- Money for a construction job is recorded **on its project**, never on the parent
  customer; the deposit that triggers the project is entered on the project. A document
  on a parent customer for a construction job is a data error (F08 shows it as
  unassigned until it is moved).

### Pools and programs
- Pools (D-30): Jobs → New pool or program job, kind Pool (name, division); link it to
  the `Pool - <group>` project found by search.
- Maintenance and snow (D-35) are not projects per contract. One QuickBooks project per
  division-season, `Maintenance <year>` and `Snow <season>`, each under a customer of the
  same name; in the platform one job each: Jobs → New pool or program job, kind Program
  (revenue method Recurring service), linked to that project found by search. A program
  is recognised as billed: no contract, no EAC, never on the WIP schedule. A sold maintenance
  or snow estimate is not normally loaded; if one arrives, attach it to the season's program
  job with role Ignored and the note "maintenance contract; billed as service" (the only role
  a program job accepts; Review sold estimates presets both when the program is chosen). Maintenance and snow purchases and labor are coded to the program project.
- The project list a person keeps: active construction and excavation jobs, one pool per
  material group, one program project per maintenance or snow season. Everything else is
  a plain customer.

### Picking the customers and projects to work on (F07.2, D-37)
The platform works on the QuickBooks rows a person has **tracked** and on nothing else;
the sync still copies the whole company, and the month totals, the drift check and every
tie-out read the full copy. Customers (roles `firm_admin`, `firm_staff`, `client_admin`):
- **Tracked** lists every tracked row (projects, then sub-customers, then customers) with
  its job (a link to the job) or the sentence that it needs one. A row QuickBooks has made
  inactive stays tracked, reads "Inactive in QuickBooks" and raises nothing. **Untrack**
  is offered only for a row with no job; a linked row says "Unlink on the job first".
- **Find a customer or project**: type part of the QuickBooks name and **Search**. Active
  rows only, projects first, 50 a page (Previous page / Next page); an empty box returns
  nothing. Each row shows its parent, what it is, the QuickBooks id, the invoice and
  payment counts, its job when linked, and **Track**. Track and untrack send the row's id
  and nothing else; the name is only searched.
- Linking a job to a QuickBooks row (above) tracks the row in the same action; its
  `job_alias_linked` audit row records `customer.tracked_at` before and after. Every
  track and untrack by hand writes one audit row (`customer_tracked`,
  `customer_untracked`). Rows linked before migration 0012 were tracked by the migration
  as of the link, by the user who made it (no audit row: the link's row is the record).
- A fresh company shows a quiet Jobs page: `LEDGER_PROJECT_NO_JOB` is raised only for a
  tracked, active row with no job, with or without documents, and the block "QuickBooks
  projects with no job" is drawn only when there is one.

### Possible duplicate customers
Behind the link **Possible duplicate customers** on the Customers page (same roles):
pairs of active top-level QuickBooks customers whose names have the same words once
punctuation, "and", "&" and suffixes are dropped, or differ by one word (a first name),
with each one's invoice and payment counts. Merge them in QuickBooks; the platform follows
on the next sync. Nothing on the page writes anything. (Before trusting links across a
merge, check what QuickBooks sends after one: F05.1 Discovered.)

### The Sold Jobs Board (F08, D-02, D-39)
Jobs is the board: for every job, from QuickBooks, deposit invoiced and received, billed
to date, fuel surcharge billed, collected to date, open A/R, remaining to bill and days
since last activity, with the original estimate's number under the name and the
estimator. Every figure is computed when the page is read from the F05 rows through the
job's QuickBooks links (`job_alias`, system `qbo_customer`); nothing is stored. The page
names the company, the as-of date (the company's today; every figure is to date) and the
tie-out status; the job page's Billing section shows the same figures with the billing
and payment histories, newest first, voided and deleted documents marked and in no
figure.

**Before the figures show.** Two policy keys are needed (Configuration, Policy): the
deposit items and the fuel surcharge items. In QuickBooks create one deposit item and one
fuel surcharge item **per division income account** (Products and services → New →
Service; income account the division's 4xxx); run a change poll or wait for one so the
items are held; then tick them on Policy. The pick-list (F08.2) shows the company's
active items sorted by name; "Find an item" narrows the list as you type; "Show inactive
items" adds the inactive and deleted ones, each marked in words. An item that is already
ticked is always shown, so a decided value is never hidden. An inactive or deleted item
may be ticked: an older invoice may sit on it, and matching is by item id. Until both keys are decided the page says so
and shows collected to date, open A/R and unapplied payments only.

**How a deposit is recognised (D-02, owner's answer 2).** An invoice numbered
`<estimate number>_DEP` (the LMN estimate number, e.g. `EST6115758_DEP`) whose lines,
other than fuel surcharge lines, are all on a deposit item. One mark without the other
is not a deposit: the invoice still counts in billed to date and raises
`DEPOSIT_NOT_IDENTIFIED` (fix the item or the number in QuickBooks). A deposit is keyed
on the job's project, never on the parent customer (D-35).

**How a fuel surcharge is recognised (D-39).** A line on a fuel surcharge item, by item
id only. It is outside billed to date and remaining to bill and is shown as "Fuel
surcharge billed"; collected to date and open A/R are whole-document figures and include
it. Sales tax is outside billed to date too (owner, 2026-10-06).

**Not on a job.** One row under the totals: documents and payments on QuickBooks rows
with no job (untracked rows, tracked rows with no job, and the parent customer of a
construction job, D-35, D-37). It is never dropped: the tie-out needs it. Money on a
parent customer for a construction job is a data error; move the document to the
project in QuickBooks and the next poll moves the amount.

**Collected to date (D-41).** Cash only: a payment's cash is its total less its
unapplied amount, and every payment adds exactly its cash. Within a payment, a line
that points at an invoice or a sales receipt counts for the job of that document and a
credit memo line counts against it, dated by the payment; a line naming a document the
copy does not hold counts on the payment's customer row. Lines of any other type (a
journal entry, a deposit, an expense) are not read one by one: their net, the payment's
cash less its invoice lines plus its credit memo lines, is the payment's remainder and
counts on the payment's own customer row. Where that remainder is a credit, an invoice
was settled by something other than cash; the amount is shown apart as "Other credits
applied" on the same row (billed − collected − other credits applied = open A/R) and the
job raises `PAYMENT_OTHER_CREDIT`, one sentence per payment with the amount and the
date. A payment with nothing to apply to raises `PAYMENT_UNAPPLIED` and is not billed or
collected until it is applied in QuickBooks. A sales receipt is billed once and
collected once.

**Tie-out.** For every calendar month, jobs + not on a job = the Connections month
totals: billed (invoices − credit memos + sales receipts, tax and surcharge included)
and collected (payments + sales receipts), to the cent. The status is on the page and
the XLSX has a Tie-out tab with the months and the differences. The board's rows show
first; the tie-out is its own request (`GET /api/jobs/tie-out`, summed by the database
over every job whatever the filter) and the line reads "Tie-out: checking…" until it
answers (F08.2). "Does not tie" names the months; one cause is a payload the normalizer
skipped (Connections, skipped payloads); on the collected side, run the diagnostic below.

**Checking the collected side (F08.2).** On `rye-beach` the collected side failed in 18 of
308 months (2026-10-06) because a journal-entry credit (once, a deposit) applied to
invoices through a payment was counted as cash; D-41 settled it. The diagnostic stays
for the next time. `scripts/collected_tieout.py` is read-only,
prints no customer name, and reports every month whose collected side does not tie:
the board's figure, QuickBooks' figure, the difference; each live payment of the month
whose signed applications plus unapplied money differ from its total; each application
on a payment of the month that does not point at an invoice (a CreditMemo, JournalEntry
or Deposit line, with whether the copy holds that document); and whether the month's
difference equals twice the sum of its non-CreditMemo credit lines (what counting such
a line as cash does, the pre-D-41 cause). On the server:

```sh
cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \
    scripts/collected_tieout.py --tenant rye-beach            # every month that does not tie
cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \
    scripts/collected_tieout.py --tenant rye-beach --month 2018-12 --month 2022-10
cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \
    scripts/collected_tieout.py --tenant rye-beach --counts   # row counts only
```

The output is ids, dates, kinds and amounts; paste it into the F08.2 brief. What a
credit applied through a payment does to collected to date is the owner's treatment,
decided before any figure changes.

**Speed (F08.2).** The board reads the listed jobs' own QuickBooks rows; the
not-on-a-job row, the tie-out and the Connections month totals are summed by the
database; a job page reads that job's rows only. Nothing is stored. To time the pages
against a tenant the size of a real one on a development database, seed one:
`cd backend && .venv/bin/python scripts/seed_board_load.py --documents 10000` makes
tenant `load-test` (refusing a protected slug and any database not named `wip` or
`wip_test`) and adds that many invoices and payments on one untracked row across 308
months; `--counts` above says how many rows the real tenant holds.

**Exports.** "Export XLSX" and "Export PDF" take the filters as shown. The XLSX holds

**Invoice lines and work areas (F08.1 Part 1, D-45).** The job page's work-area tables
show billed to date and left to bill per work area, with a totals row, and the section
"Invoice lines" lists every line of the invoices, credit memos and sales receipts on the
job's QuickBooks rows with its quantity, rate and service date (read from QuickBooks for
display) and how it is tied to a work area: by its pay application (Part 2), by "#n" at
the start of its description (D-26; only when n is a work area of the original estimate
and of no other estimate attached to the job), or as a person assigned it. "Not assigned
to a work area" is the job's billed to date less every tied line; it holds the deposit
invoice, sales tax and fuel surcharge lines, which are never tied, so billed to date is
unchanged by anything here. To assign a line (`firm_admin`, `firm_staff`,
`client_admin`): pick the work area in the row and press **Assign**; a line whose
description equals a work area's name shows that work area as "(suggested)", and
**Confirm all as suggested (n)** assigns every suggested line in one press, one audit row
each (`billing_line_assigned`; a change is `billing_line_reassigned`, **Clear** is
`billing_line_cleared`; every event is a row of `billing_line_work_area`, never edited).
A line is tied whole: one that covers several work areas stays unassigned. A later
version of the estimate: the assignment follows the work-area number, as "#n" does; a
renamed work area shows the renumbered sentence beside the line, and a work area the
latest version no longer carries leaves the line "Not assigned" with a sentence until it
is assigned again or cleared. When billed to date on change orders no one has approved
is above 0.00 the job says so (`BILLING_UNAPPROVED_CO`, below), on the board, the job
page and Home; it changes no figure. On 6115758 the nine ledge removal days (#18, #22 to
#29) billed on two invoices are the case; the owner decides with the project manager
whether they are approved or moved to a time-and-materials job (D-24).
values written from Decimal (no float artifacts) with the cents format, a totals row,
the not-on-a-job row, the legend (§8.7) and the Tie-out tab; the PDF carries the same
rows and totals (D-40). Every role that reads jobs can export.

### Pay applications (F08.1 Part 2, D-26, D-36, D-39, D-43)
For a fixed-price job (D-24), operations says how far along each work area is and the
platform says what to bill and gives the customer the document.
- **Entering the request** (`client_pm`, `client_admin`, `firm_staff`, `firm_admin`): on
  the job, **Pay applications → New billing request**: the application date; whether the
  fuel surcharge applies to this application (no default, D-39); one percent for every
  listed work area, or a cumulative percent complete to date per work area (D-26),
  prefilled from the last issued application. The schedule lists every kept original
  work area and every change order approved on the application date (D-42); an
  unapproved change order is not listed and cannot be billed by percent. **Save draft**
  makes pay application n (one more than the highest `_PMT` number on the job's
  invoices, then continuing); the next request replaces the open draft.
- **Reading the exceptions**: one sentence each on the draft, none stopping the other
  lines; the work area is left off this application: over 100.00%
  (`BILLING_OVER_100`), priced 0.00 (`BILLING_UNPRICED_CO`), omitted from the estimate
  (`BILLING_OMITTED_AREA`), below the previous application's percent
  (`BILLING_NEGATIVE`). Enter the request again with the corrected percents.
- **The application**: per line the scheduled value, percent complete to date, earned to
  date, earned on previous applications (the last issued application, or, before the
  first one, what the "#n" and assigned invoice lines billed on the work area, D-45),
  earned this application, balance to finish; the summary: total earned to date, less
  billed to date before this application (the job's whole billed to date as QuickBooks
  has it on or before the application date, the owner's answer A: the deposit, earlier
  applications and any other invoice alike), the amount due, never below 0.00; when
  billed exceeds earned, "Billed ahead by x" and no invoice is due. With the surcharge:
  "Fuel surcharge (5.00%)" on the amount due and the total to invoice.
- **Issuing** (`client_admin`, `firm_staff`, `firm_admin`): **Issue** freezes the rate,
  billed before and the amount due on the application and writes
  `pay_application_issued`. It is refused in words while the surcharge choice is
  unanswered, while the surcharge applies and the rate is undecided (Configuration,
  Policy), or while the board's policy keys are undecided. An issued application is
  never edited: a correction is **Void** (the reason is required, `pay_application_voided`)
  and a new request.
- **Keying the invoice in QuickBooks**: on the job's project, document number
  `<estimate number>_PMT<n>`, one line "Pay application n" for the amount due and, when
  it applies, one line for the surcharge on a fuel surcharge item (D-39); the application
  screen shows the three things to copy. The platform writes nothing to QuickBooks. Once
  the invoice is held, its lines read "Pay application n" under Invoice lines and each
  work area is billed what the application earned this application.
- **PDF**: the customer's copy, from the same figures as the screen: the company, "Pay
  application n", the customer, the job, the estimate, the application date, the status,
  the schedule of values, the summary, the surcharge line when it applies, "Invoice
  <number>", "Page n of m". No legend; no product name.
- **What each tie sentence means**: `PAYAPP_NOT_INVOICED`, an issued application with an
  amount due and no invoice of that number in QuickBooks (key it); `INVOICE_NO_PAYAPP`,
  an invoice on the job dated on or after the job's first issued application that is not
  an application's and not the deposit (the format changes job by job, D-36: enter a
  request, or correct the number); `PAYAPP_INVOICE_MISMATCH`, the invoice less its fuel
  surcharge lines differs from the amount due, or its fuel surcharge lines differ from the
  printed surcharge (the sentence names the difference; correct the invoice).

### What each message means
| Code (in the API; never shown alone) | The sentence says | What to do |
|---|---|---|
| `EST_UNATTACHED` | A sold estimate is on no job, with how long it has been sold. | Review it: new job or attach. |
| `JOB_SECOND_ESTIMATE_FOR_CUSTOMER` | A sold estimate's customer already has a job (by project id or by client name). | Decide per D-03: new job for separate scope, attach as a change order for an addition. |
| `JOB_NO_LEDGER_LINK` | An In progress or Substantially complete job has no QuickBooks link, so its billing cannot be read. Never raised for a Sold (backlog, D-35), Closed or Cancelled job. | Link its project; create it in QuickBooks first if needed (§13.2). If no money has moved yet, the job should be Sold. |
| `LEDGER_PROJECT_NO_JOB` | A tracked, active QuickBooks row (customer, sub-customer or project) has no job, with or without documents (D-37). An untracked or inactive row never raises it. | Link it to its job, or make the job first; untrack it if it is not a job. |
| `JOB_DIVISION_UNSET` | A job has no division (only for data made outside the review). | Set the division on the job. |
| `CUSTOMER_FUZZY` | Two customers look like one (Customers page). | Merge in QuickBooks. |
| `PAYMENT_UNAPPLIED` | Money received on the job is applied to no invoice (D-02; F08). It is not billed or collected to date. | Apply it in QuickBooks once the invoice exists (the deposit invoice, D-02, or the pay application's invoice). |
| `PAYMENT_OTHER_CREDIT` | An invoice on the job was settled through a payment by something other than cash, a journal entry or a deposit (D-41; F08.2); the amount is "Other credits applied", outside collected to date. One sentence per payment, with the amount and the date. | Nothing in QuickBooks unless the entry was wrong; the figure is there so collected to date stays cash. |
| `DEPOSIT_NOT_IDENTIFIED` | A `_DEP` invoice is not on a deposit item, or a deposit item is on a document that is not `<estimate number>_DEP` (F08). It still counts in billed to date. | Fix the item or the document number in QuickBooks; the next poll clears it. |
| `BILLED_OVER_CONTRACT` | Billed to date exceeds the revised contract (F08; §10). | Likely a change order not yet approved: attach or approve it; or correct the invoice. |
| `BILLING_UNAPPROVED_CO` | Billed to date on kept change orders that are not approved is above 0.00 (D-45; F08.1): the amount and the work areas, from the lines tied to them. No figure changes. | Approve the change orders (the project manager, D-42), move the work to a time-and-materials job (D-24), or correct the assignment; the flag clears on read. |
| `BILLING_OVER_100`, `BILLING_UNPRICED_CO`, `BILLING_OMITTED_AREA`, `BILLING_NEGATIVE` | A billing request named a work area above 100.00%, priced 0.00, omitted, or below the previous application's percent (D-26; F08.1); the work area is left off the draft, the other lines stand. | Enter the request again with the corrected percent, or leave the work area off. |
| `PAYAPP_NOT_INVOICED` | An issued pay application with an amount due and no invoice of its number in QuickBooks (D-36; F08.1). | Key the invoice from the application ("Pay applications" above). |
| `INVOICE_NO_PAYAPP` | An invoice on the job dated on or after the job's first issued pay application that is not an application's and not the deposit (D-36; F08.1). | Enter the billing request it belongs to, or correct its number. |
| `PAYAPP_INVOICE_MISMATCH` | The invoice does not tie to its pay application: the amount less fuel surcharge lines, or the fuel surcharge lines themselves, differ from the application (D-39; F08.1); the sentence names the difference. | Correct the invoice in QuickBooks; the next poll clears it. |

The review items are computed when read (`app/domain/jobs/issues.py`); F09 persists them.
Home lists the three F08 needs after the F07 ones, first rule that applies.

## QuickBooks connection (F05)

Built against the Intuit **sandbox** only (D-25). The sandbox company is connected to its own
tenant (slug `qbo-sandbox`) and never to `rye-beach`. The platform reads QuickBooks; it never
writes to it.

### The four settings
No defaults. They are needed only when a QuickBooks route or task runs, so a deployment
without QuickBooks still starts; a missing one is named (never its value) when "Connect to
QuickBooks" is pressed.

| Variable | Value |
|---|---|
| `QBO_CLIENT_ID` | From the Intuit developer app, "Keys & credentials", **Development** |
| `QBO_CLIENT_SECRET` | Same place. Never logged, returned, or stored in the database |
| `QBO_ENVIRONMENT` | `sandbox` (Development keys reach sandbox companies only) or `production` |
| `QBO_REDIRECT_URI` | Development: `http://localhost:5173/api/qbo/callback` |
| `QBO_WEBHOOK_VERIFIER` | F05.1. The Verifier Token of the Intuit app's webhook tab that matches `QBO_ENVIRONMENT`. Only on a public host; unset on the Mac (the webhook route answers 503) |
| `PROTECTED_TENANT_SLUGS` | F05.1 (D-28). Slugs `scripts/delete_tenant.py` refuses; production `rye-beach` |

The redirect URI must be listed, character for character, under "Redirect URIs" on the same
page of the Intuit app. In development it points at the Vite server (port 5173), which passes
`/api` on to the API. Sign in at `http://localhost:5173`, not `127.0.0.1`: the connection still
completes either way (the callback does not depend on the session cookie), but the browser
would come back to a sign-in screen. Restart the API and the worker after changing a setting.

### Connect and reconnect
1. Create the tenant once (`POST /api/admin/tenants`, slug `qbo-sandbox`), switch to it.
2. Connections → **Connect to QuickBooks** (firm admin only). The browser goes to Intuit;
   choose the sandbox company and press Connect.
3. The browser returns to Connections with one sentence saying what happened. "Connected",
   the company name and "sandbox company" are shown on success.

What can be refused, and what to do:
- *link already used or expired*: the link is good for 10 minutes and one use. Start again.
- *already connected to another client*: one QuickBooks company belongs to one tenant.
- *linked to a different QuickBooks company*: a reconnect must choose the same company.
- *books are already held for a different QuickBooks company*: once anything has synced, the
  tenant belongs to that company for good, even after Disconnect. QuickBooks numbers records
  per company, so a second company's records would overwrite the history of the first. A new
  QuickBooks company needs a new tenant. (Disconnect then Connect to a different company
  works only while nothing has synced.)
- *started by a different user*: finish in the browser of the admin who started it.

**Reconnect** runs the same flow for the same company and keeps everything synced so far.
**Disconnect** revokes the connection at Intuit, clears the stored tokens and keeps the
synced data and the company link. Each of these writes to the tenant's audit log
(`connection_started`, `connection_completed`, `connection_tokens_set`,
`connection_needs_reconnect`, `connection_disconnected`); no token, authorization code or
`state` value is ever in an audit row or a log line, and the API's access log records the
callback without its query string.

### Tokens and "Needs reconnect"
Tokens are stored encrypted (key id beside the ciphertext, bound to the tenant, the
connection and the field; `scripts/reencrypt.py` covers them). The access token is refreshed
when it has under five minutes left, and on a 401 once; every refresh replaces both tokens,
and the moment is kept in `connection.tokens_refreshed_at` (one log line, no audit row).

**Needs reconnect** means Intuit refused the refresh token (`invalid_grant`): it was revoked
in QuickBooks, it expired unused, or the connection was removed there. The status is set
once, audited, and the stored tokens are cleared. Nothing retries and nothing syncs until a
firm admin presses **Reconnect**. `connection.last_error` holds the reason as a short code.

### Rotating the client secret
1. Create a new secret on the Intuit app's "Keys & credentials" page.
2. Put it in `QBO_CLIENT_SECRET`, restart the API and the worker.
3. Press "Reconnect" on one tenant's Connections page and confirm it says "Connected".
   Stored tokens stay valid across a secret rotation; only new token requests use the secret.
4. Delete the old secret at Intuit.

### Backfill, change polling, nightly check
The platform keeps a copy of the company: every §6.2 entity stored raw (`raw_record`,
insert-only, versioned), and customers, invoices, credit memos, sales receipts and payments
normalized into `customer`, `billing`, `billing_line`, `payment`, `payment_application`.
Accounts are matched to the chart by account number only (`gl_account.external_id`): a number
held by exactly one active QuickBooks account and one chart row attaches the QuickBooks id to that
row and changes nothing else (not the name, not the type). The page lists what did not line up:
accounts without a number, numbers used twice in QuickBooks, numbers in QuickBooks with no
chart row, and chart rows whose number QuickBooks does not use. Deposits
and the cost-side entities are stored raw and read by later features.

- **Backfill** (`sync_run.kind = backfill`): starts on the first connection, or from
  "Start fresh backfill". One worker task per page and entity (1,000 records, paged by Id);
  every page stores raw first, then queues the normalizer for those records. The run ends
  `succeeded` when every entity is done and seeds the change poll and the nightly check.
  The Connections page shows "Backfill, running" until then; refresh to follow it.
- **Change poll** (`kind = cdc`): every `QBO_CDC_POLL_MINUTES` (default 15) once a backfill
  has completed. One Change Data Capture request for all entities since the last successful
  run (minus two minutes of overlap; identical payloads store nothing). A delete arrives as a
  stub and becomes a new raw version flagged deleted; the row gets `deleted_at` and leaves
  every total. Each poll queues its successor first, so a failed poll cannot end the chain.
  "Sync now" queues one extra poll; pressing it twice queues one.
- **Nightly check** (`kind = drift`, about 02:00–03:00 Eastern): QuickBooks' own `COUNT(*)`
  per entity against the platform's current records, and the month totals of the normalized
  rows against a re-sum of the raw payloads. A difference ends the run `drift`; the
  Connections page names the entity and both counts, or the month and both amounts. Nothing
  is re-pulled. What to do: "Sync now" first (a change may simply not have been polled
  yet); if the difference stays, "Start fresh backfill"; if it stays after that, read the
  run's `detail` in `sync_run` and the affected raw record.
- **Fresh backfill needed**: the page says so when no backfill has completed or the last
  successful run is more than 29 days old (Intuit's CDC reaches back 30 days). Polling stops
  by itself in that state; it never re-pulls on a schedule. Press "Start fresh backfill".
- **Payloads that could not be read**: a record whose amounts have more than two decimals,
  whose totals do not add up, or that is in another currency is stored raw, counted per
  entity on the page, and skipped; the rest of the page loads. A later version that reads
  clears the count.
- The worker re-seeds the poll and check chains of every connected company when it starts
  (`STARTUP_HOOKS`), one tenant per transaction, so a restart never leaves a company unpolled.

Reading a run by hand (as `app_rw`, with `SET LOCAL app.tenant_id`):
`SELECT kind, outcome, started_at, finished_at, records_fetched, records_stored, error, detail
FROM sync_run ORDER BY started_at DESC LIMIT 20;`

### Intuit production keys and questionnaire answers (F05.1)
Production keys are issued from the Intuit app's **Production** tab once its settings and the
app assessment questionnaire are complete. Everything below is done by the owner in the
developer portal; nothing is code, and no key is ever pasted into chat, the repo or the Mac.

Production tab settings, character for character:

| Field | Value |
|---|---|
| Host domain | `jobcost.dev` |
| Launch URL | `https://jobcost.dev/` |
| Disconnect URL | `https://jobcost.dev/api/qbo/disconnected?realmId=` (Intuit fills the realm after `=`; see "Intuit-side disconnect") |
| Privacy policy | `https://jobcost.dev/privacy` |
| EULA / terms | `https://jobcost.dev/terms` |
| Redirect URI | `https://jobcost.dev/api/qbo/callback` |
| Scope | `com.intuit.quickbooks.accounting` only |

Questionnaire answers (the facts recorded in this file and on the privacy page; answer in
these words, adding nothing):
- **What the app does**: job cost and work-in-progress reporting for construction and
  landscape contractors, operated by a CPA practice for its own clients. Not listed on the
  Intuit App Store; one operator, one deployment.
- **Data read**: the client's own QuickBooks Online company, read-only, through the
  Accounting API: accounts, customers and projects, vendors, items, invoices, payments,
  credit memos, sales receipts, deposits, bills, vendor credits, purchases, journal
  entries, time activities, preferences, company info. Nothing is written to QuickBooks.
- **Hosting**: one DigitalOcean droplet and a DigitalOcean Managed Postgres cluster and
  Space, United States (New York region). TLS only (Let's Encrypt; HSTS).
- **Token storage**: OAuth tokens are encrypted at the application layer with a key held
  only on the server (key id stored beside the ciphertext, rotation supported); the
  database role the application uses cannot bypass row-level security; tokens are never
  logged or returned by the API.
- **Data sharing**: none. Data is not sold and not shared with third parties.
- **Access**: authorized users of the client and of the practice, by role, with a password
  and, for practice staff, a required authenticator app.
- **Backups**: daily managed backups with seven-day retention.
- **Deletion**: on request; QuickBooks tokens are revoked at Intuit and removed as soon as a
  connection is disconnected, and a client's data is deleted with `scripts/delete_tenant.py`
  ("Deleting a tenant").
- **Contact**: `admin@jobcost.dev`; the operating entity is Ryze Group, Inc., a New
  Hampshire corporation.
- **Error handling**: a 429, 5xx or transport failure at Intuit is retried five times in
  all with exponential backoff (1 s doubling, capped at 60 s, `Retry-After` honoured); an
  `invalid_grant` refusal is never retried and ends the connection as "needs reconnect".
  The failing response's `intuit_tid` is stored on the failed `sync_run.detail` or on the
  `connection_needs_reconnect` audit row, and every Intuit call is logged with its
  `intuit_tid`, so a support case can name the call. The signed-in app's footer carries
  `Support: admin@jobcost.dev`.

When the keys arrive, on the droplet as root: put them in `/etc/wip/app.env` as
`QBO_CLIENT_ID` and `QBO_CLIENT_SECRET`, set `QBO_ENVIRONMENT=production`, put the
**Production** webhook verifier in `QBO_WEBHOOK_VERIFIER`, then `systemctl restart wip-api
wip-worker`. The Development keys stay on the Mac's `.env` only. `app.env` was installed once
by `setup.sh` and is never overwritten: the two F05.1 names (`QBO_WEBHOOK_VERIFIER`,
`PROTECTED_TENANT_SLUGS=rye-beach`) are added to it by hand, in the order
`deploy/env.template` shows.

### Webhooks (F05.1)
Intuit posts a signed delivery to `https://jobcost.dev/api/qbo/webhook` when something changes
in a connected company. The delivery is only a trigger: the route checks the signature, stores
the delivery raw (`webhook_event`, insert-only, one row per event), answers 200, and then
queues one change poll for the company, the same poll "Sync now" queues; a burst of
notifications is one poll. Polling every `QBO_CDC_POLL_MINUTES` stays on as the safety net,
and the nightly check is unchanged. That is Intuit's own recommendation (Best practices: a
CDC call back to the last processed event plus a daily sweep), which F05 already was.

**Registering** (owner, developer portal, one tab at a time: the server holds one verifier,
the one for the environment `QBO_ENVIRONMENT` names):
1. Webhooks → **Development** tab first, while the server still runs the Development keys:
   endpoint URL `https://jobcost.dev/api/qbo/webhook`; entities: Customer, Invoice, Payment,
   CreditMemo, SalesReceipt, Deposit, Account (the F05 set) and Bill, VendorCredit, Purchase,
   JournalEntry, BillPayment (the F10 set, so F10 needs no portal change). Save; copy the
   **Verifier Token** into `QBO_WEBHOOK_VERIFIER` in `app.env`; restart both services.
2. Prove it end to end: switch to the `qbo-sandbox` tenant, create an invoice in the sandbox
   company, do **not** press Sync now, and watch it appear on the Connections page within five
   minutes. The page shows "Last webhook received" and the count in the last 24 hours.
3. **Production** tab, after the keys arrive: the same endpoint and entities; its own Verifier
   Token replaces the Development one in `app.env` at the same moment as the keys; restart.

**Verifier rotation**: generate a new token on the tab that matches `QBO_ENVIRONMENT`, put it
in `app.env`, restart. Deliveries signed with the old token are refused with 401 during the
seconds in between and Intuit retries them (below), so nothing is lost.

**What to check when none arrive**:
- Intuit's rules (Best practices): the endpoint must answer 200 within three seconds; a missed
  delivery is retried at 10 s, 20 s, 30 s, 5 min, 20 min, 2 h, 4 h, 6 h and then every 6 h;
  **later events are held until the first is acknowledged**, so a failing endpoint stalls
  delivery rather than losing it, and an endpoint that exhausts its retries is disabled on the
  tab. Look first for a stuck first event and for the endpoint answering something other
  than 200: `grep '/api/qbo/webhook' /var/log/nginx/access.log | tail` (503 = the verifier
  is unset; 401 = it does not match the tab; 429 = an address sent twenty bad signatures in
  fifteen minutes; 200 = arriving). `journalctl -u wip-api | grep 'qbo webhook'` shows one
  line per stored delivery (counts and Intuit's transaction id, never a payload) and one
  line per bad signature per address per window.
- The verifier on the tab matching `QBO_ENVIRONMENT` equals `QBO_WEBHOOK_VERIFIER`, and the
  services were restarted after it was set.
- The tab's endpoint status is not "disabled"; if it is, fix the cause, re-enable, and press
  Sync now once to catch up (the poll never needed the deliveries).
- The company is Connected: a delivery for a company that is "Needs reconnect" or "Not
  connected" is stored and counted but queues nothing; an unknown realm is stored and
  ignored.
- Reading a delivery by hand (as `app_owner`, no tenant context needed):
  `SELECT received_at, realm_id, event_type, entity_id, intuit_tid FROM webhook_event ORDER BY
  received_at DESC LIMIT 20;`

### Intuit-side disconnect (F05.1)
When a user removes the app inside QuickBooks (Apps → My apps → Disconnect), Intuit sends the
browser to the registered disconnect URL and, because it is registered with `?realmId=`, fills
in the company's realm. `GET /api/qbo/disconnected?realmId=…` queues one change poll for that
company (nothing is marked on the query string alone: anyone can send it) and shows the static
"QuickBooks connection ended" page. The poll's first call finds the revoked token, the refresh
is refused, and the connection shows **Needs reconnect** with `invalid_grant`, audited: no later
than the next scheduled poll, usually within a minute. A disconnect that never reaches us ends
the same way at the next poll. Our own **Disconnect** button revokes at Intuit first, then
clears the tokens (F05); the privacy page promises exactly that.

### Rye Beach connection record (F05.1)
- **Connected** by the owner to tenant `rye-beach` on 2026-09-22 (local; times UTC): the
  production keys went into `app.env` that evening (bounded by the `qbo-sandbox` deletion at
  23:48:02Z and the first connect attempt at 23:49:54Z); connect attempts 23:49:54Z and
  23:51:06Z; tokens set and `connection_completed` at 23:51:29Z; environment `production`,
  realm `722764240` (not a secret), scope `com.intuit.quickbooks.accounting` only (the stored
  scope is asserted). First backfill 23:51:29Z to 2026-09-23 00:18:22Z (26 min 53 s), 93,529
  records fetched and 93,529 stored, `succeeded`. CDC polls every 15 minutes from 00:30Z; the
  first found 4 and stored 1. The Connections page shows Connected with month totals.
- **Tie-out, 2026-09-23 (owner)**: 13 months (Aug 2025–Aug 2026) × 4 types (invoices,
  credit memos, sales receipts, payments) against QuickBooks' own reports, equal to the cent.
  Three first-pass differences were QuickBooks export errors (a wrong date range; one report
  not run), re-exported and equal; no platform cause. Amounts and report names stay in the
  owner's workpaper. Findings for F08 / D-02 (zero-amount invoices and payments, the
  unidentified-deposits sales receipt, deleted deposit accounts, the July 2025 credit memos)
  are under Discovered in `docs/briefs/F05.1.md`; none is a tie-out difference.
- **Webhooks on production, 2026-09-25 (owner pass)**: a memo edit on one invoice was visible
  on the platform within five minutes with no Sync now; the Connections page shows "Last
  webhook received" and the 24-hour count. (The sandbox proof on jobcost.dev never ran: the
  production keys were installed first, so `qbo-sandbox` could no longer reach Intuit.)
- **Disconnect, live, 2026-09-25 (owner)**: Disconnect, then Reconnect, then one poll; the
  `connection_disconnected` audit row carries `revoked_at_source`; month totals unchanged
  after the reconnect.
- **Account numbers (export check 2026-09-25)**: the owner's QuickBooks Chart of Accounts
  export of 2026-09-23 (kept outside the repo) holds 169 accounts, every one numbered, no
  duplicate numbers. Against the F04 chart on production (160 accounts): 159 in both; in
  QuickBooks but not in the chart: 2200, 2210, 2215, 2220, 2225, 2230, 2825, 3001, 6040, 7020;
  in the chart but not in QuickBooks: 2725. Every account the F04 rules map to a division or
  to job cost is in QuickBooks, so all of them attach. Expected on the Connections page:
  attached 159 of 160, chart-only 2725, the ten above as numbered-not-in-chart, no account
  without a number; the owner confirms the figure from the page. The fixture stays as it is
  (2630 is in QuickBooks and in the production chart, not in the fixture; F04 Discovered).
  **Page, 2026-09-25 (owner)**: "Attached to QuickBooks: 169 of 169 chart accounts". The page
  counts the tenant's *active* `gl_account` rows and how many hold a QuickBooks id, so
  production's chart is the 169-account file of 2026-09-23, not the 160-account F04 workbook
  (which would read 159 of 160 with 2725 chart-only); the newest chart batch is the one
  applied, it matches QuickBooks one for one, and the ten new numbers all sit outside
  4000–5999, so the F04 rules map none of them to a division or job cost. No reload. If 6040
  is made inactive in QuickBooks the line becomes 168 of 169 with 6040 chart-only until the
  chart file marks it inactive. Owner's notes for the next chart file and oracle update (with
  2630): 2200–2230 new payroll liabilities; 2825 an older loan; 3001 Opening Balance Equity;
  6040 inactive, stays out; 7020 legal fees. "Accounts without a number: 114 of 283 active
  accounts": 283 active QuickBooks accounts, 114 with no `AcctNum` (they are not on the chart
  and attach to nothing); to explain them by group run, on the droplet:

  ```sh
  cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \
    scripts/accounts_without_number.py --tenant rye-beach
  ```

  (read-only; one group per `AccountType` with the QuickBooks id, `FullyQualifiedName` and
  `AccountSubType` of each account; no balances). "Deposit lines not linked to a document:
  1603 lines" is the F05 state of deposits (stored raw, normalized under D-02 in F08).
  **Run 2026-09-25 (owner, after deploying 753b691)**: 114 of 282 active accounts (the page
  said 283 earlier that day; one account went inactive in between). By `AccountType`:
  Expense 70, Cost of Goods Sold 12, Income 11, Other Current Liability 7, Other Expense 6,
  Other Income 3, Long Term Liability 2, Bank 1, Fixed Asset 1, Other Current Asset 1. Names
  stay out of the repo; the owner explains each group in the brief. Three of them matter to
  the platform beyond the attach count (QuickBooks ids only): the 12 unnumbered Cost of Goods
  Sold accounts (317, 411, 413, 386, 387, 477, 264, 321, 359, 86, 354, 266) and the 11 unnumbered
  Income accounts (236, 336, 1150040021, 467, 1150040004, 199, 316, 469, 158, 200, 198) are
  outside `account_map`, so any current-year posting to them is invisible to the §8.5 cost and
  revenue tie-outs until they are numbered or made inactive (F10, F13); an unnumbered Customer
  Deposits liability exists (1150040036, `DeferredRevenue`), evidence for D-02 and P0-3 (F08);
  an unnumbered Bank account (219) sits beside the numbered operating account 1010 and looks
  like a duplicate for the owner to confirm.
  The owner's explanation by group (2026-09-25) is in `docs/briefs/F05.1.md` under the
  account-number criterion: legacy overhead hierarchy, system accounts and placeholders;
  twelve legacy COGS and five legacy income lines to be numbered or deactivated before F10
  and F13; the Customer Deposits liability held for D-02; the two loans get 27xx numbers in
  the chart update; bank account 219 to be merged with or deactivated beside 1010.
- **Open**: the owner's phone pass line in `docs/briefs/F05.1.md` (the memo-edit and
  Disconnect/Reconnect checks are done). S-01 question 4 is answered ("Spike S-01").

### Recording the test fixtures
`cd backend && .venv/bin/python scripts/qbo_record_fixtures.py --tenant qbo-sandbox` writes
`tests/fixtures/qbo_sandbox/*.json` from the connected sandbox company (realm id replaced;
about twenty metered reads). It skips files that already exist; delete one to record it
again. The hand-made fixtures and the oracle are described in that folder's README.

### Spike S-01
`cd backend && .venv/bin/python scripts/s01_spike.py --tenant qbo-sandbox` prints field names
and shapes from the connected sandbox company, never values. Findings are written up in
`docs/spikes/S-01.md`. It is a throwaway and makes a few dozen metered reads per run.

**Question 4 (F05.1, the real company)**: whether a Ramp-synced Bill carries the Customer/Job
on the line. Read-only from the tenant's stored raw records, no Intuit call, no amounts
printed; run on the droplet as `wip` (the application role, tenant context set by the
script):

```sh
cd /opt/wip/backend && sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python \
  scripts/s01_q4.py --tenant rye-beach \
  --bill "J&R Concrete:202698" --bill "Cut To Fit:1171" --bill "East Coast:3919"
```

The three bills (owner, 2026-09-25; all "Bill synced to the ERP" in Ramp, Customer/Job coded
per line): J&R Concrete Foundations LLC #202698 (2026-09-15, 1 line, 5240); Cut To Fit Co LLC
#1171 (2026-09-20, 4 lines, 5240); East Coast Landscape Supply #3919 (2026-09-18, 7 lines,
5135, the D-30 pool). For each the script prints the QuickBooks Bill `Id`, the line count and,
per line, whether `AccountBasedExpenseLineDetail.CustomerRef` is present with its value and
name. A bill reported `NOT IN raw_record` means the poll has not fetched it yet: press
**Sync now** once, wait for the poll, run again. Only the ids go into BLUEPRINT §13.7.

Answered 2026-09-25 (owner, on production after deploying 5e16391): **yes**, every line
`AccountBasedExpenseLineDetail` with `CustomerRef`: #202698 → Bill Id 98892, CustomerRef
100000091, 1 of 1 lines; #1171 → Bill Id 98908, CustomerRef 6087, 4 of 4; #3919 → Bill Id
98890, CustomerRef 6415 (`Pool:Pool - Hydroseed`), 7 of 7. The name arrives as
`FullyQualifiedName` (`Parent:Project`). The pool project is spelled `Pool - Hydroseed` in
QuickBooks (hyphen). Purchases are checked when Ramp card transactions begin (F10).

## Production (jobcost.dev) (F05.0, D-27)

One DigitalOcean droplet (`jobcost`, Ubuntu 24.04, `s-1vcpu-2gb`, NYC1) runs nginx, the
API and the worker; Managed Postgres 16 (`jobcost-db`, NYC1, private hostname, trusted
source = the droplet) is the database; the private Space `jobcost-files` (NYC3) is the
object store. No containers on the server. The owner deploys by hand over ssh. Everything
below is reproducible from the repo by two scripts.

### Environments (F05.1)

| Environment | Where | Env file | Intuit keys | Webhooks |
|---|---|---|---|---|
| Mac (development) | the laptop, compose Postgres on 5433 | repo-root `.env` (never committed) | **Development** keys, `QBO_ENVIRONMENT=sandbox`, the sandbox company on tenant `qbo-sandbox` | none (no public host; `QBO_WEBHOOK_VERIFIER` unset) |
| CI | GitHub Actions | `tests/_env.py` (throwaway keys and verifier per run) | none: Intuit is never reached; `FakeIntuit` answers | signed by the throwaway verifier |
| Production | the droplet, `jobcost.dev` | `/etc/wip/app.env` (API and worker), `/etc/wip/migrate.env` (owner URL, root only) | **Production** keys, `QBO_ENVIRONMENT=production`, Rye Beach on tenant `rye-beach`; no sandbox tenant after F05.1 | Production tab, verifier in `app.env` |

One key pair per deployment: the server holds either the Development or the Production
keys, never both, and `QBO_ENVIRONMENT` says which. A connection made under the other
environment shows **Needs reconnect** with `environment_mismatch` at its next poll and stops
(never retried); disconnect it, or delete its tenant.

### Deleting a tenant (F05.1, D-28)
An operator action on the server, never through the API, and the one time the append-only
triggers are suspended (for that transaction only; `prod_check.py` proves they are back).
Run in a quiet moment: the script holds an exclusive lock on `audit_log` and `raw_record` for
the seconds the deletion takes, and API requests writing audit rows wait for it.

```sh
cd /opt/wip/backend
set -a && . /etc/wip/migrate.env && set +a
ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/delete_tenant.py <slug>
```

The owner URL comes from the root-only migration file; the key ring, the Intuit keys and the
Space key come from `app.env` (to revoke the tokens and delete the objects). The script:
1. refuses a slug in `PROTECTED_TENANT_SLUGS` (production: `rye-beach`) and refuses to run
   as a role that does not own the tenant tables (`app_rw` stops with one sentence);
2. prints the row count per tenant table and the object count under `tenant/{id}/`, and
   asks for the slug typed back exactly; anything else stops it, nothing changed;
3. revokes the QuickBooks tokens at Intuit if the tenant holds any (best effort: with the
   server on the other environment's keys the revoke fails and is reported, which is why
   the runbook says to press **Disconnect** on the tenant *before* a key swap);
4. deletes the objects under the tenant's prefix in the Space;
5. in one transaction, as the owner with the tenant context set: disables the append-only
   triggers on the tenant tables that have them, deletes every tenant table's rows in
   foreign-key order (derived from the catalog), re-enables the triggers, clears sessions
   pointing at the tenant, deletes the `tenant` row, and writes one `firm_audit_log` row
   (`tenant_deleted`: slug, rows per table, objects, operator). A failure anywhere rolls
   everything back, triggers included.

Afterwards run `prod_check.py`; "append-only triggers present and enabled" must be `ok`.
The audit row survives because it is firm-level: `SELECT occurred_at, detail FROM
firm_audit_log WHERE action = 'tenant_deleted' ORDER BY occurred_at DESC;` as `app_owner`.

**First use, `qbo-sandbox` on production (F05.1, step 3)**: 2026-09-22 23:48:02Z, by the
owner from the CLI as root (`firm_audit_log` `tenant_deleted`), just before the production
keys went in. Rows removed: `raw_record` 286, `billing_line` 63, `task` 55, `billing` 35,
`customer` 30, `sync_run` 28, `payment` 22, `payment_application` 22, `cost_category` 14,
`audit_log` 13, `connection` 1, `membership` 1, `import_batch` 1; every other tenant table 0;
1 Space object. `prod_check.py` passed afterwards ("append-only triggers present and
enabled" `ok`).

### Server layout, and who may read what

| Path | Owner, mode | What |
|---|---|---|
| `/opt/wip` | `wip:wip` 755 | The checkout (`git`, detached at the last ref `deploy.sh` checked out). `.deployed` holds the sha the services were last restarted on, which is what is serving even after a failed deploy. `backend/.venv` is the venv; `frontend/dist` is the served build (`dist.prev` the one before, `dist.new` a build in progress); all three and `.deployed` are gitignored. |
| `/etc/wip/app.env` | `wip:wip` 600 (directory `root:wip` 750) | The API and worker environment: `DATABASE_URL` (`app_rw`), the key ring, the Space key, the Intuit keys. Read by systemd and by hand-run scripts as `wip`. **Never** `DATABASE_OWNER_URL`. |
| `/etc/wip/migrate.env` | `root:root` 600 | `DATABASE_OWNER_URL` (`app_owner`) and `PROTECTED_DATABASE_NAMES`. Read only by `deploy.sh`, as root, around `alembic upgrade head`. |
| `/etc/systemd/system/wip-api.service`, `wip-worker.service` | root | From `deploy/systemd/`. `User=wip`, `EnvironmentFile=/etc/wip/app.env`, `Restart=always`, `ProtectSystem=strict` (the processes write nowhere but `/tmp`). |
| `/etc/nginx/sites-available/jobcost.dev`, `snippets/wip-security-headers.conf`, `snippets/wip-tls.conf` | root | From `deploy/nginx/`. |
| `/etc/letsencrypt/live/jobcost.dev/` | root | The certificate; `certbot.timer` renews it and the deploy hook reloads nginx. |
| `/home/wip/.ssh/id_ed25519` | `wip` 600 | The read-only deploy key for the repository. |
| `/var/www/certbot` | root | ACME webroot. |
| `/swapfile` | root 600 | 2 GB swap (the frontend build's margin). |
| `/run/lock/wip-deploy.lock` | root | `deploy.sh`'s lock. |

Logs are in journald: `journalctl -u wip-api -u wip-worker -f` (`-n 200` for the last
lines; `--since "1 hour ago"`). nginx: `/var/log/nginx/access.log`, `error.log`.

### First-time set-up, in order

Console work first (done 2026-09-21): project, droplet with SSH key only, Managed Postgres
with the droplet as its only trusted source (added as the droplet resource, not as its
public IP: the droplet reaches the cluster over the VPC address), the Space and one
access key, the cloud
firewall (22 from the owner's IP, 80 and 443 from anywhere), Namecheap A records for `@`
and `www`, the Intuit redirect URI `https://jobcost.dev/api/qbo/callback`.

1. **`deploy/setup.sh`** as root on the droplet. From the Mac:
   ```sh
   scp deploy/setup.sh root@174.138.33.185:/root/setup.sh
   ssh root@174.138.33.185 bash /root/setup.sh
   ```
   Idempotent: each step prints `done:` or `skipped:`; run it again after any failure. It
   generates the `wip` user's deploy key and, if the repository is private, stops at the
   clone with the public key printed: add it under GitHub → repository → Settings → Deploy
   keys (read-only), then run it again. It ends by getting the certificate for
   `jobcost.dev` and `www.jobcost.dev` (DNS must already point at the droplet) and says what
   to do next. It does not run migrations and does not start the services.
   Re-run it after a change under `deploy/nginx/` or `deploy/systemd/` (after the
   `deploy.sh` that checked the change out): it installs what differs and reloads.
2. **Fill the two environment files** on the droplet, as root, with a plain editor
   (`nano /etc/wip/app.env`, `nano /etc/wip/migrate.env`). Every blank is a secret the
   owner supplies; nothing else needs changing. Generate the key ring on the server:
   ```sh
   python3 -c "import os,base64;print('prod1:'+base64.b64encode(os.urandom(32)).decode())"
   ```
   and paste the output as `CRYPTO_KEYS=` (the id `prod1` matches `CRYPTO_ACTIVE_KEY_ID`).
   The key never leaves the server and is never pasted into chat, e-mail or a ticket.
   Passwords in the two URLs: letters and digits only, so nothing needs URL-encoding.
   Both URLs use the cluster's **private** hostname, port 25060, `?sslmode=require`,
   database `wip`. Then check: `stat -c '%U:%G %a' /etc/wip/app.env /etc/wip/migrate.env`
   → `wip:wip 600` and `root:root 600`.
3. **Roles and the database on the cluster**, from the droplet (the cluster trusts only
   the droplet; `psql` is installed by setup.sh). Choose the two role passwords first
   (`python3 -c "import secrets;print(secrets.token_hex(24))"` twice), put them in the
   two URLs of step 2, then:
   ```sh
   PGSSLMODE=require PGHOST=<private hostname> PGPORT=25060 PG_MAINTENANCE_DB=defaultdb \
   POSTGRES_USER=doadmin PGPASSWORD='<doadmin password>' \
   APP_OWNER_PASSWORD='<app_owner password>' APP_RW_PASSWORD='<app_rw password>' \
   APP_DATABASES=wip bash /opt/wip/db/init/01_roles.sh
   ```
   `PG_MAINTENANCE_DB=defaultdb` because the managed cluster has no `postgres` database.
   If the connection is refused or times out, check the cluster's trusted sources: the
   entry must be the **droplet as a resource** (chosen from the list of droplets), not
   its public IP address, because connections from the droplet arrive over the VPC
   address, not the public one.
   If `CREATE DATABASE wip OWNER app_owner` is refused with "must be able to SET ROLE
   app_owner" (Postgres 16 on a managed cluster, where `doadmin` is not a superuser), run
   `psql "sslmode=require host=<private hostname> port=25060 user=doadmin dbname=defaultdb" -c 'GRANT app_owner TO doadmin;'`
   and run the script again (it skips what exists). Verify and record the result:
   ```sql
   SELECT rolname, rolsuper, rolbypassrls, rolcreatedb FROM pg_roles WHERE rolname IN ('app_owner','app_rw');
   -- app_owner f f t ; app_rw f f f
   SELECT datname, pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'wip';  -- app_owner
   ```
   Nothing in the repo ever holds these passwords; the two env files on the droplet do.
   **Recorded 2026-09-22 (first run on `jobcost-db`)**: `app_owner` rolsuper f, rolbypassrls f;
   `app_rw` rolsuper f, rolbypassrls f; database `wip` present. It took two server-side
   fixes to get there: the trusted source had to be the droplet as a resource (the public-IP
   entry did not admit the VPC connection) and `PG_MAINTENANCE_DB=defaultdb`. The password
   in `DATABASE_URL` must be the one given to `01_roles.sh`: on the first deploy they
   differed, the health check answered 503 with `"db":"unavailable"`, and
   `ALTER ROLE app_rw PASSWORD '…'` as `doadmin` plus a restart fixed it.
4. **First release**: `bash /opt/wip/deploy/deploy.sh` (below). It migrates, builds, starts
   both services and checks `https://jobcost.dev/api/health`.
5. **Production data set-up** (the owner, from the droplet as `wip`; each command is the
   same audited service the API uses, actor `cli`):
   ```sh
   cd /opt/wip/backend
   sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/create_user.py bootstrap \
       --firm-name "<the practice>" --email <owner e-mail> --display-name "<name>"
   # open the printed link on the phone: password → QR code → confirm → recovery codes
   sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/create_user.py create-tenant --name "Rye Beach Landscaping" --slug rye-beach
   sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/create_user.py create-tenant --name "QBO Sandbox" --slug qbo-sandbox
   sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/create_user.py add-entry --email <owner e-mail> --tenant rye-beach
   sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/create_user.py add-entry --email <owner e-mail> --tenant qbo-sandbox
   ```
   Then in the browser: switch to QBO Sandbox → Connections → Connect to QuickBooks with the
   Development keys (the sandbox company works from any host); Imports → upload one file
   and download it (the D-21 record below).
6. **`prod_check.py`** (below), and after the owner's first sign-in from outside, the
   client-IP check: as `app_owner` (`psql` with the owner URL, no tenant context needed)
   `SELECT ip, at FROM firm_audit_log WHERE action = 'login_success' ORDER BY at DESC LIMIT 1;`
   must show the owner's public address, not `127.0.0.1` (`TRUSTED_PROXY_COUNT=1`).

### Deploying a release

```sh
ssh root@174.138.33.185 bash /opt/wip/deploy/deploy.sh            # origin/main
ssh root@174.138.33.185 bash /opt/wip/deploy/deploy.sh <sha|ref>  # a specific commit
```

Under a lock (`flock`; a second run says "another deploy is running" and exits 1):
reads the serving commit from `/opt/wip/.deployed`; `git fetch` and a detached checkout of the ref as `wip`; `pip
install -e` into the venv; `npm ci` and a Vite build into `frontend/dist.new` (the served
`dist` is untouched); **`alembic upgrade head` as root from `/etc/wip/migrate.env`, before
any restart**, inside `timeout 600`; then the frontend swap (`dist` → `dist.prev`,
`dist.new` → `dist`); `systemctl restart wip-api wip-worker`; up to ten `curl`s of
`https://jobcost.dev/api/health` two seconds apart, requiring `"status":"ok"` and
`"db":"ok"`. It prints `deployed <sha>` and `free -m` before and after the build (move to
`s-2vcpu-2gb` if the build swaps). The restart step writes the sha to `/opt/wip/.deployed`
(owned by `wip`); the "serving" and "was serving" lines read that file, not the checkout,
because after a failed deploy the checkout sits at the ref that failed while the previous
release keeps running (the deploy-check of 2026-09-22 showed the checkout's sha as
"serving" before this change). A failure prints the step, the commit that was serving
and the roll-back command. **A failed migration stops the script before the restart**: the
old release keeps serving, new code is on disk, the database is unchanged (Alembic runs the
migrations in one transaction). Fix the migration and run `deploy.sh` again, or go back.

Every `git` command in `deploy.sh` runs as `wip`, the checkout's owner (`git_wip`), so root
needs no `safe.directory` entry: git refuses a repository owned by another user as
"dubious ownership", which is what stopped the first deploy on 2026-09-22 when root ran
`rev-parse`. The `safe.directory /opt/wip` entry added by hand that day in root's global git
config is harmless and unused; `git config --global --unset-all safe.directory` removes it.

The health step tells the outcomes apart: **no answer** (nginx or `wip-api` not up: read the
journal), a **503 with `"db":"unavailable"`** (the API is up but cannot reach the database:
check `DATABASE_URL` in `/etc/wip/app.env`, in particular that the `app_rw` password equals
the role's, then restart both services), or another status. Each message ends with the
`journalctl` line to read.

### Rolling back

`bash /opt/wip/deploy/deploy.sh <previous sha>` (the sha `deploy.sh` printed as "was
serving"). Migrations are **forward-only in production**: a roll-back checks out the older
code and runs `alembic upgrade head`, which changes nothing, so the schema stays at the
newer revision. Every migration is written so that the previous release runs against it
(additive columns are nullable or defaulted); if a migration is not, its docstring says so
and the roll-back is a forward fix. `alembic downgrade` against `wip` is refused by the
guard (`PROTECTED_DATABASE_NAMES=wip` in both env files).

### Proving the failed-migration path (scratch database, never production)

Acceptance criterion 3 of F05.0, run once after the first successful deploy and again
whenever `deploy.sh` changes. The check points `deploy.sh` at a **scratch database on the
managed cluster** with a deliberately broken migration; the production database is never
touched, and the scratch database is dropped afterwards.

The branch and its commit are made in the checkout, `/opt/wip`, as user `wip` (the
checkout's owner; root's git refuses it as "dubious ownership"). The branch never leaves
the droplet and is deleted in step 6.

```sh
# 1. a scratch database on the cluster (roles exist already; the script skips them)
PGSSLMODE=require PGHOST=<private hostname> PGPORT=25060 PG_MAINTENANCE_DB=defaultdb \
POSTGRES_USER=doadmin PGPASSWORD='…' \
APP_OWNER_PASSWORD='<app_owner password>' APP_RW_PASSWORD='<app_rw password>' \
APP_DATABASES=wip_scratch bash /opt/wip/db/init/01_roles.sh

# 2. a root-only migrate file for it (same owner URL as /etc/wip/migrate.env, database wip_scratch)
install -m 600 -o root -g root /dev/null /root/migrate-scratch.env
printf 'DATABASE_OWNER_URL=postgresql+psycopg://app_owner:<password>@<private hostname>:25060/wip_scratch?sslmode=require\nPROTECTED_DATABASE_NAMES=wip\n' > /root/migrate-scratch.env

# 3. a local branch with a migration whose upgrade() raises (never leaves the droplet)
cd /opt/wip/backend
HEAD_REV=$(sudo -u wip .venv/bin/alembic heads | awk '{print $1}')
sudo -u wip git -C /opt/wip checkout -b deploy-check
sudo -u wip tee alembic/versions/9999_deploy_check.py >/dev/null <<EOF2
"""deploy-check: fails on purpose (F05.0 criterion 3). Never merged."""
revision = "9999"
down_revision = "$HEAD_REV"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raise RuntimeError("deploy-check: this migration fails on purpose")


def downgrade() -> None:
    pass
EOF2
sudo -u wip git -C /opt/wip add backend/alembic/versions/9999_deploy_check.py   # -a stages no new file
sudo -u wip git -C /opt/wip -c user.name=deploy-check -c user.email=deploy-check@jobcost.dev commit -qm "deploy-check"

# 4. before: what is running and what is served
systemctl show -p ActiveEnterTimestamp wip-api wip-worker; ls -ld /opt/wip/frontend/dist

# 5. the deploy must fail at step "migrate" with the services and dist untouched
MIGRATE_ENV_FILE=/root/migrate-scratch.env bash /opt/wip/deploy/deploy.sh deploy-check; echo "exit $?"
systemctl show -p ActiveEnterTimestamp wip-api wip-worker; ls -ld /opt/wip/frontend/dist

# 6. back to main (a normal deploy, against the production migrate.env), then clean up
bash /opt/wip/deploy/deploy.sh
sudo -u wip git -C /opt/wip branch -D deploy-check
rm -f /root/migrate-scratch.env
psql "sslmode=require host=<private hostname> port=25060 user=doadmin dbname=defaultdb" -c 'DROP DATABASE wip_scratch;'
```

Expected: step 5 exits non-zero at `deploy failed at step: migrate`, both
`ActiveEnterTimestamp` values are the same before and after, `dist`'s timestamp is
unchanged, and `psql … -d wip_scratch -c '\dt'` shows nothing (the migrations rolled back).
While the checkout is at `deploy-check` the running release is still the previous one;
"serving" in step 6's output is read from `/opt/wip/.deployed`, not from the checkout.

**Run 2026-09-22** (owner, after the first deploy; criterion 3 of F05.0): step 5 stopped
with `deploy failed at step: migrate`, exit 1; `wip-api` and `wip-worker`
`ActiveEnterTimestamp` (08:45:33 / 08:45:34 UTC) and `frontend/dist` (08:40) were the same
before and after; Alembic reported transactional DDL, so `wip_scratch` rolled back. Step 6
redeployed `origin/main` (0b703bb) with exit 0; the branch was deleted, the scratch env file
removed and `wip_scratch` dropped. Two things found and fixed in the run's wake: the commit
in step 3 needs the `git add` above (`-a` had left the new file unstaged), and "serving"
had shown the deploy-check commit (now read from `.deployed`).

### Restarting, and hand-run scripts

`systemctl restart wip-api` (or `wip-worker`); `systemctl status wip-api wip-worker`. Both
are enabled and start at boot. A crashed process is restarted after 2 s (`Restart=always`).
Stopping the worker waits up to one task lease (330 s) for a task in flight.

Scripts on the server run as `wip` with the API's environment file:

```sh
cd /opt/wip/backend
sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/create_user.py …
sudo -u wip ENV_FILE=/etc/wip/app.env .venv/bin/python scripts/reencrypt.py --dry-run
```

`make` targets are for development; on the server use the commands above.

### Certificate renewal

`certbot.timer` (installed with the package) runs `certbot renew` twice a day; the
certificate's renewal file carries `renew_hook = systemctl reload nginx` from
`setup.sh`'s `--deploy-hook`. Check: `systemctl list-timers certbot.timer` and
`certbot renew --dry-run` (no certificate is changed). Expiry: `certbot certificates`.

### Backups (Managed Postgres)

DigitalOcean takes daily backups of the cluster with 7-day retention, on by default.
Verify in the console (Databases → jobcost-db → Backups) and record the date checked here:
_not yet verified_. The restore drill is F23. The Space is not backed up by the provider
beyond its own durability; uploaded files are also held as raw records where a source
kind parses them.

### `prod_check.py` after every deploy

```sh
sudo -u wip ENV_FILE=/etc/wip/app.env /opt/wip/backend/.venv/bin/python /opt/wip/backend/scripts/prod_check.py
```

Read-only. Connects as the application does (`DATABASE_URL`, `app_rw`) and prints one line
per check: role is `app_rw`, `NOSUPERUSER`, `NOBYPASSRLS`, owns no table; connection over
SSL; every tenant table has RLS enabled and forced with `tenant_isolation`; no policy outside
the D-11 allow-list; append-only triggers present and enabled on each of
`APPEND_ONLY_TABLES` and the register matches; a HEAD on the Space's bucket succeeds. Exit 1
on any `FAIL:` line. It never prints a URL, key, hostname or row. The same catalog queries
(`app/tenancy/catalog.py`) are what `tests/test_rls.py` and `tests/test_append_only.py`
enforce in CI. Run after the first deploy (2026-09-22, commit 81158c2 plus the two fixes
above) and again after the last deploy of F05.0 (2026-09-22, commit 0b703bb, database
`wip` at 0008); both passed. Output of the latter, verbatim (exit 0):

```
ok: role is app_rw, NOSUPERUSER, NOBYPASSRLS
ok: role owns no table
ok: connection over SSL
ok: tenant tables enumerated
ok: RLS enabled and forced with tenant_isolation on every tenant table
ok: no policy outside the D-11 allow-list
ok: append-only triggers present and enabled
ok: append-only register matches the catalog
ok: object store answers a HEAD on the bucket
all 9 checks passed
```

### Firewall

DigitalOcean cloud firewall on the droplet: inbound 22 from the owner's IP only, 80 and 443
from anywhere, nothing else. The Managed Postgres cluster's only trusted source is the
droplet, so port 25060 is unreachable from the internet. uvicorn binds `127.0.0.1:8000`
only. Check from the Mac: `nc -vz -w 5 174.138.33.185 443` succeeds, `… 8000` and
`… 5432` fail, and `nc -vz -w 5 <public db hostname> 25060` fails.

## Brand (F05.2)

One image is the brand. `frontend/public/brand/logo.svg` is the only hand-made picture in
the repo (the owner's logo, committed as supplied on 2026-10-02 in place of the original
hammer; it carries its own fill colours, which `brand.json` does not govern). Beside it,
optionally, `logo-mark.svg`: a simplified mark used for the 16 and 32 px favicons only,
for a logo whose detail does not read at 16 px. There is none at present: the current logo
reads at 16 px as it is, so those two favicons come from `logo.svg` (the script says which it
used). And `brand.json`
(`background` `#fff`, `foreground` `#000`, `padding` 0.15), hand-copied from
`styles.css` and not following it (`--accent` is never used in the brand assets). The
wordmark and the manifest name come from `SITE_HOST` in `frontend/src/product.js`, which
since D-33 is `PRODUCT_NAME` itself: the product is named `jobcost.dev`, one string with
the hostname (D-09 closed).

`npm run brand` (in `frontend/`; `scripts/make-brand.mjs`, rasterised by the `sharp`
devDependency) writes into `frontend/public/`: `favicon.svg`, `favicon-16.png`,
`favicon-32.png`, `favicon-48.png`, `favicon.ico`, `apple-touch-icon.png`, `icon-192.png`,
`icon-512.png`, `icon-512-maskable.png`, `site.webmanifest`, `og-image.png` and
`brand-manifest.json` (the SHA-256 of each input and the file list). All of them are
committed; CI and `deploy.sh` never run the script. CI's frontend job and
`frontend/tests/brand.test.js` both fail when an input changed and the outputs were not
regenerated, naming `brand-manifest.json`. The OG wordmark is set in the font the
generating Mac resolves for `"Helvetica Neue", Helvetica, Arial, sans-serif`; the committed
PNG shows that rendering.

Swapping the logo:

1. Overwrite `frontend/public/brand/logo.svg` (an SVG with a `viewBox`; nothing else is
   required). If the new logo does not read at 16 px, add `logo-mark.svg`, a matching
   simplified mark; without it the 16 and 32 px favicons come from `logo.svg` itself, and the
   script says so when it runs. If a mark exists and the logo changes, replace or delete it to
   match.
2. `cd frontend && npm run brand`, then look at `public/og-image.png` and
   `public/favicon-16.png`.
3. `npm test`, commit the sources and the outputs together, and deploy as usual
   (`deploy.sh` builds `dist/` from the committed files).

Changing a colour or the padding is the same procedure starting from `brand.json`. Changing
the name starts from `PRODUCT_NAME` (both sides); `SITE_HOST` follows it, and
`frontend/index.html` reads both through `%PRODUCT_NAME%` and `%SITE_HOST%`, filled by
`vite.config.js` in dev and in the build, so there is nothing to edit there. If the name and
the hostname ever diverge, `SITE_HOST` gets its own string and `tests/test_product_name.py`
its own rule.
