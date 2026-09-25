# BLUEPRINT — Job Cost & WIP Platform (working name: `jobcost`)

Prepared 2026-09-17. First tenant: Rye Beach Landscaping LLC.
This is the design document. `ROADMAP.md` is the build order. `CLAUDE.md` holds the rules Claude Code must follow on every feature.

---

## 1. What this is

A multi-tenant web application that sits **beside** each client's accounting system and answers four questions per job, every month, with numbers that tie to the general ledger:

1. What did we sell? (contract + change orders, and the cost we estimated)
2. What have we billed and collected? (deposits, progress billings, retainage, open A/R)
3. What has it cost so far? (labor, materials, subs, rentals, other)
4. Where does that leave us? (percent complete, earned revenue, over/under billing → WIP schedule → one journal entry)

**What it is not.** It is not a second general ledger, not an estimating tool, not a time clock, and not an invoicing tool. QuickBooks stays the book of record. LMN stays the estimating/time system. Ramp stays the spend system. This tool reads from them, reconciles them to each other, and produces reports plus one month-end adjusting entry.

**The business model it serves.** You are not selling software. You are selling a monthly job-cost close delivered by a CPA, and this tool is what makes that service fast enough to be profitable at 10–30 clients. That drives two priorities that a normal SaaS would get backwards:

- The **firm console** (you, across all clients: who is synced, who has exceptions, whose WIP is unapproved) matters more than the client-facing portal.
- **Onboarding a messy client** (crosswalks, account mapping, opening balances for jobs already in progress) is a first-class workflow, not an afterthought. Every client you take on will look like Rye Beach did before cleanup.

---

## 2. What your files told me

These findings are why the design is shaped the way it is.

### 2.1 From the chart of accounts, balance sheet, and P&L

| Finding | Evidence | Design consequence |
|---|---|---|
| Division lives in the account number, not in Classes | 41xx/51xx = LS, 42xx/52xx = EX, etc. "same last two digits = same cost" | The tool derives **division** and **cost category** from the GL account via a per-tenant `account_map`. No dependency on QBO Classes. Your x10/x20/x30 slot scheme maps 1:1 to cost categories, which is excellent. |
| No WIP balance-sheet accounts exist | No underbillings asset, no overbillings liability, no retainage, no customer deposits | Phase 0 adds them (§13). Until then revenue = billings, which is the thing the WIP entry corrects. |
| WIP only applies to part of the business | LS + EX = $2.85M of $3.97M YTD revenue (72%). Snow, Grounds Care, Material/Salt Sales are recurring or point-of-sale | Jobs carry a **revenue method**: `fixed_price` (in WIP), `time_and_materials`, `recurring_service`, `none`. Only `fixed_price` jobs appear on the WIP schedule. All types get profitability reports. |
| Labor is posted to the GL in lump sums by division | 5110/5210/5310/5410 Gross Payroll totals $969K with no job detail | **Labor job cost cannot come from the GL.** It comes from hours-by-job (LMN) × pay rate (isolved) × burden. The GL is used only to tie out the total. |
| Labor burden is mostly sitting in overhead | Payroll taxes are in COGS (7.2% of field gross) but workers' comp ($79.5K) and health/dental/life ($106.7K) are in 6xxx | The tool needs a per-tenant **burden rate** applied to job labor. Rough Rye Beach range from the P&L: ~15% (taxes + WC) to ~26% (adding benefits, which overstate because they include office staff). You set the policy; the tool applies and discloses it. |
| Owned-equipment cost never reaches a job | No depreciation booked YTD (9020 is empty); parts $158K in 6410; fuel $162K pooled in 56xx; equipment debt ≈ $806K | LMN estimates price equipment at internal rates, but the GL has no matching job cost. The WIP must treat equipment **consistently on both sides** of the percent-complete fraction (Decision D-04). |
| A payroll coding anomaly | Payroll Taxes - SNOW is $798 on $59.7K gross (1.3% vs ~7.5% elsewhere) | Snow payroll taxes are landing somewhere else. Not a tool issue, but it is the kind of thing the tie-out screen should surface automatically (ratio checks per division). |
| Float artifacts in exports | `141366.68000000002` in the balance sheet export | All money is `Decimal` / `NUMERIC(14,2)`. Never float. This is a CLAUDE.md hard rule. |

### 2.2 From the two estimator closing reports

