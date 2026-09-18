# CLAUDE.md

Standing instructions for Claude Code in this repo. Read this file, `current-feature.md`, and the BLUEPRINT sections that feature cites before writing code.

## What this project is
A multi-tenant job cost and WIP (work-in-progress) reporting platform for construction and landscape contractors, operated by a CPA practice. It reads from each client's QuickBooks Online (API) and from estimate, timesheet, and payroll exports (files), ties everything to the general ledger, and produces job reports and a month-end WIP schedule with one adjusting journal entry.

The owner is a CPA and the domain expert. When accounting treatment is unclear, **stop and ask**; do not pick a plausible treatment.

## Documents and their jobs
| File | Purpose | Who edits |
|---|---|---|
| `docs/BLUEPRINT.md` | Design and rationale. §8 is the accounting spec. | Owner-approved changes only |
| `ROADMAP.md` | Feature order, acceptance criteria, status | Update status when a feature closes |
| `current-feature.md` | The one feature in flight. There is one live `current-feature.md`. On close, the brief is copied to `docs/briefs/Fxx.md` and the live file is rewritten. | Rewritten per feature |
| `CHANGELOG.md` | What changed and why, per feature | Append on every feature close |
| `docs/DECISIONS.md` | D-xx decisions with date and reasoning | Append-only |
| `docs/OPERATIONS.md` | Runbooks | Update as features land |

## Workflow
1. Work only on the feature in `current-feature.md`. If you find something else that needs fixing, add it under "Discovered" in that file; do not fix it in passing.
2. Before coding, restate the acceptance criteria and list the files you expect to touch. Wait for a go-ahead on anything that adds a table, a dependency, or an external call.
3. Write or update tests first for anything in `app/wip/`, tenancy, or normalizers.
4. On completion: tick criteria in `current-feature.md`, append to `CHANGELOG.md` (what, why, migrations, decisions referenced), flip status in `ROADMAP.md`.
5. Never mark a feature done on synthetic data alone when a Rye Beach fixture exists for it.
6. Claude Code commits at close-out, one commit per closed feature or patch, and never pushes. The owner pushes.
7. Migration round-trip checks run only against a scratch database. `downgrade` refuses to run elsewhere.

## Hard rules (do not break; ask if one seems wrong)

### Money and math
- Money is `Decimal` in Python and `NUMERIC(14,2)` in Postgres. No floats anywhere in a money path, including JSON parsing (parse as string → Decimal) and XLSX reads.
- Rounding: `ROUND_HALF_UP` to cents, applied once at defined points in `app/wip/calc.py`. Percent complete is kept at full precision internally.
- `app/wip/calc.py` contains pure functions only: no DB, no I/O, no clock. It must match `tests/golden/` to the cent. If a change makes a golden test fail, the change is wrong until the owner updates the golden file.

### Tenancy
- Every tenant-scoped table has `tenant_id NOT NULL`, an index that leads with it, and RLS **enabled and forced**, created via the shared migration helper. The CI test that enumerates tables will fail otherwise. Do not weaken that test.
- The app connects as a non-owner role without `BYPASSRLS`. Tenant context is set with `SET LOCAL app.tenant_id` inside the request transaction. No module-level or cached sessions.
- Worker tasks take `tenant_id` as an explicit argument and set context the same way. No task iterates tenants inside one transaction.
- No query, log line, error message, cache key, or file path may mix tenants. Object storage keys start with `tenant/{tenant_id}/`.
- `membership` carries one additional `FOR SELECT` policy (own rows by `app.user_id`), created by `allow_own_membership_read()` (D-11). No other table may have a policy beyond `tenant_isolation` without a new decision.
- Insert-only tables call `make_append_only()` and are listed in `APPEND_ONLY_TABLES` (D-13).
- Approved tables without `tenant_id`: `firm`, `tenant`, `user`, `session`, `firm_audit_log`, `firm_membership`. Any other table without `tenant_id` needs a decision.
- A table without tenant scope may never have a column named `tenant_id`; the enumeration test identifies tenant tables by that name.
- Data migrations set tenant context per tenant and never disable or unforce RLS.
- Reading another user's `membership` rows from an admin request goes only through `read_as_user()` in `app/core/auth.py` (D-18): it swaps `app.user_id` for the block, restores the actor's id in `finally`, is called only from `app/auth/admin.py` after the firm-role check, and its results are limited to tenants of the actor's firm.

