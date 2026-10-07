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
  - As built (F07.3, 2026-10-04): Home is the set-up checklist (QuickBooks, suggestion rules, chart, account mapping, policy, burden rates; done or not, computed on read, one link each) and the job path (sold estimates to review, each open job's next need, tracked QuickBooks rows with no job). On a fresh company it is the onboarding guide; on a working one the worklist.

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
- A **QBO project/sub-customer** links to exactly one job. A sold job with no QBO link is an exception. A **tracked** QBO row (a customer, sub-customer or project a person picked, or one linked to a job) with no job is an exception; a row nobody has picked raises nothing and is listed nowhere except in the picker's search (D-37, replacing "a QBO project with activity and no job").
- **Division** comes from the estimate or is set on the job; cost lines whose GL account maps to a *different* division than their job raise a soft warning (mis-coding detector).
- `revenue_method` gains the value `pool` (D-30, D-31); the values are now `fixed_price`, `time_and_materials`, `recurring_service`, `none`, `pool`. A pool holds shared supplies until month-end allocation; it has no estimate and no contract, and a person sets the value (never inferred from the name).

**As built (F07, 2026-09-29).** Migration 0011; the three tables are tenant-scoped with RLS.
- `job`: `customer_id` (set by the first QuickBooks link to the linked row's parent, cleared with the last), `name`, `division_id` (required by the API), `revenue_method` (all five values; only `fixed_price` reaches the WIP schedule; `pool` and `recurring_service` jobs are made by hand with no estimate, D-30, D-35), `status` (D-35: `sold` = no money yet, backlog only; `in_progress` = the project exists, is linked and money has moved), `sold_on` (the original estimate's date, else the day the job was made), `notes`. **No contract column**: the revised contract is computed on read.
- `job_estimate(job, estimate, role, note)`: an estimate is on at most one job; a job has at most one `original`; a pool has none. **`ignored`** is a sold estimate that is superseded or was sold in error: it stays attached with a required reason so it never returns to the review queue, and it counts for nothing (no price, no change order, no EAC).
- `job_alias(job, system, external_id)`: systems `lmn_estimate` (written by attaching an estimate, any role) and `qbo_customer` (written by a person's link, the QuickBooks `Customer.Id` of a project, sub-customer or customer). One outside id → one job; a job may hold several QuickBooks rows. `lmn_job` waits for a source.
- `estimate_work_area.kind` (+ who, when): NULL until a person confirms original or change order on the job's original estimate; a new version carries the confirmation to the row with the same order number and name.
- Original contract = Σ kept work areas confirmed original on the original estimate (its header price when it has no work areas loaded). Change orders (confirmed on the original, or every kept work area of an estimate attached as `change_order`) are shown unapproved until a person records the customer's approval (F07.4, D-42); an approved one joins the revised contract at the price approved. Revised contract = original contract + approved change orders. EAC = Σ of the attached estimates' EAC in the basis, `ignored` excluded, approved or not (D-44). T&M: no revised contract (D-24); pool: no contract, no EAC (D-30); `recurring_service` (a maintenance or snow program, one per division-season): recognised as billed, no contract, no EAC (D-35).
- A QuickBooks project is created when the first money moves (D-35); linking a `sold` job offers to set it `in_progress` in the same action. A job at `in_progress` or `substantially_complete` with no QuickBooks link raises `JOB_NO_LEDGER_LINK`; a `sold`, `closed` or `cancelled` job never does. A `recurring_service` job accepts an estimate only as `ignored` (a sold maintenance or snow estimate, with a note).
- Suggestions (never decisions): attach candidates *same customer* / *by client name only* / *by name only*; QuickBooks candidates *estimate id in name* / *customer name* / *address* (exact token rules, `app/domain/jobs/names.py`), plus a search by name from which a person links by id. Customer duplicates are a read-only list; they are merged in QuickBooks.

**As built (F07.4, 2026-10-06, D-42, D-44).** Migration 0013: `change_order_approval` (tenant-scoped, RLS forced, append-only): one row per approval or withdrawal of a change-order work area, never edited. An approval records the user (under their own login), the date the customer agreed, who at the customer agreed (optional), a reference to the evidence (optional, or required by the tenant's `change_order_evidence` policy), a note, and the work area's price that day; a withdrawal records the user, when, a reason and the approval it withdraws. The key is `(estimate_id, order_no)`, the identity F07 gives a work area; the stored name and price are the guard on read, never a search in a write path (the write takes the row the person pressed on). Whether an approval applies is computed on read (rule C, the owner, 2026-10-06): it applies while every version with work areas received after the one it was made on carries a row at that order number that is kept, has the same name and the same price, and the latest row is confirmed as a change order (or is a kept row of an estimate attached as a change order); any break ends it for good, a later version that restores the name and price does not revive it, and the platform writes no withdrawal on its own (`CO_APPROVAL_NOT_CARRIED` says so until a person approves again or withdraws). `client_pm` and `firm_admin` approve and withdraw (`can_approve_change_orders`); `client_admin`, `firm_staff` and `client_viewer` do not. A confirmed change order with an applying approval cannot be changed to original. Revised contract = original contract (confirmed originals) + approved change orders; EAC is unchanged by approval (D-44). `GET /api/change-orders/unapproved` lists every kept, confirmed, unapproved change-order work area on a sold, in progress or substantially complete job, with its age (the received date of the first version in the unbroken run carrying the row with that name).

**As built (F08.1 Part 1, 2026-10-07, D-45).** Migration 0014: `billing_line_work_area` (tenant-scoped, RLS forced, append-only): a person's assignment of an earlier invoice, credit-memo or sales-receipt line to a work area, one row per event (`assigned` or `cleared`), never edited; the latest row for a line is its state. A line is tied to a work area in D-45's order: by its pay application (F08.1 Part 2), by "#n" at the start of its description when n is a work area of the original estimate and of no other attached estimate (owner, 2026-10-07), or by a person's assignment, which follows the work-area number through later versions as a "#n" line does (a renamed work area shows `EST_WORK_AREA_RENUMBERED` beside the line; a dropped one leaves the line not assigned until a person acts). The assignment is by id (`firm_admin`, `firm_staff`, `client_admin`); the one suggestion is a kept work area whose name equals the description under `same_name`, shown and never applied by rule, and "Confirm all as suggested" sends the pairs of ids the screen showed. Billed to date per work area is Σ of the tied lines, a credit memo's negative, computed on read and nowhere stored; "Not assigned to a work area" is the job's billed to date less every tied line (sales tax, fuel surcharge lines, discount lines and the deposit invoice are never tied). `BILLING_UNAPPROVED_CO` is raised when billed to date on kept change orders without an applying approval is above 0.00, naming the amount and the work areas, on the board, the job page and Home (ahead of the F07.4 needs); it changes no figure and never mentions payment.

**As built (F08.1 Part 2, 2026-10-07, D-26, D-36, D-39, D-43).** Migration 0015: `pay_application` (per fixed-price job, numbered one more than the highest `_PMT` on its invoices then continuing; `draft`, `issued`, `void`; the person's surcharge choice with no default; frozen at issue: the rate, billed before and the amount due) and `pay_application_line` (the schedule of values as it stood at issue: the work area by `(estimate_id, order_no)`, its scheduled value, its cumulative percent). Every other figure is computed on read: earned to date per work area half up to the cent, earned on previous applications (the latest earlier issued application listing the work area, else Part 1's tied lines), earned this application, balance to finish; the summary's billed before is the job's whole billed to date as QuickBooks has it over every counted document dated on or before the application date (the owner's answer A, 2026-10-07), never counting a `_PMT<m>` invoice with m ≥ n; amount due never below 0.00, else "Billed ahead by x" and no invoice due; the surcharge on the amount due when chosen (D-39). The schedule lists kept originals and change orders approved on the application date (D-42); the four D-26 exceptions leave a work area off the draft, one sentence each. A billing request is entered by `client_pm`, `client_admin`, `firm_staff` and `firm_admin`; issue and void by the latter three (answer C), one audit row each; an issued application is never edited. The invoice keyed from the application (`<estimate number>_PMT<n>`) is matched by number and tied in D-39's two parts; its lines are tied by the application (D-45's first slot) and each work area is billed what the application earned this application; `PAYAPP_NOT_INVOICED`, `INVOICE_NO_PAYAPP` (invoices dated on or after the first issued application) and `PAYAPP_INVOICE_MISMATCH` are review sentences. The PDF (reportlab, D-40) is built from the same figures with the words the owner approved; no retainage anywhere (D-43).

**As built (F07.2, 2026-10-04, D-37).** Migration 0012: `customer.tracked_at` and `customer.tracked_by` (NULL = not tracked; a CHECK keeps who with when; no new table). A person tracks a row by its id on the Customers page (the picker: a paged search over active rows by name, projects first; empty text returns nothing), or the link to a job tracks it in the same action (the `job_alias_linked` row records `customer.tracked_at` before and after); untrack is refused while a link exists; one audit row per track and untrack (`customer_tracked`, `customer_untracked`). Rows linked before 0012 were tracked by the migration's data step as of the link, by the user who made it. The sync writer never names the two columns, so a change poll leaves the flag alone. `LEDGER_PROJECT_NO_JOB` is raised for a tracked, active row with no job, with or without documents, and for no other row; a tracked row QuickBooks makes inactive stays tracked, is listed as inactive and raises nothing. The duplicates list is unchanged behind a link. What is fetched and held, the month totals and every contract figure are unchanged.

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

- Interface: `EstimateSource.parse(file) -> list[EstimateDTO]`. DTO: external id, status, customer, jobsite, name, estimator, dates, price, its work areas (order number, kept or omitted, name, price; D-01) and, under each work area, its **cost lines** (cost code, hours, amount; D-32). Estimated cost by cost category is the sum of the cost lines over kept work areas.
- The one implementation (D-32, 2026-09-25): the platform's own **estimate template**, a workbook with the sheets *Estimates*, *Work areas* and *Estimate costs* (or one CSV per sheet), filled by the estimator on the tenant's cost codes (D-23). Estimators code estimates to the grid directly; the platform never infers a code from a name or a vendor's item type. The vendor-export and closing-report-PDF implementations of the first draft are struck: a parser for a vendor's screen layout would tie the platform to it and still not yield a category split. The blank template is `docs/templates/estimate_template.xlsx`; F06 built it.

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
**Spine**: `customer`, `job`, `job_alias`, `estimate`, `estimate_version` (one per accepted upload; the D-01 baseline), `estimate_work_area` (per version; identity `order_no`), `estimate_cost(estimate_work_area, cost_category, division, cost_code, hours, amount)` (D-32: keyed to the work area, not the estimate), `job_estimate(job, estimate, role)`, `change_order_approval` (F07.4, D-42: one row per approval or withdrawal of a change-order work area, keyed by estimate and `order_no`; append-only), `eac_revision`
**Billing**: `billing` (invoice/credit memo/sales receipt, header), `billing_line`, `payment`, `payment_application(payment → billing, amount)`, `retainage` fields on billing, `billing_line_work_area` (F08.1, D-45: a person's assignment of a line to a work area, by `(estimate_id, order_no)`; append-only), `pay_application` and `pay_application_line` (F08.1 Part 2, D-36: the schedule of values as it stood at issue and the three figures frozen at issue)
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
| 1 | Original contract | Σ kept work areas confirmed original on the job's `original` estimate (its header price when none are loaded) (F07, D-01) |
| 2 | Approved change orders | Σ price of the kept change-order work areas (confirmed on the original, or any kept work area of an estimate attached as `change_order`) with an approval that applies (F07.4, D-42): recorded by `client_pm` or `firm_admin`, at the work area's price that day, ended for good by any later version that changes its name or price |
| 3 | **Revised contract** | 1 + 2 |
| 4 | Original estimated cost | From estimate cost breakdown, WIP-basis categories only |
| 5 | **Estimated total cost (EAC)** | 4 + CO cost + `eac_revision`s. Must be ≥ cost to date. CO cost is the estimated cost of every kept change-order work area, approved or not (D-44) |
| 6 | Estimated gross profit | 3 − 5 |
| 7 | Cost to date | WIP-basis job cost through period end |
| 8 | **Percent complete** | 7 ÷ 5, capped at 100% |
| 9 | Earned revenue | 8 × 3 |
| 10 | Billed to date | Invoices − credit memos + sales receipts through period end (incl. retainage billed), less sales tax (owner, 2026-10-06) and less fuel surcharge lines (D-39); the deposit invoice counts from its date (D-02). F08 computes it on read. Per work area (F08.1, D-45): Σ of the lines tied to it, by pay application, then "#n", then a person's assignment; a line with no tie counts in the job's figure and in no work area's. |
| 11 | **Over / (under) billed** | 10 − 9 |
| 12 | Gross profit to date | 9 − 7 |
| 13 | Backlog | 3 − 9 |
| 14 | Prior-period GP%, and **fade/gain** | change in column 6 ÷ 3 since last approved period |

Loss jobs: when column 6 is negative, the full projected loss is recognized now (earned revenue is reduced so GP to date equals the total expected loss) and the line is flagged.

Supporting memo columns: collected to date (cash only, D-41), other credits applied (D-41: invoices settled through a payment by something other than cash; billed − collected − other credits applied = open A/R), open A/R, retainage held, deposit invoiced, deposit received, fuel surcharge billed (D-39), unapplied payments (D-02), last cost date, last billing date, EAC last reviewed date and by whom.

### 8.3 What counts as "cost" (WIP basis)
EAC in the basis counts the estimated cost of every kept work area on a job's attached estimates, including change-order work areas that are not approved (D-44): approving a change order moves its price into the revised contract and never moves EAC, so until it is approved the job carries the cost with none of the price, and percent complete and margin are understated rather than overstated.

Controlled by `account_map.in_job_cost` and tenant policy. Both the numerator (cost to date) and denominator (EAC) must use the **same** categories. See Decision D-04 for owned equipment. Default recommendation: burdened labor + materials + supplies + subs + rentals + disposal + permits; owned equipment and fuel excluded from the WIP fraction and shown as memo on the profitability report. Pooled supplies reach jobs by month-end allocation per D-30 and are in the WIP basis on both sides.

Labor burden (slot 20) is in the basis on both sides, as D-05 decides and D-34 applies: one effective-dated rate per division, a fraction of wages, applied to labor on both sides of percent complete. Cost to date carries it per `labor_entry` (F11); EAC carries it as Σ Labor (slot 10) cost lines on kept, priced work areas × the rate in force for the division on the estimate date (else the version's received date in the tenant's time zone), quantized `ROUND_HALF_UP` per work area and division. On an estimate burden is computed when it is read and never written: the cost lines stay as loaded and the detail shows cost as estimated and with burden side by side (D-34); a slot-20 line an estimator keys is shown as estimated, left out of EAC and raises `EST_BURDEN_LINE`; with no rate in force, EAC in the basis is not computed. Rye Beach's rates from 2026-01-01: LS 0.2136, EX 0.1959, GC 0.2207, SNOW 0.2061 (D-34 replaced D-05's 0.1713 for SNOW).

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

1. **Revenue**: billed-in-period across all jobs + unassigned income = GL income accounts for the period. Fuel surcharge billed (D-39) and T&M revenue (D-24) are revenue outside the schedule and are shown as such; F08 ties the billing side month by month: jobs + not on a job = the QuickBooks month totals for billed (invoices − credit memos + sales receipts, tax and surcharge included) and for collected (payments + sales receipts), to the cent; collected to date is cash (D-41), so a credit applied through a payment never enters it.
2. **Cost**: GL-direct job cost + unassigned COGS = GL 5xxx for the period.
3. **Labor**: computed job labor (unburdened) vs GL gross payroll COGS accounts. The difference is shown as unallocated labor (shop time, travel, PTO) with a tenant-set tolerance.
4. **A/R**: open balances by job + unassigned = GL 1200.
5. **Roll-forward**: prior approved snapshot + period activity = current snapshot, per job.
6. **Ratio checks** (warnings): payroll tax % by division, materials % of revenue by division versus trailing average. This would have caught the SNOW payroll-tax anomaly.
7. **Pool balances** (D-30): every pool job balance is 0.00 at period end, or waived with a note.

### 8.6 Deposits (Decision D-02, closed 2026-10-01)
A customer deposit is **one advance invoice** on the job's QuickBooks project (D-35): document number `<estimate number>_DEP` (D-26), to the division's income account, never a percent of each work area. It counts in billed to date from the invoice date. No customer-deposit liability account is used and 2420 is not added to the chart. At period end the WIP entry (§8.4) defers whatever is billed ahead of earned revenue (DR 4n90 WIP Adjustment / CR 2410, reversing on day 1 of the next period), so a job with a deposit and no cost to date shows as overbilled by the deposit, which is the condition the entry exists to correct. On each later pay application (D-36) the deposit is netted at the job level, inside "less billed to date before this application", never line by line. The platform identifies a deposit by the `_DEP` document number and the tenant's configured deposit item (the "first invoice before first cost" rule stays a suggestion only) and reports deposit invoiced and deposit received per job (§8.2 memo columns; §9 report 1). A payment with no invoice to apply it to is not billed to date: it is shown on the job as an unapplied payment and raises a review item until an invoice exists. The alternative (a 2420 liability with a reclass at every progress invoice) would hold under another name what 2410 already holds, and add an apply step every client will forget.

### 8.7 Output legend
Every exported schedule carries a tenant-configurable footer (default: "Prepared by management from the company's records. No assurance is provided."). You will know better than I do how this interacts with your SSARS obligations once schedules start going to banks and sureties; the tool just needs to make the legend impossible to forget.

---

## 9. Reports (in build order)

1. **Sold Jobs Board** — every sold job: contract, change orders, deposit invoiced/received, billed, fuel surcharge billed (D-39), collected (cash, D-41), other credits applied (D-41), open A/R, remaining to bill, days since last activity, estimator. *This is the report that answers your deposit question.* **As built (F08, 2026-10-06)**: the Jobs page is the board; every figure is computed when read from the F05 rows through the `qbo_customer` aliases (nothing stored); a totals row and one "Not on a job" row (D-35, D-37); the tie-out status on screen; XLSX (values from Decimal, a tie-out tab) and PDF (D-40). Report 2's billing half: the job page's Billing section with the billing and payment histories.
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

**The pay application (F08.1 Part 2, 2026-10-07; D-36, D-39)** is a customer document, not a report: for a fixed-price job, the schedule of values (kept originals and approved change orders) with earned to date, earned on previous applications, earned this application and balance to finish per work area; the summary deducts the job's whole billed to date before the application as QuickBooks has it (the owner's answer A; no separate line for billing outside the schedule of values), the amount due never below 0.00, "Billed ahead by x" when billed exceeds earned, and the fuel surcharge when the person says it applies. On screen and as PDF from the same figures; the invoice is keyed in QuickBooks from it and tied by document number. No legend on the PDF.

---

## 10. Exception types (seed list)

| Code | Trigger | Severity |
|---|---|---|
| `EST_NO_ID` | Imported estimate lacks an external id (Mijal) | warn |
| `EST_ZERO_SOLD` | Sold estimate with $0 price | warn |
| `EST_UNATTACHED` | Sold estimate not attached to a job (F07: the review queue; the brief's `EST_SOLD_UNREVIEWED`) | block-close |
| `EST_NO_COST` | Sold estimate without cost breakdown (cannot enter WIP) | block-close for fixed-price |
| `JOB_NO_LEDGER_LINK` | Open job has no QBO project/customer (F07; the brief's `JOB_NO_QBO_LINK`) | block-close |
| `LEDGER_PROJECT_NO_JOB` | Tracked, active QBO row (customer, sub-customer or project) with no job, with or without documents (F07.2, D-37; F07 raised it for any project or sub-customer with a document; the brief's `QBO_PROJECT_NO_JOB`) | block-close |
| `JOB_SECOND_ESTIMATE_FOR_CUSTOMER` | Sold estimate to review whose customer already has a job, by project id or client name (F07, D-03) | warn |
| `JOB_DIVISION_UNSET` | Job with no division (F07; only data made outside the review) | warn |
| `COST_UNASSIGNED` | In-job-cost GL line with no job | warn, totals shown on tie-out |
| `COST_DIVISION_MISMATCH` | Line's account division ≠ job division | warn |
| `BILLED_OVER_CONTRACT` | Billed > revised contract (likely missing change order); F08 raises it on read when remaining to bill is negative | warn |
| `CO_APPROVAL_NOT_CARRIED` | A change order's approval ended because a later version of the estimate changed the work area's price or name, omitted it or dropped it (F07.4, D-42, rule C); one sentence naming the approved price and date and what the version did, until a person approves again or withdraws | warn |
| (Home need) `change_orders_unapproved` | A sold, in progress or substantially complete job with kept, confirmed change orders no one has approved: "n change orders, 53,704.13, not approved" (F07.4); the unapproved change orders list shows them with their age | info |
| `PAYMENT_UNAPPLIED` | A payment on the job's customer rows with money not applied to any invoice (D-02; F08). Not billed or collected to date until applied | warn |
| `PAYMENT_OTHER_CREDIT` | An invoice on the job settled through a payment by something other than cash, a journal entry or a deposit (D-41; F08.2). Shown as other credits applied, outside collected to date; one sentence per payment with the amount and the date | warn |
| `BILLING_UNAPPROVED_CO` | Billed to date on kept change-order work areas without an applying approval is above 0.00 (D-45; F08.1 Part 1): one sentence per job naming the amount and the work areas, on the board, the job page and Home. A flag only: billed to date, the revised contract, remaining to bill, EAC and over / (under) billed are unchanged, and it never mentions payment or proposes approval | warn |
| `BILLING_OVER_100` | A billing request names a work area above 100.00% (D-26; F08.1): one sentence, the work area left off the draft, the other lines stand | warn |
| `BILLING_UNPRICED_CO` | A billing request names a work area priced 0.00 (D-26; F08.1): left off the draft | warn |
| `BILLING_OMITTED_AREA` | A billing request names a work area omitted from the estimate (D-26; F08.1): left off the draft | warn |
| `BILLING_NEGATIVE` | A billing request names a percent below the previous application's (D-26; F08.1): left off the draft | warn |
| `PAYAPP_NOT_INVOICED` | An issued pay application with an amount due and no invoice `<estimate number>_PMT<n>` in QuickBooks (D-36; F08.1): key it | warn |
| `INVOICE_NO_PAYAPP` | An invoice on the job dated on or after the job's first issued pay application that is not an application's and not the deposit (D-36; F08.1; the format changes job by job) | warn |
| `PAYAPP_INVOICE_MISMATCH` | The invoice less its fuel surcharge lines differs from the amount due, or its fuel surcharge lines from the printed surcharge (D-39; F08.1): the sentence names the difference | warn |
| `DEPOSIT_NOT_IDENTIFIED` | A `_DEP` document not on a deposit item, or a deposit item on a document that is not `<estimate number>_DEP` (D-02, owner's answer 2; F08). It still counts in billed to date | warn |
| `COST_OVER_EAC` | Cost to date > EAC | block-close until EAC revised |
| `EAC_STALE` | Job > X% complete or > N days since EAC review | warn |
| `CUSTOMER_FUZZY` | Possible duplicate or mismatch (Hosmer/Hossler); F07: the read-only Customers list, exact token rules | info |
| `LABOR_NO_JOB` / `LABOR_NO_RATE` | Timesheet row unmatched to job or employee rate | warn |
| `PRIOR_PERIOD_CHANGED` | Source data dated in an approved period changed | warn, shows delta |
| `WARRANTY_REVENUE` | Warranty-type job has billings | info |

---

## 11. Multi-tenancy, roles, security

**Shape**: `firm` (your practice) → `tenant` (client company) → `membership` (user ↔ tenant ↔ role). Firm staff hold memberships in many tenants; client users in one. No self-serve signup; you create tenants.

**Roles**: `firm_admin`, `firm_staff`, `client_admin` (owner/controller: sees everything for their company, approves EAC), `client_pm` (their jobs, no pay rates, can propose EAC; F07.4, D-42: approves a change order and withdraws an approval under their own login, as does `firm_admin`; `client_admin`, `firm_staff` and `client_viewer` do not), `client_viewer`. F08.1 Part 2 (the owner's answer C, 2026-10-07): a billing request is entered by `client_pm`, `client_admin`, `firm_staff` and `firm_admin`; a pay application is issued and voided by `client_admin`, `firm_staff` and `firm_admin`; every role reads the applications and the PDF.

**Isolation**: shared schema, `tenant_id` on every row, **Postgres Row-Level Security** on every tenant table, policy keyed to `current_setting('app.tenant_id')`, set with `SET LOCAL` inside each request's transaction. The application DB role is not the table owner and has no `BYPASSRLS`. Migrations run as a separate owner role. A CI test creates two tenants and proves that every table with a `tenant_id` column has RLS enabled and that cross-tenant reads return zero rows. Background jobs take a tenant id explicitly and set the same context. Removing a tenant altogether is an operator action on the server with the owner role, never an API call: it is the one time the append-only triggers are suspended, for that transaction only, and it leaves a firm-level audit row (D-28).

**Secrets**: OAuth tokens and pay rates encrypted at the application layer (envelope key from environment/secret manager, key id stored with the ciphertext so keys can rotate). Files in object storage under `tenant/{id}/…`, private, short-lived signed URLs.

**Audit**: append-only `audit_log` for logins, connection changes, crosswalk decisions, EAC revisions, policy changes, period approvals and re-opens, exports.

**Auth**: email + password with mandatory TOTP for firm roles; consider a hosted identity provider rather than hand-rolling. Session cookies, not tokens in local storage.

**Practice hygiene outside the code**: engagement-letter language on data access and on who owns the estimates; Intuit's production-key security questionnaire; a written information security plan. None are hard, all are easier before the second client than after.

---

## 12. Stack and layout

Same stack you already run in production, so nothing new to operate: Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres 16, React 18 + Vite (plain JS), DigitalOcean (App Platform or a droplet, Managed Postgres, Spaces). One addition: a worker process with a **Postgres-backed job queue** (no Redis) for syncs, imports, and report rendering. PDF documents (report exports from F08, the pay application in F08.1) are produced with `reportlab`, pinned (D-40); XLSX with openpyxl.

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
- [ ] Naming convention with the LMN id in it: `6366990 Turley - E Dunbarton Rd`. Deterministic matching forever. Both spellings are accepted, `6366990 …` and `EST6366990 …`: the id first, followed by a non-digit (F07 suggests the project on that rule; the link itself is a person's, by id). Exception (D-30): a supplies pool is named `Pool - <group>` (as spelled in QuickBooks, with a hyphen: `Pool - Hydroseed`; D-30 wrote an en dash, and matching is by id) under a customer of the same name, since it has no estimate. Second exception (D-35): maintenance and snow run as one program project per division-season, `Maintenance <year>` and `Snow <season>`, each under a customer of the same name. A construction project is created when the first money moves, not when the estimate sells (D-35).
- [ ] Create projects for the 16 sold estimates (D-03: 16 jobs). Turley is two projects, `6366990 Turley - 378 E Dunbarton Rd` and `6120638 Turley - Landscape Projects 2026`; DeVellis/Mukherjee is one project, `6346291 …`, and EST6281138 ($998.71) is attached to its job as a change order when it sells.
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
| 2420 | Customer Deposits | *Not created* (D-02, 2026-10-01: a deposit is one advance invoice through income; 2410 defers it) |
| 4190 | WIP Adjustment - LS | Income |
| 4290 | WIP Adjustment - EX | Income |

### 13.5 Collect sample files (these become test fixtures)
- [x] ~~LMN: the estimate export that includes **cost by category and labor hours** for sold estimates~~ — no longer needed (D-32): estimates come in on the platform's template with cost lines by cost code; the two template fixtures (EST6115758, EST6120638) are in `tests/fixtures/rye_beach/estimates/`. Still wanted: the timesheet/job-cost export by job and employee (F11); the contact export (it carries LMN's internal ids).
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
  - Line-level customer refs on Ramp-synced expenses: **yes** (owner, 2026-09-25, `scripts/s01_q4.py` on the real company). Three Ramp-synced Bills with a Customer/Job coded per line in Ramp, every line `AccountBasedExpenseLineDetail` and every line carrying `CustomerRef`: Bill Id 98892 (1 line) → CustomerRef 100000091, 1 of 1; Bill Id 98908 (4 lines) → CustomerRef 6087, 4 of 4; Bill Id 98890 (7 lines, the D-30 pool bill) → CustomerRef 6415 (`Pool:Pool - Hydroseed`), 7 of 7. The `name` arrives as the customer's `FullyQualifiedName` (`Parent:Project`), so matching stays by id. The sandbox showed the same shape on `Purchase` lines, with nothing on the header. Purchases on the real company are checked when Ramp card transactions begin (F10). The QuickBooks pool project (D-30) is spelled `Pool - Hydroseed` with a hyphen; D-30's en dash stands because matching is by id.

---

## 14. Decisions you need to make

| # | Decision | Recommendation |
|---|---|---|
| D-01 | Who creates the QBO project when a job sells: a person, LMN's sync, or (later) this tool? | Person, using the naming convention, until volume hurts. |
| D-02 | Deposits: through income with WIP deferral, or a deposit liability? | Closed by D-02 (2026-10-01): one advance invoice (`_DEP`) through income, deferred by the WIP entry against 2410; 2420 not created; netted at the job level on each pay application (D-36). Applied by F08, F08.1. |
| D-03 | Turley: one job or two? General rule for multi-estimate customers? | Closed by D-03 (2026-09-27): one job per sold estimate unless a person attaches it to an existing job as a change order; Turley is two jobs, DeVellis/Mukherjee one. Applied by F07. |
| D-04 | Owned equipment and fuel in the WIP cost basis? | **Exclude from both sides in v1**; show as memo. Revisit once equipment hours by job are reliable, then include on both sides using internal rates. Never include on one side only. |
| D-05 | Labor burden policy: which costs, one rate or per division, reviewed how often? | Closed by D-05 (2026-09-27): taxes + workers' comp at the assigned class + employer share of benefits; one effective-dated rate per division, applied to both sides; reviewed quarterly with the WC-to-941 tie-out. Applied on estimates by F06.1. |
| D-06 | Labor rate: actual employee rate, or crew average? | Actual, with division average as fallback when a rate is missing. |
| D-07 | Who approves EAC changes at the client, and how often? | Client admin monthly, before you close. Without this the WIP is arithmetic on stale guesses. |
| D-08 | Small-job threshold: do jobs under $X skip the WIP and recognize on billing? | Yes, tenant-configurable (e.g., < $5K and < 30 days). Cuts noise sharply: 4 of the 16 sold estimates are under $5K, and a fifth is $5,001.93. |
| D-09 | Product name. | Closed by D-33 (2026-09-26): `jobcost.dev`, one string with the hostname (D-27), in one constant per side. |
| D-34 | SNOW burden rate; is burden stored on an estimate? | Closed by D-34 (2026-09-29): SNOW 0.2061 at each employee's assigned WC class; burden is shown beside estimated cost, never written into it. |
| D-35 | When is a QuickBooks project created, and how are maintenance and snow tracked? | Closed by D-35 (2026-09-29): a construction or excavation project is created when the first money moves; a sold job without one is backlog; maintenance and snow run as one program project and one `recurring_service` job per division-season. Applied by F07. |
| D-36 | What does the customer receive for a progress billing, and what does the invoice carry? | Closed by D-36 (2026-10-01): the platform produces a pay application with a schedule of values (kept original and approved change-order work areas; earned to date, retainage, less billed before, amount due); the invoice stays in QuickBooks, keyed from it (`_PMT<n>`, one line, "Pay application n") and tied to it by document number. Applied by F08.1; D-26's "#n" line rule stays for invoices keyed before pay applications are in use. Amended by D-43 (no retainage) and D-45 (how earlier invoice lines reach a work area). Applied by F08.1 Part 2 (2026-10-07; the owner's answer A: the summary deducts the job's whole billed to date before the application). |
| D-37 | Which QuickBooks customers and projects do the screens work on: every row the sync holds, or the ones a person picks? | Closed by D-37 (2026-10-04): a person picks (tracks) rows by id on the Customers page, and a link to a job tracks the row; the screens and `LEDGER_PROJECT_NO_JOB` work from tracked rows only; what is fetched and held, the month totals and every tie-out are unchanged. Applied by F07.2. |
| D-38 | Does a tracked row that QuickBooks has made inactive still ask for a job? | Closed by D-38 (2026-10-04, amends D-37): no; it stays tracked, reads "Inactive in QuickBooks" and raises nothing. F07.2 as built. |
| D-39 | A fuel surcharge on a fixed-price job: contract, or recognised as billed? | Closed by D-39 (2026-10-06): outside the contract, recognised as billed, excluded from billed to date (§8.2 column 10) and shown as its own figure; recognised by item id (`fuel_surcharge_treatment`: the items and the rate); printed on the pay application. Applied by F08; the per-application choice and the two-part tie by F08.1. |
| D-40 | Which library produces the platform's PDF documents? | Closed by D-40 (2026-10-06): `reportlab`, pinned, one library for every PDF; built from the same Decimal figures as the screen and the XLSX. Applied by F08. |
| D-41 | Is an invoice settled through a payment by a journal entry or a deposit collected? | Closed by D-41 (2026-10-06): collected to date is cash (a payment's total less unapplied); the net of a payment's lines of any other type is its remainder, on the payment's own row; a credit remainder is shown as "Other credits applied" and raises `PAYMENT_OTHER_CREDIT`. Applied by F08.2. |
| D-42 | Who approves a change order, on what evidence, and from when does it count? | Closed by D-42 (2026-10-06): the project manager (`client_pm`) or `firm_admin` marks it approved under their own login, recording the date the customer agreed, optionally who agreed, a reference and a note; the evidence required is a tenant policy with no default (`change_order_evidence`: none required, or a reference required; Rye Beach: none). An approval is for the work area at its price that day; a later version that changes the price or name ends it (rule C: for good, no revival; owner 2026-10-06); withdrawal with a reason; history never edited, one audit row each; counts in the revised contract from the approval date, an approval or withdrawal after a period is approved takes effect in the first open period (F14). Applied by F07.4. D-01 completed as to sign-off. |
| D-43 | Retainage | Closed by D-43 (2026-10-06): not built; F08.1 carries no retainage line; a decision records the treatment (invoicing, QuickBooks, billed to date, over/(under) billed, the fuel surcharge base of D-39) before any tenant that holds or is subject to retainage is taken on. |
| D-44 | Does EAC include the estimated cost of unapproved change orders? | Closed by D-44 (2026-10-06): yes; approving a change order moves its price into the revised contract and never moves EAC; until then the job carries the cost with none of the price. Applied by F07 and F07.4 (no change in behaviour; now decided); F14 uses this EAC. |
| D-45 | Billing on a change order no one has approved: adjust a figure, or flag it? And how does an earlier invoice line reach a work area? | Closed by D-45 (2026-10-06): flag, never adjust: `BILLING_UNAPPROVED_CO` names the amount and the work areas and changes nothing. A line is tied by its "#n" (D-26) or, where it has none, by a person's assignment picked by id; the description and the position are suggestions only. Billed to date per work area reads pay applications, then "#n" lines, then assignments. D-26 and D-36 amended as to that. Applied by F08.1 Part 1 (2026-10-07). |

---

## 15. Risks, stated plainly

1. **Garbage labor in, garbage WIP out.** If crews do not clock to jobs, labor (the largest cost: $969K YTD) is unallocated and percent complete is materials-driven. This is the biggest risk and it is operational.
2. **Estimates without cost breakdowns.** If LMN's export does not give cost by category in a stable format, EAC must be keyed per job. Tolerable at 16 jobs; painful at 100. Find out in Phase 0.
3. **Backfill discipline.** The tool is only as good as project tagging in QBO. Ramp enforcement fixes card and bill spend; vendor bills entered directly in QBO and LMN-synced bills need the same rule.
4. **Intuit platform changes.** Partner-tier gating and metered reads moved in 2025–26 and may move again. Staying on the core REST API with incremental sync is the defensive posture.
5. **Scope creep toward a ledger.** Every request to "just fix it in the tool" should be answered by fixing it in QBO and re-syncing.
6. **One-person bus factor.** `OPERATIONS.md` (token rotation, restore from backup, how to re-run a normalizer, how to reopen a period) is written as features land, not at the end.