| Finding | Evidence | Design consequence |
|---|---|---|
| **The closing report cannot answer the deposit question** | It has Est. ID, client, jobsite, name, price, status. No dates, no deposit, no billing, no cost breakdown | Deposit and job status require joining LMN estimates to QBO invoices/payments. That join is Feature 1 of the product (the Sold Jobs Board). To answer it *today*, by hand, see §13.6. |
| 16 sold estimates = $916,399.57 | Hess $429,962.74 (10) + Sanford $486,436.83 (6) | Small enough that the first tenant can be onboarded by hand-verified crosswalk. Good test set. |
| One customer is 58% of sold work | Turley: $167,938.50 (Hess, EST6366990) + $366,889.80 (Sanford, EST6120638) = $534,828.30 | Are these two jobs, two phases, or one job? The model must support **many estimates → one job**, and a human decides. Also: WIP accuracy on Turley alone drives the whole schedule. |
| Change orders arrive as new estimates | "AWO'S 6-15-26" (Penthany, $16,746.83); a $998.71 DeVellis add-on at 1701 Ocean Blvd, same project as a $39,032.44 sold estimate, different estimator, different client-name spelling | Every estimate attached to a job has a **role**: `original`, `change_order`, or `ignored`. Attaching is a reviewed action, never automatic. |
| Names do not match across records | "Turley, Kyle & **Amy**" vs jobsite "Kyle & **May**"; "Hosmer" vs "Hossler"; "Elyse Menken" vs "Elyse Arrigo"; DeVellis Design (designer) vs Mukherjee Residence (owner) | Matching is by **ID, never by name**. The tool keeps a crosswalk (`job_alias`) and proposes fuzzy matches for a human to confirm. Put the LMN estimate ID in the QBO project name going forward (§13.2). |
| Dirty records exist | A sold estimate with no Est. ID (Mijal, $8,548.03); $0.00 estimates in pending and lost; a "Plant Warranties" estimate sold at $7,751.37 | The importer must never reject a file for dirty rows. It loads what it can and raises **exceptions** (§10). Warranty jobs need a `warranty` job type: cost, no revenue. |
| Commercial GC clients | Chinburg Builders ($111,810.40), Jayeff Construction/KinderCare | Expect retainage and possibly pay-app billing. Retainage is in the data model from day one even if the first UI ignores it. |
| Close rate is misleading | Hess shows 97.0% because only one estimate was ever marked Lost. Sold ÷ all estimates: Hess 45.9%, Sanford 30.4%. $1.42M is sitting in Pending | Stale pending estimates are not backlog. The tool's backlog = **sold, unbilled** only. A pipeline-aging view is a cheap later add-on that owners will love. |

### 2.3 From checking what each system can actually hand over (Sept 2026)

| System | Reality | Consequence |
|---|---|---|
| **LMN** | Its support docs state Zapier is the only API integration available; the Zapier trigger fires on estimate created/updated. Job costing and Zapier both require Professional/Enterprise plans. Rich report exports exist (estimates by cost code, won/lost/pending hours-cost-revenue, timesheets). | LMN ingestion = **file import** of standard report exports, with an optional Zapier webhook later for "estimate sold" events. Build the importer to be forgiving and re-runnable. |
| **Ramp** | QBO Projects sync into Ramp's **Customer/Job tracking category**, and coded transactions sync to QBO carrying customer, class, location, memo, receipt. | **v1 needs no Ramp integration at all.** Configure Ramp to require the job on COGS spend; it arrives in QBO already job-tagged; the tool reads QBO. A Ramp API connection is only useful later for chasing uncoded transactions before they sync. |
| **QuickBooks Online** | Accounting REST API is open to any registered app. The newer **Projects GraphQL API is limited to Silver/Gold/Platinum partner tiers**. Transactions can carry `ProjectRef`/`CustomerRef`, and expense/bill *lines* carry a customer reference. | Do **not** depend on the Projects GraphQL API. Read projects as customer records through the REST API (spike S-01 confirms the `IsProject` flag and line-level refs in your company file). Job assignment is read at the **line** level. API reads are metered under Intuit's partner program, so sync incrementally (CDC + webhooks), never full re-pulls. |
| **isolved** | No self-serve public API that I could confirm; access appears to be partner-gated or through aggregators. | Payroll ingestion = **file import** of a payroll register (employee, pay date, hours, gross, rate). Only needed for pay rates; hours-by-job come from LMN. |

**Net effect:** one real API integration (QBO) plus forgiving file importers. That is a much smaller, more durable product than four API integrations, and it generalizes: your next client will not use LMN, but they will have *some* estimate export and *some* timesheet export.

---

## 3. Design principles

