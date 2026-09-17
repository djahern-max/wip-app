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
