# DECISIONS

Append-only. One entry per decision, newest last. Open items and recommendations
live in BLUEPRINT §14 until decided here.

Format:

```
## D-xx · Title · YYYY-MM-DD
**Decision**: …
**Reasoning**: …
**Affects**: features / BLUEPRINT sections
```

---

_D-01 … D-09 are open (BLUEPRINT §14); P0-5 records them here._

## D-10 · 2026-09-17 · Authentication is built in the platform, not bought
**Decision**: Email + password (argon2id), TOTP mandatory for `firm_admin` and `firm_staff`, server-side sessions stored in Postgres and carried by an `HttpOnly`, `Secure`, `SameSite=Lax` cookie. No hosted identity provider.
**Reasoning**: BLUEPRINT §11 left this open. There is no self-serve signup and the user count is small. Membership, roles, and tenant switching live in the platform's database either way, so a hosted provider would add an external dependency and a vendor without removing the authorization work. Accepted cost: the platform owns password reset, lockout, recovery codes, and secret rotation.
**Affected**: F02. BLUEPRINT §11 (Auth). New dependencies `argon2-cffi`, `pyotp`, `cryptography`, frontend `qrcode`. New global table `session`. Revisit if a client requires SSO.

## D-11 · 2026-09-17 · A user reads their own memberships through a second RLS policy
**Decision**: `membership` carries one additional policy, `FOR SELECT` only, `USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)`, created by the migration helper `allow_own_membership_read()`. `app.user_id` is set transaction-locally by the session dependency. All writes to `membership` still pass only through `tenant_isolation`. The RLS enumeration test holds an explicit allow-list of extra policies and fails on any policy not listed. No other table may carry an extra policy without a new decision.
**Reasoning**: With RLS forced, login and the tenant switcher see zero memberships before a tenant is chosen. A bypass role is ruled out by the CLAUDE.md tenancy rules. A `SECURITY DEFINER` function would work but sits outside what the enumeration test inspects; a policy is visible in the catalog and testable.
**Affected**: F02. BLUEPRINT §11 (Isolation). CLAUDE.md Tenancy section (amended). `tests/test_rls.py`.

## D-12 · 2026-09-17 · Two audit tables
**Decision**: `audit_log` is tenant-scoped (`tenant_id NOT NULL`, RLS enabled and forced) and holds events that belong to one tenant. `firm_audit_log` is global, keyed by `firm_id`, readable only by firm roles, and holds events with no tenant: logins, lockouts, TOTP and password events, user creation. Entering a tenant writes `tenant_enter` to that tenant's `audit_log`, so a `client_admin` can see who accessed their company.
**Reasoning**: The alternative, one table with a nullable `tenant_id`, breaks the `tenant_id NOT NULL` hard rule and complicates the isolation policy. Two tables keep the tenant rule without exception and keep firm-level security events out of client view.
**Affected**: F02 and every later feature that writes audit events (§11 Audit list). `firm_audit_log` and `session` join `firm`, `tenant`, `user` as the approved tables without `tenant_id`.

## D-13 · 2026-09-17 · Insert-only tables are enforced by trigger
**Decision**: Migration helper `make_append_only(table)` installs a `BEFORE UPDATE OR DELETE` row trigger and a `BEFORE TRUNCATE` statement trigger that raise. Tables using it are listed in `APPEND_ONLY_TABLES`; a CI test fails if a listed table lacks the triggers and proves the statements fail as `app_rw` and as `app_owner`. First applied to `audit_log` and `firm_audit_log`; to be applied to the WIP snapshot tables when they are created.
**Reasoning**: F01 grants `app_rw` DML on all future tables through default privileges so that migrations never name the app role. Per-table `REVOKE` would undo that. A trigger is role-agnostic, cannot be removed by a non-owner, and can be enumerated by a test in the same way as RLS.
**Affected**: F02; the period snapshot features under BLUEPRINT §8 (approved periods immutable, snapshot tables insert-only). CLAUDE.md Tenancy and Source-of-truth sections (amended).