1. **QBO is the book of record.** The tool never becomes a competing ledger. It writes to QBO in exactly one place: the approved month-end WIP journal entry (and v1 only exports that entry for you to post).
2. **Everything ties out or says why not.** Every report shows its reconciliation to the GL. Costs not assigned to a job are never hidden; they appear as an explicit "Unassigned" line. If the tie-out is broken, the period cannot be closed.
3. **The job is the spine.** One canonical `job` record, with a crosswalk to every outside identifier. IDs, never names.
4. **Raw first, then normalize.** Every sync and every file is stored as received, then transformed. Any report can be traced to the source row, and normalization can be re-run when rules change.
5. **Closed periods are immutable.** Approving a WIP period snapshots it. Later changes in QBO show as next-period activity or as a flagged "prior period changed" exception. Never silently restate.
6. **Judgment belongs to people, with an audit trail.** Estimated cost to complete is management's estimate. The tool records who set it, when, and why. It never invents one.
7. **Exceptions over errors.** Dirty data creates a work queue, not a failed import.
8. **Tenant isolation is enforced by the database**, not by remembering a `WHERE` clause.
9. **Adapters at the edges.** Estimate source, labor source, and ledger source are interfaces. LMN and QBO are the first implementations.
10. **Boring infrastructure.** One API service, one worker, one Postgres, one object store.

---

## 4. System landscape

```
   ESTIMATING / TIME            SPEND                 PAYROLL
  ┌──────────────┐        ┌──────────────┐       ┌──────────────┐
  │     LMN      │        │     Ramp     │       │   isolved    │
  │ estimates    │        │ cards, bills │       │ pay register │
  │ timesheets   │        │ job required │       │ (rates)      │
  └──────┬───────┘        └──────┬───────┘       └──────┬───────┘
         │ report exports        │ native sync          │ register export
         │ (+ Zapier later)      ▼                      │
         │                ┌──────────────┐              │
         │                │  QuickBooks  │◄── payroll JE (lump, by division)
         │                │   Online     │
         │                │ BOOK OF RECORD
         │                └──────┬───────┘
         │                       │ REST API: OAuth2, CDC, webhooks
         ▼                       ▼                      ▼
  ┌───────────────────────────────────────────────────────────────┐
  │                          jobcost                              │
  │  raw store → normalize → JOB SPINE → reports → WIP period     │
  │  exceptions queue · tie-outs · audit log · firm console       │
  └──────────────────────────────┬────────────────────────────────┘
                                 │ approved WIP journal entry
                                 ▼ (v1: export · v2: post via API)
                            QuickBooks Online
```

---

## 5. The job spine

```
customer ──< job ──< job_estimate   (role: original | change_order | ignored)
              │──< job_alias        (system, external_id)  e.g. ('lmn_estimate','EST6366990'),
              │                                                  ('qbo_customer','412'),
              │                                                  ('lmn_job','J-2291')
              │──< billing / payment_application
              │──< cost_line        (from ledger lines, labor entries, allocations)
              └──< eac_revision     (who changed estimated total cost, when, why)
```

Rules:

- A **job** is the unit of the WIP schedule. It has one customer, one division, one revenue method, one status (`sold`, `in_progress`, `substantially_complete`, `closed`, `cancelled`), and a contract value that is always *computed* from its attached estimates plus manual adjustments, never typed over.
- An **estimate** becomes relevant when its status is Sold. The tool proposes: "new job" or "attach to existing job as change order" (same customer + same jobsite → suggest attach). A person confirms.
- A **QBO project/sub-customer** links to exactly one job. A sold job with no QBO link is an exception. A QBO project with activity and no job is an exception.
- **Division** comes from the estimate or is set on the job; cost lines whose GL account maps to a *different* division than their job raise a soft warning (mis-coding detector).
- `revenue_method` gains the value `pool` (D-30, D-31); the values are now `fixed_price`, `time_and_materials`, `recurring_service`, `none`, `pool`. A pool holds shared supplies until month-end allocation; it has no estimate and no contract, and a person sets the value (never inferred from the name).

---

## 6. Ingestion design

### 6.1 Common machinery

- `connection` — per tenant, per system. OAuth tokens encrypted at rest. Status, last success, last error.
- `sync_run` / `import_batch` — one row per pull or upload: who, when, counts, checksum of the file, outcome.
- `raw_record` — `(tenant_id, source, entity_type, external_id, payload JSONB, fetched_at, batch_id)`. Upsert by `(tenant, source, entity_type, external_id)`, keeping prior versions.
- Normalizers are **pure functions** from raw records to canonical rows. Re-runnable, idempotent, unit-tested against fixture files.
- Re-uploading the same file is a no-op. Uploading a newer version of the same report updates rows by external ID.

