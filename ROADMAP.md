# ROADMAP

Build order for the job cost & WIP platform. Design rationale lives in `docs/BLUEPRINT.md`; section references (§) point there.

**How to use this file with Claude Code.** One feature at a time. Copy the feature's block into `current-feature.md`, expand it, build it, tick the acceptance criteria, write the `CHANGELOG.md` entry, update the status column here, clear `current-feature.md`. A feature is not done until its acceptance criteria are demonstrated against **fixture data from Rye Beach**, not invented data.

**Status key**: ☐ not started · ◐ in progress · ☑ done · ⏸ deferred

**Sequencing logic**: each phase ends with something you can use at Rye Beach that week. Phase B answers the deposit question. Phase C gives job cost. Phase D gives the WIP. Multi-tenancy is built in Phase A and *proven* in Phase E with a second tenant.

---

## Phase 0 — Groundwork (no code)
See BLUEPRINT §13. Gate for Phase B: projects exist in QBO for the 16 sold jobs, sample LMN exports are in hand, spike S-01 has run, and the hand-built deposit spreadsheet (§13.6) exists.

| ID | Item | Status |
|---|---|---|
| S-01 | QBO read-only spike: projects as customers, line-level refs, CDC deletes (sandbox in F05; Ramp line check in F05.1) | ☐ |
| P0-1 | QBO projects created + YTD transactions re-tagged for sold jobs | ☐ |
| P0-2 | Ramp Customer/Job field enforced on job-cost categories | ☐ |
| P0-3 | CoA additions (1350, 2410, 4190, 4290, optional 1210/2420) | ☐ |
| P0-4 | Sample files collected and anonymized into `tests/fixtures/` | ☐ |
| P0-5 | Decisions D-01…D-08 recorded in `docs/DECISIONS.md` | ☐ |
| P0-6 | Golden WIP workbook built by hand (8 scenario jobs, §12) | ☐ |

---

## Phase A — Foundation (multi-tenant from the first migration)

### F01 · Project skeleton & tenant isolation  ☑ (2026-09-17)
Repo layout (§12), config, DB session with tenant context, `firm / tenant / user / membership`, Alembic baseline, RLS helper that every tenant table migration must call, health endpoint, CI.
**Accept**: two tenants seeded; test proves (a) every table having `tenant_id` has RLS enabled and forced, (b) app role cannot read across tenants even with a raw query, (c) a request without tenant context reads zero rows.

### F02 · Auth, roles, audit log  ☑ (2026-09-17)
Login, TOTP for firm roles, session cookies, role checks as dependencies, tenant switcher for firm users, append-only `audit_log`.
**Accept**: role matrix test (5 roles × protected routes); `client_pm` cannot fetch pay-rate endpoints; audit rows written for login, tenant switch, role change.

### F02.1 · Auth hardening patch  ☑ (2026-09-17; owner browser pass 2026-09-17)
Firm authority as a `firm_membership` row with entry rows in tenants (D-15), activation links that also enrol TOTP (D-16), one practice per deployment (D-17), the `app.user_id` swap helper (D-18), per-IP throttle, allow-list response schemas, settings without defaults, origin policy, probes out of the application.
**Accept**: `docs/briefs/F02.1.md`; 211 tests. The owner's browser pass (create user via CLI → link → password → TOTP → recovery codes → login → switcher → logout) also closes the F02 frontend criterion.

### F03 · Ingestion framework  ☑ (2026-09-18)
`connection`, `sync_run`, `import_batch`, `raw_record` with versioning; file upload to Spaces under tenant prefix; Postgres-backed worker queue; idempotency by file checksum and by external id; crypto helper for tokens.
**Accept**: uploading the same file twice creates one batch; a modified file creates new raw versions and leaves history; worker task always runs with explicit tenant context.

### F04 · Tenant configuration  ☑ (2026-09-20; owner browser pass 2026-09-19, fixes checked 2026-09-20)
`division`, `cost_category` (seeded), `account_map`, `tenant_policy`, `burden_rate`. Admin UI to map GL accounts → division + cost category + in-job-cost flag, with a "suggest from account number pattern" helper.
**Accept**: loading the Rye Beach chart auto-suggests the correct division and category for every 4xxx/5xxx account from the slot scheme; unmapped accounts are listed.