## D-14 · 2026-09-17 · F01 dependencies ratified; dependency rule restated
**Decision**: `pydantic-settings` and `httpx` (dev), added during F01 without a prior go-ahead, are approved. The stop-and-ask rule applies to every third-party dependency regardless of size; a feature brief may pre-approve named dependencies.
**Reasoning**: Both are the standard companions of the mandated stack and carry no design consequence. The rule is restated because it was reported after the fact rather than asked.
**Affected**: F01 CHANGELOG entry (no change needed). CLAUDE.md Workflow rule 2 (unchanged; this entry is the reminder).

## D-15 · 2026-09-17 · Firm authority is an explicit row in `firm_membership`
**Decision**: New global table `firm_membership(firm_id, user_id, role ∈ {firm_admin, firm_staff})`. A user's firm role comes only from this table. For such a user, the per-tenant `membership` row grants entry to that tenant and carries no authority of its own; entering a tenant still requires one, so the client's `audit_log` shows `membership_created` and `tenant_enter` for every firm user who opens their books. Only a `firm_admin` writes `firm_membership`; every write is audited to `firm_audit_log`; the last `firm_admin` of a firm cannot be removed or demoted. `firm_audit_log.firm_id` stays nullable, because a failed login for an unknown e-mail has no firm under any model.
**Reasoning**: F02 derived firm authority from `membership`: the highest firm role on any one tenant applied to every tenant of the firm. One wrong role on one client would grant firm-wide administration, per-tenant firm roles did not mean what the schema implied, and bootstrap needed a tenant to exist before an admin could. An explicit row removes all three. Done now because the auth tables hold no real users.
**Affected**: F02.1. BLUEPRINT §11 (Shape, Roles). CLAUDE.md approved global tables. Supersedes the derivation described in the F02 CHANGELOG entry; that entry's follow-up claiming D-15 makes `firm_id` NOT NULL is corrected in the F02.1 entry.

## D-16 · 2026-09-17 · Accounts are activated only through an admin-issued one-time link
**Decision**: Users are created without a password. A `firm_admin` issues a single-use link (stored hashed, 72-hour expiry) that sets the password and, for firm users, enrols TOTP and issues recovery codes in one flow. A firm user with no TOTP enrolled cannot log in by password. Admin password reset and admin TOTP reset both work by issuing a new link. All of the user's sessions end on link redemption, password change, TOTP reset, and any `firm_membership` change.
**Reasoning**: Under F02 a firm user who had not yet enrolled could have TOTP enrolled by anyone holding their password, which defeats the second factor exactly when it matters. Watching the audit log for `totp_enrolled` detects that after the fact; the link prevents it. No e-mail is sent; the admin hands the link over directly, which is workable at this user count.
**Affected**: F02.1. BLUEPRINT §11 (Auth). Amends D-10 (enrolment path only; the rest of D-10 stands).