### 6.2 QBO (the only live API in v1)

- OAuth2 with refresh-token rotation; tokens per tenant; reconnect flow when refresh fails.
- Initial backfill by entity, then **Change Data Capture** polling plus **webhooks** for near-real-time. Nightly light reconciliation (counts and totals by account) to catch drift.
- Entities: Account, Customer (incl. projects/sub-customers), Vendor, Item, Invoice, Payment, CreditMemo, Deposit, SalesReceipt, Bill, VendorCredit, Purchase (expenses/checks/card), JournalEntry, TimeActivity (if used), Preferences, CompanyInfo.
- Reports API: TrialBalance and ProfitAndLoss by period, used **only** for tie-outs.
- Job assignment is read per **line** (`CustomerRef` on expense/bill lines; header `CustomerRef`/`ProjectRef` on sales forms).
- Deleted and voided transactions must flow through (CDC reports deletes).

### 6.3 Estimates (LMN first)

- Interface: `EstimateSource.parse(file) -> list[EstimateDTO]`. DTO: external id, status, customer, jobsite, name, estimator, dates, price, and **estimated cost by category** (labor hours, labor $, equipment $, materials $, subs $, other $).
- Implementation 1: LMN report export (CSV/XLSX). Which report to standardize on is Phase 0 task §13.5; you need the one with cost-by-category, not the closing report.
- Implementation 2: generic CSV template for non-LMN clients.
- Implementation 3 (fallback): the Estimator Closing Report PDF, prices only. Useful for day one at Rye Beach since it is what you have; it cannot feed the WIP because it has no estimated cost.

### 6.4 Labor

- Interface: `LaborSource.parse(file) -> list[LaborEntryDTO]` (employee, date, job reference, hours, optional cost code).
- LMN timesheet/job-cost export first; QBO TimeActivity and generic CSV as alternates.
- `employee_rate` from the isolved register import (effective-dated). **Pay rates are restricted data**: visible only to firm users and tenant admins; everyone else sees job-level labor totals.
- Labor cost = hours × rate (OT-aware when the source provides it) × (1 + burden rate for the period).

---

## 7. Domain model (tables)

All tables carry `tenant_id` except `firm`, `user`, and reference enums.

**Tenancy & access**: `firm`, `tenant`, `user`, `membership(user, tenant, role)`, `audit_log`
**Integration**: `connection`, `sync_run`, `import_batch`, `raw_record`
**Configuration**: `division`, `cost_category`, `account_map(gl_account → division, cost_category, in_job_cost bool)`, `burden_rate(effective-dated)`, `equipment_rate` (later), `tenant_policy` (WIP method options, deposit item ids, thresholds, per-pool eligibility and driver per D-30)
**Spine**: `customer`, `job`, `job_alias`, `estimate`, `estimate_cost(estimate, cost_category, hours, amount)`, `job_estimate(job, estimate, role)`, `eac_revision`
**Billing**: `billing` (invoice/credit memo/sales receipt, header), `billing_line`, `payment`, `payment_application(payment → billing, amount)`, `retainage` fields on billing
**Cost**: `ledger_line` (normalized GL-side line: date, account, vendor, amount, job_id nullable, source txn ref), `labor_entry`, `allocation` (first use: supplies pools, D-30; later owned equipment and fuel), and a **view** `job_cost_line` that unions them with a `basis` column: `gl_direct`, `labor_computed`, `allocated` (first used by pool allocation, D-30)
**Period close**: `period(tenant, month, status: open|in_review|approved)`, `wip_snapshot`, `wip_line` (every column of the schedule, frozen), `journal_export`, `tieout_result`
**Work queue**: `exception(type, severity, entity_ref, status, assigned_to, resolution_note)`

Standard cost categories (seeded, tenant can rename): Labor, Labor Burden, Materials, Supplies, Subcontractors, Equipment Rental, Owned Equipment (memo), Disposal, Permits & Bonds, Warranty, Other.
Rye Beach mapping is mechanical from your slot scheme: x10→Labor, x20→Labor Burden, x30→Materials, x35→Supplies, x40→Subcontractors, x50→Equipment Rental, x55→Equipment Maintenance, x60→Disposal, x70→Permits & Bonds, x80→Warranty.

---

## 8. Accounting method (the WIP spec)

This section is the contract between you and the code. Claude Code implements exactly this; any change here is a changelog-worthy decision.