### Source-of-truth discipline
- QuickBooks is the book of record. This app does not create or edit QBO transactions except posting an **approved** WIP journal entry (a later feature, behind a flag). Do not add "quick fix" write-backs.
- Raw before normalized: persist the payload or file to `raw_record`/object storage before transforming. Normalizers are idempotent and re-runnable.
- Match on external IDs via `job_alias`. Never match on names in code paths that write links. Fuzzy name matching may only produce *suggestions* for a human.
- Importers never fail a whole file for bad rows. Load what is valid; raise `exception` rows for the rest.
- Approved periods are immutable. Snapshot tables are insert-only. Reopening is a firm_admin action with a reason and an audit entry.

### Security
- OAuth tokens and pay rates are encrypted with the app crypto helper (key id stored alongside ciphertext). Never log them. Never return pay rates to roles other than `firm_admin`, `firm_staff`, `client_admin`.
- Every state-changing action on links, EAC, policy, periods, roles, and connections writes to `audit_log`.
- No secrets in the repo. No real client data in the repo; fixtures are anonymized.

### Integrations
- QBO: core Accounting REST API only. Do **not** depend on the Projects GraphQL API (partner-tier gated). Projects are read as customer records; job refs are read at line level.
- Sync incrementally (CDC + webhooks). No scheduled full re-pulls; API reads are metered. Respect rate limits with backoff.
- LMN and isolved are file imports behind `EstimateSource` / `LaborSource` protocols. New sources implement the protocol; they do not add branches to domain code.

### Code conventions
- Python 3.12, FastAPI, SQLAlchemy 2.0 typed ORM, Alembic, Postgres 16. React 18 + Vite, plain JavaScript.
- Domain logic lives in `app/domain/` and `app/wip/`, not in routers or normalizers.
- Every Alembic migration is reversible or says why not in its docstring.
- The product name appears in exactly one backend constant and one frontend constant. The name is not final.
- Dates: accounting dates are `date`, not `datetime`. Period boundaries are tenant-local calendar months. Timestamps are UTC.

## Vocabulary (use these words in code and UI)
- **Estimate**: a priced proposal from the estimating system. **Job**: the unit we track and report. One job has one or more estimates (roles: original, change_order, ignored).
- **Revised contract** = original + approved change orders. **EAC** = estimated total cost at completion. **Cost to date**, **Billed to date**, **Earned revenue**, **Over/(under) billed**, **Backlog**, **Fade/Gain**: as defined in BLUEPRINT §8.2. Do not invent synonyms.
- **Division**: line of business (LS, EX, GC, SNOW…). **Cost category**: Labor, Labor Burden, Materials, Supplies, Subcontractors, Equipment Rental, Disposal, Permits & Bonds, Warranty, Other.
- **WIP basis**: the set of cost categories included in both cost to date and EAC for percent complete.
- **Unassigned**: ledger amounts in job-cost accounts with no job. Always displayed, never dropped.

## When to stop and ask
- Any change to BLUEPRINT §8 behavior or to golden files.
- Any new table without `tenant_id`.
- Any new third-party dependency or external API.
- Any place where source data is ambiguous and you are tempted to guess (which estimate is the original, which invoice is the deposit, which account is job cost).

### Interface conventions
- Plain and quiet. Semantic HTML and one small hand-written stylesheet. No CSS framework, component library, icon set, or chart library without a decision (new dependencies already require one).
- System font stack. One accent colour, used only for the primary action and links. No gradients, shadows, animations, or decorative imagery.
- One primary action per screen. Labels say what happens ("Upload file", "Approve period"), in the vocabulary of this file. No jargon the owner would not use with a client.
- Data is shown in tables. Numbers are right-aligned with tabular figures; money always shows cents; negatives are in parentheses, never a minus sign; zero is shown as 0.00, never a dash or blank. Totals rows are visually distinct. Columns use the names in BLUEPRINT §8.2 exactly.
- Every report shows its period, tenant name, and the tie-out status on screen, and the legend (§8.7) on every export.
- Status is words first, colour second (colour alone never carries meaning). Errors say what happened and what to do next, in one sentence.
- Every form control has a label, works by keyboard, and its submit control is disabled while a request is in flight.
- Every screen is legible and usable on a phone. Read-only reports are the priority: a wide table scrolls sideways inside its own container with the first column (job or account) held in place, and the page itself never scrolls sideways. Data-entry and admin screens must work on a phone but are designed for a laptop first.