---

## Phase B — Estimated vs. Billed (first usable release)

### F05 · QBO connection & sync  ☐
OAuth2 connect/reconnect, token refresh, backfill, CDC polling, webhooks, deletes/voids, nightly drift check. Entities per §6.2. Normalize Accounts, Customers/projects, Invoices, Payments, Credit Memos, Sales Receipts, Deposits into `customer`, `billing`, `payment`, `payment_application`.
**Accept**: against sandbox: invoice and payment totals by month equal QBO reports; deleting an invoice in sandbox removes it after next sync; token expiry triggers a reconnect exception, not a crash.

### First deployment · jobcost.dev  ☐
The platform live at jobcost.dev, with the privacy policy and terms pages Intuit requires for production keys (D-25).
**Accept**: to be written in the feature's brief.

### F05.1 · QBO production connection  ☐
Intuit's production questionnaire and keys, Rye Beach connected read-only, monthly totals tied to Rye Beach's own QuickBooks reports, webhooks turned on (D-25). S-01's fourth question, whether Ramp-synced expenses carry the job on the line, is answered here against the real company. The Phase B gate applies.
**Accept**: Rye Beach read-only: invoice and payment totals by month equal QBO reports; webhooks turned on; Intuit production assessment submitted.

### F06 · Estimate import  ☐
`EstimateSource` protocol; LMN export parser; closing-report PDF parser (prices only) as fallback; generic CSV template. Dirty rows load and raise exceptions.
**Accept**: both closing reports parse to 80 estimates (34 Hess, 46 Sanford incl. the id-less Mijal row) with totals matching the report footers to the cent ($936,660.47 and $1,599,103.55 across statuses); Mijal raises `EST_NO_ID`; $0 rows raise `EST_ZERO_SOLD` only when Sold.

### F07 · Job spine & crosswalk  ☐
`job`, `job_alias`, `job_estimate` with roles; "sold estimate → new job or attach as change order" review screen; link job ↔ QBO project with fuzzy suggestions (id-in-name first, then customer + address similarity); customer merge suggestions.
**Accept**: the 16 sold estimates resolve to the agreed job list; Turley and DeVellis cases handled per D-03; every link and unlink is in the audit log; no automatic attachment without confirmation.

### F08 · Sold Jobs Board & Job Detail (billing side)  ☐
Reports 1 and 2 from §9, billing half only. Deposit identification per tenant policy. XLSX/PDF export.
**Accept**: **reproduces the hand-built deposit spreadsheet from §13.6 exactly.** Billed and collected totals across jobs + unassigned tie to QBO income and A/R for the period.

### F09 · Exceptions queue v1  ☐
Exception model, generators for the estimate/job/link/billing types in §10, assignment, resolution notes, counts on the firm console.
**Accept**: resolving an exception's underlying cause clears it on next run; dismissing requires a note.

> **Release B**: you can answer "who has paid a deposit, what is billed, what is left to bill" for every sold job, with backlog by division and estimator.

---

## Phase C — Costs

### F10 · GL cost sync  ☐
Bills, Vendor Credits, Purchases (card/check/cash), Journal Entries → `ledger_line` at **line** level with job reference, account, vendor. Apply `account_map`. `COST_UNASSIGNED` and `COST_DIVISION_MISMATCH` exceptions.
**Accept**: GL-direct job cost + unassigned = QBO P&L COGS by account for each closed month; a Ramp-synced expense with a Customer/Job value lands on the right job with its receipt link.

### F11 · Labor import & costing  ☐
`LaborSource` protocol, LMN timesheet parser, isolved register parser → `employee_rate` (effective-dated, encrypted, restricted), `labor_entry`, burden application, OT handling where source provides it.
**Accept**: computed unburdened job labor vs GL 5x10 by division per month is displayed with the unallocated difference; rows without a job or rate raise exceptions; `client_pm` sees totals only.