### 8.1 Scope
A job is on the schedule for period P if `revenue_method = fixed_price` and it has a contract and **any** billing or cost through the end of P, until the period after it is closed. Sold jobs with no activity appear on the **backlog** report, not the WIP. Pool jobs (D-30) are never on the schedule or the backlog.

### 8.2 Columns

| # | Column | Definition |
|---|---|---|
| 1 | Original contract | Sum of `original` estimates |
| 2 | Approved change orders | Sum of `change_order` estimates with status Sold |
| 3 | **Revised contract** | 1 + 2 |
| 4 | Original estimated cost | From estimate cost breakdown, WIP-basis categories only |
| 5 | **Estimated total cost (EAC)** | 4 + CO cost + `eac_revision`s. Must be ≥ cost to date |
| 6 | Estimated gross profit | 3 − 5 |
| 7 | Cost to date | WIP-basis job cost through period end |
| 8 | **Percent complete** | 7 ÷ 5, capped at 100% |
| 9 | Earned revenue | 8 × 3 |
| 10 | Billed to date | Invoices − credit memos through period end (incl. retainage billed) |
| 11 | **Over / (under) billed** | 10 − 9 |
| 12 | Gross profit to date | 9 − 7 |
| 13 | Backlog | 3 − 9 |
| 14 | Prior-period GP%, and **fade/gain** | change in column 6 ÷ 3 since last approved period |

Loss jobs: when column 6 is negative, the full projected loss is recognized now (earned revenue is reduced so GP to date equals the total expected loss) and the line is flagged.

Supporting memo columns: collected to date, open A/R, retainage held, deposit received, last cost date, last billing date, EAC last reviewed date and by whom.

### 8.3 What counts as "cost" (WIP basis)
Controlled by `account_map.in_job_cost` and tenant policy. Both the numerator (cost to date) and denominator (EAC) must use the **same** categories. See Decision D-04 for owned equipment. Default recommendation: burdened labor + materials + supplies + subs + rentals + disposal + permits; owned equipment and fuel excluded from the WIP fraction and shown as memo on the profitability report. Pooled supplies reach jobs by month-end allocation per D-30 and are in the WIP basis on both sides.

### 8.4 The journal entry
One entry per period, per division, auto-reversing on day 1 of the next period:

```
Underbilled jobs (sum):  DR 1350 Costs & Est. Earnings in Excess of Billings
                         CR 4190 WIP Adjustment - LS   (or 4290 - EX)
Overbilled jobs (sum):   DR 4190 WIP Adjustment - LS
                         CR 2410 Billings in Excess of Costs & Est. Earnings
```

Using separate 4x90 adjustment accounts keeps billed revenue visible and makes the adjustment obvious on the P&L. v1 produces a formatted entry (screen, CSV, PDF). v2 posts it through the API **only** from an approved period and records the QBO JE id on the snapshot.

### 8.5 Tie-outs that gate approval
A period cannot move to `approved` unless each of these passes or is explicitly waived with a note:

1. **Revenue**: billed-in-period across all jobs + unassigned income = GL income accounts for the period.
2. **Cost**: GL-direct job cost + unassigned COGS = GL 5xxx for the period.
3. **Labor**: computed job labor (unburdened) vs GL gross payroll COGS accounts. The difference is shown as unallocated labor (shop time, travel, PTO) with a tenant-set tolerance.
4. **A/R**: open balances by job + unassigned = GL 1200.
5. **Roll-forward**: prior approved snapshot + period activity = current snapshot, per job.
6. **Ratio checks** (warnings): payroll tax % by division, materials % of revenue by division versus trailing average. This would have caught the SNOW payroll-tax anomaly.
7. **Pool balances** (D-30): every pool job balance is 0.00 at period end, or waived with a note.

### 8.6 Deposits (Decision D-02)
Recommended: deposits are invoiced like any other billing and hit income; the WIP entry defers them, and a job with a deposit and no cost correctly shows as ~100% overbilled. This keeps LMN/QBO invoicing habits unchanged. The Sold Jobs Board still identifies deposits (by configured deposit item, or first invoice before first cost) so you can answer "who has paid a deposit?" The alternative (a 2420 Customer Deposits liability with reclass at job start) is supported by the model but adds a manual step every client will forget.

### 8.7 Output legend
Every exported schedule carries a tenant-configurable footer (default: "Prepared by management from the company's records. No assurance is provided."). You will know better than I do how this interacts with your SSARS obligations once schedules start going to banks and sureties; the tool just needs to make the legend impossible to forget.

---

## 9. Reports (in build order)