## D-17 · 2026-09-17 · One CPA practice per deployment
**Decision**: The platform serves one practice (the owner's) and its client companies. Rye Beach Landscaping is the first tenant. `firm` and every `firm_id` column stay in the schema so hosting a second practice is not foreclosed, but tables without tenant scope (`user`, `tenant`, `session`, `firm_membership`, `firm_audit_log`) are not isolated between firms at the database level, and there is no operator role above `firm_admin`. If another practice is ever served, the default is a separate deployment with its own database. Revisit before any second practice shares a database.
**Reasoning**: BLUEPRINT §11 defines `firm` as the owner's practice and rules out self-serve signup; nothing in the ROADMAP onboards or bills a firm. Job cost and WIP data are isolated per tenant by RLS regardless of firm, so the only exposure in a shared database would be the global tables, and building firm-level isolation for a case that is not planned would be cost without a user.
**Affected**: F02.1 (null-`firm_id` audit rows restricted to `firm_admin` as a low-cost guard). BLUEPRINT §11. Phase E (second tenant) is unaffected: it proves a second client, not a second firm.

## D-18 · 2026-09-17 · Reading another user's memberships from an admin request swaps `app.user_id` through one helper
**Decision**: One context-manager helper, `read_as_user()` in `app/core/auth.py`, sets `app.user_id` to the target user for the block and restores the actor's id in a `finally` block. It is callable only from the admin service (`app/auth/admin.py`), after the firm-role check, and its results are filtered to tenants of the actor's firm. Tests prove that `app.user_id` equals the actor's id after normal exit and after an exception inside the block, that an `INSERT`, `UPDATE` or `DELETE` on `membership` inside the block without tenant context still fails or matches nothing, and that no other module calls the helper.
**Reasoning**: A `firm_admin` creating a firm membership, or removing one, must know the target's client-role rows and entry rows across the firm's tenants. The D-11 policy already lets the server read a user's own rows once `app.user_id` names them (login and link redemption do this). Looping over every tenant of the firm with a tenant context each would cost one query per tenant and touch every tenant's rows for one user; the swap is one query and reads exactly that user's rows. Confining it to one helper with a guaranteed restore keeps the request from ever acting for the wrong user.
**Affected**: F02.1. CLAUDE.md Tenancy section (one sentence naming the helper and its location; pending owner approval of the wording).

## D-18a · 2026-09-17 · D-18 wording approved
**Decision**: The CLAUDE.md Tenancy sentence naming `read_as_user()` was approved by the owner as proposed and applied in commit bdc347a. The "pending owner approval" note in D-18's Affected line is superseded by this entry.
**Reasoning**: Append-only log; the original entry stands as written.
**Affected**: D-18. CLAUDE.md Tenancy section.

## D-19 · 2026-09-17 · The worker queue is tenant-scoped and the worker polls one tenant at a time
**Decision**: `task` is a tenant table like any other (`tenant_id NOT NULL`, RLS enabled and forced, `tenant_isolation` only). The worker reads the tenant list from the global `tenant` table and, for each tenant, opens `tenant_session(engine, tenant_id)` and claims at most one due task with `FOR UPDATE SKIP LOCKED`. One transaction never holds two tenant contexts. `LISTEN/NOTIFY` with a payload of the tenant id only wakes the loop early; polling is the fallback. Task functions must take `tenant_id` as their first parameter, enforced at registration. Task payloads hold ids, never source data.
**Reasoning**: With RLS forced, a worker with no tenant context sees zero tasks. The alternatives were a global queue table (a new table without `tenant_id`, holding references into every tenant) or a second extra RLS policy for a worker setting (D-11 says no further extra policies without a decision, and any session could set it). Polling per tenant needs no exception to any tenancy rule, and at 10–30 tenants the cost is a few trivial queries every few seconds. BLUEPRINT §12 asks for a Postgres-backed queue with no Redis; this adds no dependency.
**Affected**: F03 and every later feature that enqueues work (F05 sync, F06/F11 imports, report rendering). BLUEPRINT §11 (background jobs take a tenant id explicitly), §12. CLAUDE.md Tenancy (worker rule, unchanged). Revisit if tenant count makes the poll loop measurable, or if a task that belongs to no tenant is ever needed.

## D-20 · 2026-09-17 · `raw_record` is insert-only; a change is a new version
**Decision**: Each `(tenant, source, entity_type, external_id)` has numbered versions. A new payload whose hash equals the latest version's writes nothing; a different payload writes `version + 1`; a source-reported delete writes a new version flagged `is_deleted`. No row is ever updated or deleted: the table calls `make_append_only()` and is listed in `APPEND_ONLY_TABLES` (D-13). "Current" is the highest version. JSON payloads are read with `parse_float=Decimal` and written without passing through `float`.
**Reasoning**: BLUEPRINT §6.1 says upsert by key *keeping prior versions*, and §3.4 says any report can be traced to the source row. Raw records are evidence: once periods are approved (§3.5), "what did QBO say on the day we closed" has to be answerable, and `PRIOR_PERIOD_CHANGED` (§10) needs the before and after. Making the table insert-only gives that for free and puts it under the same trigger-and-test guarantee as the audit tables. The hash check is what makes re-uploading a file or re-running a sync a no-op. The Decimal rule extends the CLAUDE.md money rule to stored payloads, where QBO amounts first arrive.
**Affected**: F03; F05, F06, F10, F11 (all write through `store_raw`); F16 (`PRIOR_PERIOD_CHANGED`). BLUEPRINT §3.4, §6.1, §7.

## D-21 · 2026-09-17 · Object storage sits behind a protocol: local in development and CI, Spaces in production
**Decision**: `ObjectStore` protocol with `LocalObjectStore` (development, tests, CI) and `S3ObjectStore` (DigitalOcean Spaces through `boto3`). The store, not the caller, builds the `tenant/{tenant_id}/` prefix. Imported files are content-addressed by SHA-256 under that prefix; original filenames are data, never part of a key. Objects are private; downloads use signed URLs with a short expiry. `S3ObjectStore` is tested with botocore's Stubber; no S3 emulator is added to compose or CI. New dependencies: `boto3`, `python-multipart`.
**Reasoning**: BLUEPRINT §11 requires tenant-prefixed private objects with short-lived signed URLs, and §12 names Spaces. Tests and CI must not need cloud credentials or a network. Having the store build the prefix means a cross-tenant key cannot be constructed by a caller's mistake. Content addressing makes a retried upload idempotent at the storage layer as well as in the database. An emulator would be one more service to operate for a thin gain over the Stubber at this stage.
**Affected**: F03; every later feature that stores files (imports, report exports, opening-balance support in F18). BLUEPRINT §11, §12. Before the first deployment the owner creates the Spaces bucket and keys, and one manual upload/download against it is recorded in OPERATIONS.md.

## D-22 · 2026-09-17 · Interface conventions
**Decision**: The interface stays plain: semantic HTML, one hand-written stylesheet, no UI framework or component library. Money always shows cents, negatives in parentheses, zero as 0.00. Every screen is legible on a phone, with read-only reports as the priority (tables scroll inside their own container, first column held in place). The full conventions live in CLAUDE.md under "Interface conventions".
**Reasoning**: The owner prefers the simple design of the first screens and wants it carried forward. Nearly every later screen is a table of numbers for accountants and contractors, where consistency and legibility matter more than decoration, and no UI library means nothing extra to maintain. Parentheses and 0.00 are the owner's presentation conventions for financial schedules. Client owners will check reports from the field, so phone legibility is planned from the first report rather than retrofitted.
**Affected**: Every feature with a screen, starting with the F03 Imports page; F08, F12, F13, F17 most of all. XLSX and PDF exports follow the same number formats (BLUEPRINT §9). CLAUDE.md gains the "Interface conventions" section.

## D-23 · 2026-09-18 · A cost code is the division plus the cost category
**Decision**: Rye Beach does not keep a separate list of work-type codes. A cost code is two things the platform already tracks, read together: the **division** (one digit: 1 LS, 2 EX, 3 GC, 4 SNOW) and the **cost category** (a two-digit slot). SNOW Labor is 410; EX Subcontractors is 240. The same fourteen cost categories apply to every division:

| Slot | Cost category | Status at Rye Beach today |
|---|---|---|
| 10 | Labor | Ledger accounts exist (5n10) |
| 20 | Labor Burden | Ledger accounts exist (5n20, payroll taxes) |
| 30 | Materials | Ledger accounts exist (5n30) |
| 35 | Supplies | Ledger accounts exist (5n35) |
| 40 | Subcontractors | Exist for LS, EX, GC; none for SNOW |
| 45 | Equipment (owned) | **New.** No ledger account; cost reaches jobs only by a method D-04 has yet to decide |
| 47 | Vehicles (owned) | **New.** As for slot 45 |
| 50 | Equipment Rental | Exist for LS, EX, SNOW |
| 55 | Equipment Maintenance | Exists for GC only (5355) |
| 60 | Disposal | Exist for LS, EX |
| 65 | Fuel | **New as a job cost.** Fuel is posted to shared accounts 5610–5630 today; D-04 decides whether and how it reaches jobs |
| 70 | Permits & Bonds | Exist for LS, EX |
| 80 | Warranty | Exist for LS, EX |
| 90 | Other | No account; catch-all |

The code equals the ledger account number without its leading 5 (code 410 ↔ account 5410), so the chart of accounts, the platform, and what people say aloud all agree. This list replaces the cost category list in CLAUDE.md Vocabulary. Categories are deactivated, never deleted; adding one needs a new decision. Material Sales keeps its two accounts (5510 Bulk Material, 5520 Bulk Salt) as an exception: it sells material rather than running jobs, and its account numbers do not follow the slot scheme.
**Reasoning**: The owner and the snow manager reworked the draft snow codes into Labor, Equipment, Vehicles, Fuel, Materials, Supplies, and the owner wants the same method in every division. That list is a list of cost categories, and division × cost category is what the revamped chart of accounts already encodes, so a second list would duplicate it and invite miscoding. One short list that everyone learns once is what the owner asked for; finer detail about the kind of work comes from LMN work-area names, not from codes. The earlier draft of work-type codes (MasterFormat-style, 23 codes) was never appended and is withdrawn. Equipment (owned), Vehicles (owned), and Fuel are added because the people running the work want to see them on jobs and because LMN estimates already price them into every work area; adding the categories does not decide how their cost gets to a job or whether they are in the WIP basis, which remains D-04.
**Affected**: F04 (`cost_category` seeded with slots, `division.code_digit`, Cost codes view; no `cost_code` table). CLAUDE.md Vocabulary (cost category list replaced; add "**Cost code**: division digit plus cost category slot, e.g. 410 = SNOW Labor"). F06 (estimated cost by cost category per estimate), F10, F11, F12. D-04 (WIP basis; owned equipment, vehicles, fuel). BLUEPRINT §5, §8.1.

## D-24 · 2026-09-18 · Time-and-materials work is its own job and is not on the WIP schedule
**Decision**: Work billed by time and materials (or by the day, hour, or load without a fixed total) is set up as a separate job with contract type T&M: its own LMN estimate or rate sheet, its own QuickBooks project, its own timesheet job. A T&M job never appears on the WIP schedule and has no percent complete, earned revenue adjustment, or over/(under) billing; its revenue is what has been billed. It appears in job cost and job profitability like any other job. A fixed-price job's revised contract contains only fixed-price work areas. A unit-priced line found inside a fixed-price estimate raises an exception for the controller to resolve (move it to a T&M job, or fix its quantity and price) rather than being handled by a rule.
**Reasoning**: Percent of completion measures progress against a fixed total; T&M work has no fixed total, so including it misstates both the contract and the estimated cost (67 Elm Street: ledge removal estimated at one day, $5,475.00, billed for two, $10,950.00). Carving a line out of a job's WIP math would require cost by work area, which the ledger does not carry. Separate jobs need no allocation and work whether T&M is 2% of a project or 40%. The owner will instruct estimators and project managers accordingly.
**Affected**: F07 (job gains contract type: fixed price or T&M), F06 (exception for unit-priced lines in a fixed-price estimate), F12 (T&M jobs in profitability), F14 and F17 (excluded from WIP; shown in the tie-out as job cost outside the schedule so the ledger still ties). BLUEPRINT §5, §8 scope rules. Related: D-01, and the change-order sign-off workflow (T&M authorizations should be signed too).