### F12 · Job Detail (full) & Job Profitability  ☐
Estimate vs actual by cost category, hours estimated vs actual, drill to source transaction with QBO deep link, all job types.
**Accept**: a job's cost total equals the sum of its drill-down lines; category totals across jobs tie to F10/F11 tie-outs.

### F13 · GL Tie-out screen  ☐
§8.5 checks 1–4 and 6 on one page per period, with tolerances and waive-with-note.
**Accept**: introducing a deliberately unassigned bill in sandbox moves the amount from a job to "unassigned" and the tie-out still balances.

> **Release C**: estimate vs. billed vs. cost per job, tied to the ledger.

---

## Phase D — WIP

### F14 · WIP engine  ☐
`wip/calc.py` as pure Decimal functions implementing §8.1–8.3 including loss recognition, caps, small-job threshold (D-08), and scope rules.
**Accept**: **matches the golden workbook to the cent for all 8 scenarios.** Property tests: over + under nets to billed − earned; percent complete ∈ [0,1]; changing EAC never changes cost or billed.

### F15 · EAC workflow  ☐
`eac_revision` with reason, proposer (`client_pm`), approver (`client_admin`), history; `COST_OVER_EAC` and `EAC_STALE` exceptions; monthly review checklist per tenant.
**Accept**: WIP uses only approved revisions effective on or before period end; full history visible on Job Detail.

### F16 · Period close  ☐
`period` state machine (open → in_review → approved), `wip_snapshot`/`wip_line` freeze, roll-forward tie-out (§8.5 #5), `PRIOR_PERIOD_CHANGED` detection, reopen with reason (firm_admin only).
**Accept**: after approval, changing a June-dated bill in sandbox does not alter the June snapshot and raises the exception showing the delta.

### F17 · WIP schedule report & journal entry  ☐
Report 6 with prior-period comparison and fade/gain; JE per division with auto-reverse (§8.4) as screen/CSV/PDF; configurable legend (§8.7).
**Accept**: JE debits = credits; JE amounts equal schedule over/under totals by division; export carries the legend and the tie-out tab.

### F18 · Opening balances for in-flight jobs  ☐
Onboarding wizard for jobs started before the tool's history: opening cost to date, billed to date, by job, as of a cutover date, with support attached.
**Accept**: a tenant onboarded mid-year produces a first-period WIP whose cumulative columns include openings and whose period-activity tie-outs exclude them.

> **Release D**: monthly WIP close at Rye Beach. Run two real closes before onboarding anyone else.

---

## Phase E — Practice-ready

### F19 · Firm console  ☐
All tenants: connection health, last sync, open exceptions by severity, period status, days to close. 

### F20 · Tenant onboarding workflow  ☐
Checklist-driven: connect QBO → map accounts → import estimates → build crosswalk → opening balances → first tie-out. This is BLUEPRINT §13 turned into product.
**Accept**: a second, synthetic tenant with a *different* chart of accounts (no division-in-account-number; uses Classes) is onboarded end to end. This forces `account_map` to support class-based division, and is the real multi-tenant test.

### F21 · Fade/Gain & Estimator Accuracy  ☐
Report 8. Final vs estimated margin by estimator, division, size band.

### F22 · Client portal polish  ☐
Read-only dashboards for `client_admin`/`client_pm`, scheduled PDF delivery, EAC review reminders.

### F23 · Operations hardening  ☐
Backups and restore drill, key rotation procedure, error monitoring, rate-limit handling, `OPERATIONS.md` complete.

> **Release E**: ready for client #2.

---

## Later (deliberately not now)
- Post JE to QBO via API from an approved period.
- LMN Zapier webhook for "estimate sold" → instant `EST_UNATTACHED` exception.
- Ramp API: uncoded-transaction chaser before sync.
- Owned-equipment and fuel allocation by equipment hours (D-04 revisit).
- Retainage UI and pay-application (AIA-style) support for GC clients.
- Pipeline aging; cash forecast from backlog; bonding-format WIP.
- Second ledger adapter (QuickBooks Desktop/Enterprise is the likely first ask in construction).
- Billing/subscription management for the practice.