1. **Sold Jobs Board** — every sold job: contract, change orders, deposit invoiced/received, billed, collected, open A/R, remaining to bill, days since last activity, estimator. *This is the report that answers your deposit question.*
2. **Job Detail** — one job: estimate vs actual by cost category, billing history, payment history, change orders, cost transactions drill-down to the QBO link, labor hours estimated vs actual.
3. **Exceptions Queue** — see §10.
4. **Backlog** — sold and unbilled, by division, by estimator, by expected start.
5. **Job Profitability** — all job types including T&M, snow contracts, grounds care; with owned-equipment and fuel as memo lines.
6. **WIP Schedule** — §8, with period selector, comparison to prior period, approve/lock.
7. **GL Tie-out** — §8.5 on one screen.
8. **Fade/Gain and Estimator Accuracy** — estimated vs final margin by estimator, division, job size. This is the report that changes behavior.
9. **Firm Console** — all tenants: connection health, open exceptions, period status, days since last sync.
10. *(later)* Pipeline aging; cash forecast from backlog and billing terms; bonding-format WIP; equipment utilization.

All reports: on-screen, XLSX, PDF. XLSX exports contain values, not float artifacts, with a tie-out tab.

---

## 10. Exception types (seed list)

| Code | Trigger | Severity |
|---|---|---|
| `EST_NO_ID` | Imported estimate lacks an external id (Mijal) | warn |
| `EST_ZERO_SOLD` | Sold estimate with $0 price | warn |
| `EST_UNATTACHED` | Sold estimate not attached to a job | block-close |
| `EST_NO_COST` | Sold estimate without cost breakdown (cannot enter WIP) | block-close for fixed-price |
| `JOB_NO_LEDGER_LINK` | Job has no QBO project/customer | block-close |
| `LEDGER_PROJECT_NO_JOB` | QBO project has activity, no job | block-close |
| `COST_UNASSIGNED` | In-job-cost GL line with no job | warn, totals shown on tie-out |
| `COST_DIVISION_MISMATCH` | Line's account division ≠ job division | warn |
| `BILLED_OVER_CONTRACT` | Billed > revised contract (likely missing change order) | warn |
| `COST_OVER_EAC` | Cost to date > EAC | block-close until EAC revised |
| `EAC_STALE` | Job > X% complete or > N days since EAC review | warn |
| `CUSTOMER_FUZZY` | Possible duplicate or mismatch (Hosmer/Hossler) | info |
| `LABOR_NO_JOB` / `LABOR_NO_RATE` | Timesheet row unmatched to job or employee rate | warn |
| `PRIOR_PERIOD_CHANGED` | Source data dated in an approved period changed | warn, shows delta |
| `WARRANTY_REVENUE` | Warranty-type job has billings | info |

---

## 11. Multi-tenancy, roles, security

**Shape**: `firm` (your practice) → `tenant` (client company) → `membership` (user ↔ tenant ↔ role). Firm staff hold memberships in many tenants; client users in one. No self-serve signup; you create tenants.

**Roles**: `firm_admin`, `firm_staff`, `client_admin` (owner/controller: sees everything for their company, approves EAC), `client_pm` (their jobs, no pay rates, can propose EAC), `client_viewer`.

**Isolation**: shared schema, `tenant_id` on every row, **Postgres Row-Level Security** on every tenant table, policy keyed to `current_setting('app.tenant_id')`, set with `SET LOCAL` inside each request's transaction. The application DB role is not the table owner and has no `BYPASSRLS`. Migrations run as a separate owner role. A CI test creates two tenants and proves that every table with a `tenant_id` column has RLS enabled and that cross-tenant reads return zero rows. Background jobs take a tenant id explicitly and set the same context. Removing a tenant altogether is an operator action on the server with the owner role, never an API call: it is the one time the append-only triggers are suspended, for that transaction only, and it leaves a firm-level audit row (D-28).

**Secrets**: OAuth tokens and pay rates encrypted at the application layer (envelope key from environment/secret manager, key id stored with the ciphertext so keys can rotate). Files in object storage under `tenant/{id}/…`, private, short-lived signed URLs.

**Audit**: append-only `audit_log` for logins, connection changes, crosswalk decisions, EAC revisions, policy changes, period approvals and re-opens, exports.

**Auth**: email + password with mandatory TOTP for firm roles; consider a hosted identity provider rather than hand-rolling. Session cookies, not tokens in local storage.

**Practice hygiene outside the code**: engagement-letter language on data access and on who owns the estimates; Intuit's production-key security questionnaire; a written information security plan. None are hard, all are easier before the second client than after.

---

## 12. Stack and layout

