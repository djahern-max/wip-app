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
