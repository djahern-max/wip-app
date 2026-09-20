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

_First manual upload/download against Spaces: not yet done._

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
who decided, when, and the decision reference. Decide or Change → value + reference →
Record decision. Keys: `timezone`, `fiscal_year_start_month`, `wip_basis` (cost
category slots), `small_job_threshold` (an amount, entered as text), 
`deposit_identification`, `fuel_surcharge_treatment`. **No key has a default**: a
feature that needs an undecided key stops with a message naming it rather than
assuming a value. Every change is audited `policy_set` with before and after.

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
  rate.

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
  Changing company is a deliberate two-step: Disconnect, then Connect.
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

### Spike S-01
`cd backend && .venv/bin/python scripts/s01_spike.py --tenant qbo-sandbox` prints field names
and shapes from the connected sandbox company, never values. Findings are written up in
`docs/spikes/S-01.md`. It is a throwaway and makes a few dozen metered reads per run.