Same stack you already run in production, so nothing new to operate: Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres 16, React 18 + Vite (plain JS), DigitalOcean (App Platform or a droplet, Managed Postgres, Spaces). One addition: a worker process with a **Postgres-backed job queue** (no Redis) for syncs, imports, and report rendering.

```
jobcost/
  CLAUDE.md  ROADMAP.md  CHANGELOG.md  current-feature.md
  docs/  BLUEPRINT.md  WIP-METHOD.md(§8, extracted once stable)  OPERATIONS.md  DECISIONS.md
  backend/
    app/
      core/        config, db session + tenant context, security, crypto, audit
      tenancy/     firm, tenant, user, membership, RLS helpers
      integrations/
        base.py    EstimateSource, LaborSource, LedgerSource protocols
        qbo/       oauth, client, cdc, webhooks, normalizers
        lmn/       estimate_export, timesheet_export, closing_report_pdf
        generic/   csv templates
        payroll/   isolved_register
      domain/      jobs, estimates, billing, costs, labor, periods, exceptions
      wip/         calc.py (pure functions, Decimal only), journal.py, tieout.py
      reports/     queries + xlsx/pdf renderers
      api/         routers
      worker/      queue, tasks
    alembic/
    tests/
      fixtures/    anonymized Rye Beach files, QBO sandbox payloads
      golden/      hand-built WIP workbook → expected outputs
  frontend/        React app (firm console + tenant workspace)
```

**Testing strategy that matters here**: `wip/calc.py` is pure and is tested against a **golden workbook you build by hand** for ~8 jobs covering: normal underbilled, normal overbilled, deposit-only, loss job, change order mid-period, cost > EAC, job closed in period, retainage. If the code and your spreadsheet disagree, the spreadsheet wins until you say otherwise.

---

## 13. Phase 0 — Rye Beach groundwork (no code; start Monday)

This is the work that makes the tool possible, and it is valuable even if the tool is never built. It also becomes your onboarding checklist for client #2.

### 13.1 Confirm the plumbing
- [ ] QBO plan is Plus or Advanced and **Projects is enabled**.
- [ ] Determine where invoices originate (LMN → QBO sync, or keyed in QBO) and whether LMN currently creates customers/jobs in QBO. This decides who creates the project (D-01).
- [ ] LMN subscription tier (job costing and Zapier need Professional+).
- [ ] Are crews clocking to **jobs** in LMN Crew, or just clocking in/out? If not to jobs, labor job costing has no source, and fixing that is an operations project, not a software one.

### 13.2 One QBO project per sold job
- [ ] Naming convention with the LMN id in it: `6366990 Turley - E Dunbarton Rd`. Deterministic matching forever. Exception (D-30): a supplies pool is named `Pool – <group>` (for example `Pool – Hydroseed`) under a customer of the same name, since it has no estimate.
- [ ] Create projects for the 16 sold estimates. Decide Turley (one job or two) and DeVellis/Mukherjee (attach the $998.71 as a change order when sold).
- [ ] Re-tag this year's invoices, payments, bills, and expenses for those jobs to their project. This is the backfill that gives the tool history.

### 13.3 Ramp, built the way you want it
- [ ] Turn on the Customer/Job accounting field fed from QBO.
- [ ] Require it when the GL category is a job-cost account (51xx/52xx at minimum); leave it optional or hidden for overhead. If Ramp cannot condition the requirement on category, create a single `Overhead - No Job` value rather than letting people skip it.
- [ ] Restrict which GL accounts each cardholder group can pick (already on your checklist).
- [ ] Vendor rules for repeat suppliers set the account; the *person* sets the job.
- [ ] Bills through Ramp Bill Pay: job at the **line** level.

### 13.4 Chart of accounts additions (fits your numbering guide)
| No. | Name | Type |
|---|---|---|
| 1210 | Retainage Receivable | Other Current Assets *(only if GC contracts withhold)* |
| 1350 | Costs & Estimated Earnings in Excess of Billings | Other Current Assets |
| 2410 | Billings in Excess of Costs & Estimated Earnings | Other Current Liabilities |
| 2420 | Customer Deposits | Other Current Liabilities *(only if D-02 goes the liability route)* |
| 4190 | WIP Adjustment - LS | Income |
| 4290 | WIP Adjustment - EX | Income |

