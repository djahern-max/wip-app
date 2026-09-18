# current-feature.md

## F04 · Tenant configuration
**Roadmap phase**: A · **Blueprint refs**: §5 (configuration tables), §8.1 (WIP basis), §8.5 (tie-out), §11 · **Decisions**: D-22, D-23
**Status**: not started

### Goal
Each tenant can describe its own books to the platform: its divisions, which ledger accounts are job cost and of what kind, its burden rates, and its accounting policy settings. Rye Beach's chart of accounts loads from a file and the platform proposes the mapping from the account-number scheme; a person confirms it. Nothing here reads QuickBooks, and no accounting policy is given a default value.

### In scope
**Tables** (all tenant-scoped: `tenant_id NOT NULL`, leading index, `enable_tenant_rls`)
- `division`: `code` (LS, EX, GC, SNOW, …), `name`, `code_digit` (one digit, nullable, unique per tenant when set: Rye Beach LS 1, EX 2, GC 3, SNOW 4), `active`, `sort_order`. Unique `(tenant_id, code)`. Not seeded globally: divisions belong to the tenant.
- `cost_category` (D-23): seeded per tenant with exactly the D-23 list, each with a two-digit `slot` (10 Labor, 20 Labor Burden, 30 Materials, 35 Supplies, 40 Subcontractors, 45 Equipment (owned), 47 Vehicles (owned), 50 Equipment Rental, 55 Equipment Maintenance, 60 Disposal, 65 Fuel, 70 Permits & Bonds, 80 Warranty, 90 Other). `active`, `sort_order`. Categories are deactivated, never deleted. Adding a category beyond D-23 needs a new decision. CLAUDE.md Vocabulary is updated to this list verbatim.
- **Cost code** (D-23) is not a table: it is the division's `code_digit` followed by the category's `slot` (SNOW Labor = 410), computed by one function and shown wherever a division and a cost category appear together. For Rye Beach it equals the ledger account number without its leading 5 (5410), which is what the suggest rules exploit.
- `gl_account`: the tenant's chart of accounts as the platform knows it: `account_no`, `name`, `ledger_type` (text as the source gives it), `active`, `source` (`file` now, `qbo` in F05), `external_id` (nullable until F05), `raw_record_id`. Unique `(tenant_id, account_no)`. F05 will attach QBO ids by account number, which is an identifier, not a name.
- `account_map`: one row per mapped `gl_account`: `division_id` (nullable: shared accounts have none), `cost_category_id` (nullable: income accounts have none), `in_job_cost` (boolean, NOT NULL), `status ∈ {suggested, confirmed}`, `suggested_by_rule`, `confirmed_by`, `confirmed_at`. Only `confirmed` rows are used by any later feature.
- `account_suggest_rule`: the tenant's numbering scheme as data, not code: ordered rules of the form "account number matches pattern → division / cost category / in_job_cost". Rye Beach's rules are loaded from a fixture; a tenant with no rules simply gets no suggestions (F20's second tenant uses Classes).
- `tenant_policy`: typed keys, one row per key, with `value` (JSONB through the F03 codec), `decided_by`, `decided_at`, `decision_ref`. Keys created in F04: `timezone`, `fiscal_year_start_month`, `wip_basis` (set of cost categories), `small_job_threshold`, `deposit_identification`, `fuel_surcharge_treatment`. **Accounting-policy keys have no default.** An unset key reads as "not decided", and a helper `require_policy(key)` raises a clear error that later features (F08, F14) will surface rather than guessing.
- `burden_rate`: effective-dated (`effective_from` as `date`), optional `division_id`, `rate` as `NUMERIC(7,4)` (a fraction, Decimal end to end), `basis_note`. No overlapping periods for the same division.

**Chart of accounts import**
- A new source kind `chart_of_accounts` registered with the F03 source registry: accepts `.csv` and `.xlsx`, yields one `RawItem` per account with `external_id = account_no`. XLSX cells are read as text or converted string → Decimal; never through `float`. Rows without a four-digit account number are `RejectedItem`s (section headings, the scheme legend at the bottom of Rye Beach's workbook); the batch ends `loaded_with_issues` and the rest loads.
- A normalizer (idempotent, re-runnable) turns the latest `raw_record` per account into `gl_account` rows: new accounts are inserted, renamed accounts are updated with the old name kept in the audit detail, accounts missing from a newer file are marked inactive, never deleted.
- After normalizing, the suggestion step writes `account_map` rows with `status = suggested` for accounts that match a rule and have no confirmed mapping. It never overwrites a confirmed row.

**Screens** (D-22 conventions; one stylesheet; table pattern from F03.1)
- **Accounts**: every account with number, name, type, division, cost category, in job cost, status. Filter: all / unmapped / suggested / confirmed. Edit one row; "Confirm all suggestions" as the one primary action, with a count and a confirmation step. **Unmapped accounts are always listed and counted at the top.** The table scrolls in its own container with the account column held in place.
- **Divisions**, **Cost categories**, **Burden rates**: plain list and add/edit/deactivate forms. A read-only **Cost codes** view shows the division × cost category grid with each code and the ledger account mapped to it, or "no account" where none is.
- **Policy**: each key with its value or "Not decided", who decided, when, and the decision reference. Editable by `firm_admin` only.
- Read access: `firm_admin`, `firm_staff`, `client_admin`. Write access: `firm_admin`, `firm_staff` (capability `can_manage_tenant_config`); policy keys `firm_admin` only. `client_pm` and `client_viewer` get 403.

**Audit**
- Every create, change, confirm, deactivate on `division`, `cost_category`, `account_map`, `account_suggest_rule`, `tenant_policy`, `burden_rate` writes to `audit_log` in the same transaction, with before and after values. "Confirm all suggestions" writes one row per account, not one summary row.

**Fixtures**
- `tests/fixtures/rye_beach/chart_of_accounts.csv`: the Rye Beach chart, anonymized: bank, card, and loan account names lose institution names and trailing digits; account numbers, types, and every 4xxx/5xxx name are kept as they are. The owner supplies and approves it.
- `tests/fixtures/rye_beach/account_suggest_rules.json` and `account_map_expected.csv`: the slot scheme and the suggestions the owner expects, supplied with this brief. The JSON states the semantics (first matching rule wins); its shape may be changed to fit `account_suggest_rule`, the results may not. The expected file is the acceptance oracle; do not derive it from the code under test. These are expected *suggestions*: whether 5355, fuel, or anything else is finally job cost is the owner's call at confirmation and under D-04.

**Dependencies**: `openpyxl` for reading `.xlsx` (approved for this feature). Anything else: stop and ask.

### Out of scope
Any QuickBooks call or account sync (F05). Class-based or location-based division mapping (F20). Using the mapping to classify transactions (F10). Employee pay rates (F11). Deciding any accounting policy: F04 stores decisions, it does not make them. Cost categories beyond the D-23 list. Work-type codes (paving, planting, plowing): dropped by D-23; work-area names carry that detail. Whether Equipment (owned), Vehicles (owned), and Fuel reach jobs by direct coding or by allocation, and whether they are in the WIP basis (D-04, not decided). Estimates and work areas (F06/F07). Effective-dating `account_map` (approved periods are protected by snapshots in F16; changes are audited).

### Acceptance criteria
- [ ] RLS enumeration passes for all seven new tables; the extra-policy allow-list still has one entry; tenant A reads zero rows of tenant B in each, via ORM and raw SQL as `app_rw`.
- [ ] **Rye Beach fixture**: importing `chart_of_accounts.csv` (159 accounts) and running suggestions produces, for **every** account, exactly the division, cost category, and in-job-cost value in `account_map_expected.csv` (29 division cost accounts in job cost; income, headers, material sales, shared fuel, and everything outside 4000–5999 suggested as not job cost); every row has `status = suggested` and none is `confirmed` until a person confirms it.
- [ ] Unmapped accounts are listed and counted; the count equals the number of active accounts without a confirmed mapping.
- [ ] Importing the same chart file twice creates one batch and changes nothing. Importing a revised file (one account renamed, one added, one removed) renames, adds, and deactivates respectively; confirmed mappings on untouched accounts are unchanged; the renamed account keeps its mapping; raw history shows both versions.
- [ ] With a synthetic `.xlsx` that mimics the owner's workbook layout (title row, section headings such as "ASSETS (1000–1999)", a legend block at the bottom), heading and legend rows are rejected individually; the batch ends `loaded_with_issues`; all real accounts load.
- [ ] A suggestion never overwrites a confirmed mapping; re-running suggestions is idempotent.
- [ ] A second tenant with no suggest rules imports a chart and gets zero suggestions and every account listed as unmapped.
- [ ] `tenant_policy`: an accounting-policy key that was never set reads as not decided and `require_policy` raises naming the key; setting it requires `firm_admin`, records who, when, and a decision reference, and writes an audit row with before and after. No accounting-policy key has a default anywhere in code or migration (static test).
- [ ] `burden_rate`: overlapping effective periods for the same division are refused; the rate in force on a given `date` is returned by one function, tested at the boundaries; rates are `Decimal` end to end (no `float` on the path; hygiene test extended).
- [ ] `cost_category` holds exactly the D-23 list with its slots for a new tenant, seeded by the same audited service the API uses, per tenant, never by a cross-tenant statement. The cost-code function returns 410 for SNOW + Labor, nothing for a division without a `code_digit`, and the Cost codes view shows "no account" for Rye Beach cells such as SNOW + Subcontractors and every Equipment (owned), Vehicles (owned), and Fuel cell.
- [ ] Role matrix extended: config read and write routes per the roles above; policy write is `firm_admin` only; 401 unauthenticated; CSRF header required.
- [ ] Every state-changing action writes its audit row in the same transaction; "Confirm all suggestions" on N accounts writes N rows.
- [ ] Frontend: no `style={{`, effect scanner passes with no new allow-list entry, `npm test` passes; money and rates shown through the shared formatting helpers; the Accounts table scrolls inside its container with the account column held in place and the page does not scroll sideways at 390 px.
- [ ] `alembic upgrade head` → `downgrade base` → `upgrade head` on a scratch database; the migration is reversible; the downgrade guard still refuses `wip`.
- [ ] CI green on the pushed commit.
- [ ] **Owner, in a browser**: upload the Rye Beach chart → see 159 suggestions, 29 of them in job cost, and the unconfirmed count at the top → open the Cost codes grid and see 410 against 5410 → correct one suggestion by hand → confirm all and see the unconfirmed count reach zero → set one policy key and see it recorded with your name → add a burden rate → check the Accounts page at phone width. Not ticked by Claude Code.

### Plan (Claude Code fills in before coding)
_Files to create, dependencies to add, open questions._

Open questions to answer here before coding; wait for a go-ahead on each:
1. What BLUEPRINT §5 calls the chart-of-accounts table and whether `gl_account` as specified here matches it. If the names differ, propose which to keep.
2. The shape of `account_suggest_rule` (pattern syntax, ordering, how a rule sets only some of division / category / in-job-cost) shown against Rye Beach's scheme: 4n00 income by division; 5n00 headers; 5nXX where n is the division and XX the cost-category slot.
3. How the normalizer is triggered after `import.process_batch` (chained task with its own `dedupe_key`, or a second registered step), keeping one tenant per transaction.

### Discovered (do not fix here)
_Things noticed along the way that belong to another feature._

### Close-out
- [ ] CHANGELOG entry written
- [ ] ROADMAP status flipped
- [ ] OPERATIONS.md: loading a chart, editing suggest rules, setting a policy key
- [ ] D-23 present in `docs/DECISIONS.md`
- [ ] Brief copied to `docs/briefs/F04.md`; live file rewritten as a stub pointing at F05
- [ ] One commit, not pushed