### 13.5 Collect sample files (these become test fixtures)
- [ ] LMN: the estimate export that includes **cost by category and labor hours** for sold estimates; the timesheet/job-cost export by job and employee; the contact export (it carries LMN's internal ids).
- [ ] isolved: payroll register by employee for two pay periods.
- [ ] QBO: Transaction Detail by Customer YTD; A/R aging detail; Invoice and Received Payments list.

### 13.6 Answer the deposit question by hand, once
For the 16 sold jobs, from QBO: first invoice date/amount, payments applied, total billed, open balance. One spreadsheet, maybe two hours. It answers the owner's question now, **and it becomes the acceptance test for Feature F08**: the tool must reproduce your spreadsheet.

### 13.7 Developer setup
- [x] Intuit developer account, app, sandbox company. Note the production-key questionnaire early. (Done: sandbox in F05; questionnaire submitted and production keys in place in F05.1, before the 2026-09-23 tie-out.)
- [x] Spike S-01 (half a day, throwaway script): connect to the real company read-only, confirm projects appear as customers with a project flag, confirm line-level customer refs on Ramp-synced expenses, confirm CDC returns deletes. Answers (`docs/spikes/S-01.md`, sandbox 2026-09-20; F05.1 on the real company):
  - Real company read-only: Rye Beach connected in F05.1 with scope `com.intuit.quickbooks.accounting` only; 13 months of billing totals equal QuickBooks' reports to the cent (tie-out 2026-09-23).
  - Projects as customers: a project is a `Customer` row with `IsProject = true`; `Job = true` alone means sub-customer. `ProjectRef` is a Projects-API id and is not used; the link is `CustomerRef` → the project's `Customer.Id`.
  - CDC deletes: returned inside the entity list as a stub (`Id`, `MetaData.LastUpdatedTime`, `status = "Deleted"`), no payload.
  - Line-level customer refs on Ramp-synced expenses: in the sandbox, `Purchase` lines carry `AccountBasedExpenseLineDetail.CustomerRef` and the header carries none. **On the real company: pending** three Ramp-synced Purchase or Bill ids that John assigned a Customer/Job to in Ramp; the owner supplies the ids and the answer is recorded here (ids only, no amounts).

---

## 14. Decisions you need to make

| # | Decision | Recommendation |
|---|---|---|
| D-01 | Who creates the QBO project when a job sells: a person, LMN's sync, or (later) this tool? | Person, using the naming convention, until volume hurts. |
| D-02 | Deposits: through income with WIP deferral, or a deposit liability? | Through income (§8.6). |
| D-03 | Turley: one job or two? General rule for multi-estimate customers? | One job per distinct scope/site that management tracks as a unit; phases as change orders only if priced against the same budget. |
| D-04 | Owned equipment and fuel in the WIP cost basis? | **Exclude from both sides in v1**; show as memo. Revisit once equipment hours by job are reliable, then include on both sides using internal rates. Never include on one side only. |
| D-05 | Labor burden policy: which costs, one rate or per division, reviewed how often? | Taxes + workers' comp + field-share of benefits; one rate per division; reviewed quarterly against the GL. |
| D-06 | Labor rate: actual employee rate, or crew average? | Actual, with division average as fallback when a rate is missing. |
| D-07 | Who approves EAC changes at the client, and how often? | Client admin monthly, before you close. Without this the WIP is arithmetic on stale guesses. |
| D-08 | Small-job threshold: do jobs under $X skip the WIP and recognize on billing? | Yes, tenant-configurable (e.g., < $5K and < 30 days). Cuts noise sharply: 4 of the 16 sold estimates are under $5K, and a fifth is $5,001.93. |
| D-09 | Product name. | Defer. CLAUDE.md requires the name live in one config constant. |

---

## 15. Risks, stated plainly

1. **Garbage labor in, garbage WIP out.** If crews do not clock to jobs, labor (the largest cost: $969K YTD) is unallocated and percent complete is materials-driven. This is the biggest risk and it is operational.
2. **Estimates without cost breakdowns.** If LMN's export does not give cost by category in a stable format, EAC must be keyed per job. Tolerable at 16 jobs; painful at 100. Find out in Phase 0.
3. **Backfill discipline.** The tool is only as good as project tagging in QBO. Ramp enforcement fixes card and bill spend; vendor bills entered directly in QBO and LMN-synced bills need the same rule.
4. **Intuit platform changes.** Partner-tier gating and metered reads moved in 2025–26 and may move again. Staying on the core REST API with incremental sync is the defensive posture.
5. **Scope creep toward a ledger.** Every request to "just fix it in the tool" should be answered by fixing it in QBO and re-syncing.
6. **One-person bus factor.** `OPERATIONS.md` (token rotation, restore from backup, how to re-run a normalizer, how to reopen a period) is written as features land, not at the end.
