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
| S-01 | QBO read-only spike: projects as customers, line-level refs, CDC deletes (sandbox in F05; Ramp line check in F05.1) | ☑ (2026-09-25; answers in BLUEPRINT §13.7; question 4 yes on the real company; Purchases checked when Ramp card transactions begin) |
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

### F04.1 · Policy screen a person can answer  ☑ (2026-10-05; built and owner's pass on jobcost.dev, tenant `rye-beach`, the same day, see `docs/briefs/F04.1.md`)
A patch under F04 (no table, no migration, no policy key, no default). The time zone is a list of the United States zones in plain words; the decision reference is optional (who and when are still recorded); the three keys whose features have not arrived show why in place of "Decide" and refuse a PUT; "Not decided" lines up on the left; the WIP basis edit says what to tick.
**Accept**: as in `docs/briefs/F04.1.md`: a PUT with no reference or a blank one returns 200 with who, when, no reference and one audit row; the waiting keys are refused with their sentence, writing nothing, and a value already stored on one is still read; a valid zone off the list is kept; no default anywhere; the role matrix and Home's Policy line unchanged. Owner's pass on `rye-beach`: Eastern with no reference; the WIP basis with "D-04, D-05"; the three sentences; Home's Policy line reads done.

---

## Phase B — Estimated vs. Billed (first usable release)

### F05 · QBO connection & sync  ☑ (2026-09-21; owner pass on the live sandbox 2026-09-21)
Against the Intuit sandbox only (D-25). OAuth2 connect/reconnect, token refresh, backfill, CDC polling, deletes/voids, nightly drift check; no webhooks (F05.1). Entities per §6.2, all stored raw. Normalize Customers/projects, Invoices, Payments, Credit Memos, Sales Receipts into `customer`, `billing`, `payment`, `payment_application`; Account ids are attached to `gl_account` by account number; Deposits are stored raw pending D-02.
**Accept**: against sandbox: invoice and payment totals by month equal QBO reports; deleting an invoice in sandbox removes it after next sync; token expiry triggers a reconnect exception, not a crash.

### F05.0 · First deployment (jobcost.dev)  ☑ (2026-09-22; owner pass from a phone 2026-09-22)
The platform live at jobcost.dev, with the privacy policy and terms pages Intuit requires for production keys (D-25). One droplet, Managed Postgres, a private Space, manual deploys over ssh (D-27).
**Accept**: as in `docs/briefs/F05.0.md`: valid certificate and headers, the three static pages without sign-in, `deploy.sh` migrates before it restarts and stops on a failed migration (proven on a scratch database), services as `wip` with the owner URL held by root only, `prod_check.py` passes, the database port closed to the internet, the real client IP in the audit log, the env-template test, CI without the droplet, the owner's phone pass.

### F05.1 · QBO production connection  ☑ (2026-09-25; Rye Beach connected 2026-09-22, tie-out 2026-09-23, owner pass from a phone 2026-09-25)
Intuit's production questionnaire and keys, Rye Beach connected read-only, monthly totals tied to Rye Beach's own QuickBooks reports, webhooks turned on (D-25). S-01's fourth question, whether Ramp-synced expenses carry the job on the line, is answered here against the real company. The Phase B gate applies.
**Accept**: Rye Beach read-only: invoice and payment totals by month equal QBO reports; webhooks turned on; Intuit production assessment submitted.

### F05.2 · Brand assets  ☑ (2026-10-02; built 2026-09-26; swap test passed 2026-10-02 and the new logo kept; owner pass 2026-10-02 on jobcost.dev, see `docs/briefs/F05.2.md`)
An addition after F05.1; nothing moved. One image is the brand: `frontend/public/brand/logo.svg` (the owner's logo; the original hammer was replaced on 2026-10-02) with an optional `logo-mark.svg` for the 16 and 32 px favicons (none at present; they come from the logo); `npm run brand` generates the favicons, touch and PWA icons, manifest and Open Graph image from them and `brand.json`, all committed; the app shell, sign-in page and the three static pages show the logo from the one file; `SITE_HOST` beside `PRODUCT_NAME` (D-09 untouched). No change to the backend, the CSP, the nginx site file or `deploy.sh`.
**Accept**: as in `docs/briefs/F05.2.md`: the generator is idempotent and its outputs have the stated sizes; CI fails on a swapped logo without regeneration; the product-name, deploy and frontend-style tests pass unchanged; the owner's browser pass (tab, sign-in, header, `/privacy`, link preview, Add to Home Screen) and one swap test.

### F06 · Estimate import  ☑ (2026-09-29; built 2026-09-25; the 80-row fixture and the owner's production pass on `rye-beach` 2026-09-29, see `docs/briefs/F06.md`)
`EstimateSource` protocol with one implementation, the platform's estimate template (D-32: sheets Estimates, Work areas, Estimate costs; xlsx or one csv per sheet); estimates, work areas (D-01) and cost lines by cost code (D-23) into the spine; versions with the D-01 baseline; dirty rows load and raise exceptions. No vendor parser and no PDF fallback.
**Accept**: the template fixture for EST6115758 loads 21 work areas and 60 cost lines with kept prices 475,129.68, cost 315,832.69 and EAC in the D-04 basis 286,634.20; the EST6120638 fixture loads 32 work areas, seven omitted, kept 366,889.80; the 80-row Estimates workbook loads 79 estimates with the closing reports' per-status totals to the cent and the id-less Mijal row raises `EST_NO_ID`; 0.00 rows raise `EST_ZERO_SOLD` only when Sold; the same file again changes nothing.

### F06.1 · Burden on estimates  ☑ (2026-09-29; built and owner's pass on jobcost.dev 2026-09-29, see `docs/briefs/F06.1.md`)
A patch to F06 (nothing moved). Labor burden on estimates as D-05 and D-34 define it: per kept, priced work area and division, slot-10 lines × the rate in force on the estimate date, quantized half up; computed on read and never written; the detail shows cost as estimated and with burden side by side and EAC in the WIP basis burdened; `EST_BURDEN_LINE`, `EST_NO_BURDEN_RATE`, `EST_NO_BURDEN_DATE`. Configuration lists hide inactive rows behind "Show inactive (n)"; Imports opens on "Choose a source".
**Accept**: 67 Elm Street on EX 0.1959: burden 6,383.50, as-estimated total 315,832.69, with burden 322,216.19, EAC in the WIP basis 293,017.70 beside the as-estimated 286,634.20; Turley 0.00; the all-LS copy 6,960.29 and 293,594.49; a date before any rate leaves EAC not computed; a slot-20 line is shown as estimated and excluded from EAC; nothing is written on read.

### F07 · Job spine & crosswalk  ◐ (built 2026-09-29; the owner's pass on jobcost.dev is job by job, restated 2026-10-01; 67 Elm Street passed 2026-10-01)
`job`, `job_alias`, `job_estimate` with roles (migration 0011); the review screen for sold estimates (new job or attach with a role); the work-area kind confirmed by a person (D-01) and the revised contract, unapproved change orders and EAC computed on read; linking a job to QuickBooks rows by id from labelled suggestions (estimate id in name, then customer name and address; exact token rules) or a search; review items `EST_UNATTACHED`, `JOB_NO_LEDGER_LINK`, `LEDGER_PROJECT_NO_JOB`, `JOB_SECOND_ESTIMATE_FOR_CUSTOMER`, `JOB_DIVISION_UNSET`; a read-only customer duplicates list. Nothing written to QuickBooks. Brief: `docs/briefs/F07.md`.
**Accept**: every link and unlink is in the audit log; no automatic attachment without confirmation; the Turley (two jobs) and DeVellis (attach as change order) paths per D-03 are proven in tests and exercised on production when the owner reaches those jobs. Owner's pass, job by job (2026-10-01): 67 Elm Street end to end with revised contract 465,469.59, done; the other sold estimates are reviewed as the owner reaches them and are not a gate; P0-1 gates only the links. F07 closes on the pass that closes F07.1; the owner ticks it. Detail in the brief.

### F07.1 · Job screens patch  ◐ (built 2026-10-01; the owner's pass on jobcost.dev is open, see `docs/briefs/F07.1.md`)
A patch to F07 from the owner's pass with 67 Elm Street (nothing moved, no migration): the review queue opens on the estimate the person came from and has an "Estimate" select; "Confirm all as suggested" confirms every unconfirmed kept work area at its suggestion in one press, one audit row per work area; the Kind column says suggested or confirmed and the Action column says what a click does, as separate controls; Sold on can be corrected (`PATCH sold_on`, in the `job_updated` row; "(set when created)" is known from the audit log). The second 67 Elm Street fixture (29 work areas, as production stood on 2026-10-01). D-02 and D-36 appended.
**Accept**: both 67 Elm Street files loaded in order: 29 work areas, 28 kept, kept price 519,173.72, 104 cost lines 343,693.43, burden 11,742.47, EAC 327,929.93; the job reads 0.00 and "28 work areas to confirm" before, and 465,469.59 with 53,704.13 unapproved after one press, exactly 28 `work_area_kind_confirmed` rows, a second press writes nothing; hand-confirmed rows untouched; `client_pm` and `client_viewer` 403; labels and the Action column as the brief words them; the queue opens on the chosen estimate; a future Sold on is a 422 in one sentence. Owner's pass: a second job through the select and "Confirm all as suggested"; 67 Elm Street's rows reading "…, confirmed" and its Sold on corrected.

### F07.2 · Pick what to work on (tracked customers and projects)  ◐ (built 2026-10-04; the owner's pass on jobcost.dev, tenant `rye-beach`, is open, see `docs/briefs/F07.2.md`)
A patch to F07 (nothing moved; migration 0012; D-37): a `customer` row is **tracked** when a person picks it on the Customers page by its id, or when it is linked to a job (the link tracks it in the same action); untrack is refused while linked; one audit row per track and untrack. The Customers page is the picker (a paged search over active rows by name, projects first; a Tracked list with each row's job or the sentence that it needs one); the duplicates list is behind a link, unchanged. `LEDGER_PROJECT_NO_JOB` is raised for a tracked, active row with no job and for no other row; an inactive tracked row raises nothing. Rows linked before 0012 are tracked by the migration as of the link. What the sync fetches and holds, the month totals and every figure are unchanged.
**Accept**: as in `docs/briefs/F07.2.md`: nothing tracked raises nothing (1701 Ocean Boulevard with its billing row included); tracking writes one row and raises the item until the link clears it; the link tracks in one audit row and untrack is a 409 while linked; search is active rows only, projects first, 50 a page, empty text returns nothing; a change poll leaves the flag; the 67 Elm Street figures (EAC 327,929.93) and the month totals are identical before and after; isolation, roles and the migration round trip with the data step proven. Owner's pass on `rye-beach`: Jobs quiet on arrival; find the 67 Elm Street project by its number, track it, link it, and see only that project under Tracked.

### F07.3 · Home says what to do next (set-up checklist and job path)  ◐ (built 2026-10-04; the owner's pass on jobcost.dev, tenant `rye-beach`, is open, see `docs/briefs/F07.3.md`)
A patch under F07 (nothing moved; no table, no migration, nothing stored). `GET /api/home` composes the functions the linked pages already call: the six-line set-up checklist (QuickBooks, suggestion rules, chart of accounts, account mapping, policy, burden rates; done or not, one sentence, one link; at most one primary action), "n sold estimates to review" or "Upload an estimate", one line per open job with its status and the first thing it needs (`JOB_NEEDS`, a pure ordered list F08 onward append to), and the tracked QuickBooks rows with no job (D-37). Owner's answers: policy needs the keys read today (`timezone`, `wip_basis`); burden needs every active division with a cost-code digit rated on the company's today, not done while the basis is undecided, done with a note when Labor Burden is not in the basis. Client roles get the jobs part with links only to pages they can open.
**Accept**: as in `docs/briefs/F07.3.md`: a fresh tenant reads six not-done lines and "Upload an estimate"; the Rye Beach rules and chart turn lines 2 and 3 done and line 4 counts as the Accounts page does, done after "Confirm all suggestions"; chart before rules, then rules and suggestions, change both lines with no new upload; policy and burden follow the owner's answers (EX with its 2026-01-01 rate is not named); "16 sold estimates to review"; the EST6115758 job reads "28 work areas to confirm" until "Confirm all as suggested"; D-35 and D-37 as worded; every count equals the linked page's; roles and isolation. Owner's pass on `rye-beach`: Home names the next step at each point from estimate upload to linked project.

### F07.4 · Change order approval  ☑ (built 2026-10-06; migration 0013; D-42, D-43, D-44; owner's pass on jobcost.dev, tenant `rye-beach`, passed 2026-10-06; see `docs/briefs/F07.4.md`)
A patch under F07 that changes accounting behaviour because D-42 says so: a change-order work area joins the revised contract when a `client_pm` or `firm_admin` records that the customer agreed (the date, optionally who, a reference, a note; the evidence required is the new policy key `change_order_evidence`, no default); a withdrawal with a reason takes it out again; both are rows of the new append-only table `change_order_approval`, one audit row each (`change_order_approved`, `change_order_approval_withdrawn`). An approval is for the work area at its price that day and ends for good when a later version changes its name or price (rule C; `CO_APPROVAL_NOT_CARRIED`). Revised contract = original contract + approved change orders; EAC unchanged (D-44). The unapproved change orders list (`GET /api/change-orders/unapproved`, a page from Jobs with the count) shows every kept, confirmed, unapproved change order on an open job with its amount and age; Home gains the two needs. Retainage is owed as a later feature with its own decision (D-43).
**Accept**: as in `docs/briefs/F07.4.md`: 67 Elm Street 465,469.59 / 0.00 / 465,469.59 / 53,704.13 / EAC 327,929.93 before any approval; approving #18 (5,475.00) as `client_pm` gives 470,944.59 and 48,229.13 with one audit row naming the work area, the user and the price; withdrawing restores them; every change order approved gives 519,173.72 (EST6120638: 301,553.22 → 366,889.80); a second version pricing #18 at 6,000.00 ends the approval with one sentence naming 5,475.00 and 6,000.00, a restored version 3 does not revive it; the refusals; roles; isolation; the list's 12 + 9 rows; F08's remaining to bill follows; nothing but history stored. Owner's pass on `rye-beach`: set the evidence key; a project manager approves one change order on 6115758 under their own login and the figures move by exactly its price.

### F08 · Sold Jobs Board & Job Detail (billing side)  ☑ (built 2026-10-06; owner's pass on jobcost.dev, tenant `rye-beach`, passed 2026-10-06; the §13.6 spreadsheet criterion is not built, stood in for by the owner's check of 6115758 against a QuickBooks export; see `docs/briefs/F08.md`)
Reports 1 and 2 from §9, billing half only. Deposit identification per tenant policy (D-02: the `_DEP` number and the deposit item, both). Fuel surcharge lines outside billed to date (D-39). XLSX/PDF export (D-40).
**Accept**: **reproduces the hand-built deposit spreadsheet from §13.6 exactly** (not built in the first pass: the spreadsheet is not in the repo; the check figures for the tracked jobs come from a QuickBooks export on the owner's pass). Billed and collected totals across jobs + unassigned tie to QBO income and A/R for the period: built as the month-by-month tie-out on the Intuit sandbox fixture (jobs + not on a job = the Connections month totals, to the cent).

### F08.2 · Board tie-out on the collected side, speed, and three screen fixes  ☑ (patch under F08, from the owner's pass of 2026-10-06; built 2026-10-06 in two commits, the second on D-41; owner's pass on jobcost.dev, tenant `rye-beach`, passed 2026-10-06; see `docs/briefs/F08.2.md`)
A patch, no table, no migration, no dependency. Item 1: a read-only diagnostic (`scripts/collected_tieout.py`) found the cause of the 18 months whose collected side did not tie (a journal-entry credit applied through a payment, counted as cash); D-41 settled it: collected to date is cash, the net of a payment's other lines is its remainder on the payment's own row, a credit remainder is "Other credits applied" and raises `PAYMENT_OTHER_CREDIT`. Item 2: the board reads the jobs' own rows, the not-on-a-job row and the month sums come from the database, the tie-out is its own request, a job page reads one job. Item 3: the item pick-list shows active items with a search box and a "Show inactive items" switch. Item 4: Track, Untrack, Link and Unlink say what they are doing. Item 5: a QuickBooks row is suggested when its name contains the estimate number anywhere, within the F07 scope.
**Accept**: the diagnostic names the cause; after the fix every month ties on billed and collected; statement counts equal at 10 and 1,000 documents on untracked rows; the tie-out served by its own request and the XLSX tab balances; the pick-list, busy labels and suggestion as stated; owner's pass on `rye-beach`.

### F08.1 · Pay applications (billing requests)  ☑ (built 2026-10-07 in two commits: Part 1, earlier invoice lines assigned to work areas and `BILLING_UNAPPROVED_CO`, migration 0014, D-45, the owner's check passed the same day; Part 2, the billing request and the pay application, migration 0015, the owner's answers A, B, C; the owner's pass on jobcost.dev, tenant `rye-beach`, 6115758, passed 2026-10-08 with F08.3's; see `docs/briefs/F08.1.md`)
Restated per D-36 (2026-10-01; position unchanged). The schedule of values includes approved change orders (D-42); no retainage (D-43). Per D-39: the fuel surcharge choice on each application, the printed line and the two-part tie. Capture the operations billing request per job (cumulative percent complete per work area, D-26) and produce the **pay application** with its **schedule of values**, on screen and as PDF: every kept original and approved change-order work area (D-01) with scheduled value, percent complete to date, earned to date (quantized half up per work area), earned on previous applications, earned this application, balance to finish; the summary: total earned to date, less billed to date before this application (the deposit, D-02, and earlier applications), amount due this application, never below 0.00, or by how much the job is billed ahead; no retainage (D-43). Issue, void and re-issue of an application, audited. Earlier invoice lines assigned to work areas and `BILLING_UNAPPROVED_CO` (D-45): billed to date per work area from the pay applications, the "#n" lines and a person's assignments, and a flag, never an adjustment, when change orders no one has approved have been billed. The invoice is keyed in QuickBooks from the application (`<estimate number>_PMT<n>`, the amount due, "Pay application n") and tied to it by document number: `PAYAPP_NOT_INVOICED`, `INVOICE_NO_PAYAPP`, `PAYAPP_INVOICE_MISMATCH` beside D-26's exceptions (over 100%, a work area priced 0.00, an omitted work area, a percent lower than the previous application's). No write to QuickBooks; tables `pay_application`, `pay_application_line` (BLUEPRINT §5, §7 when built). **Accept**: 67 Elm Street's application for 2026-08-21 reads earned to date 166,294.48 (work areas 1 to 4 at 100%), less deposit 149,800.00, due 16,494.48; the 2026-09-17 instruction for Turley EST6120638 lists work areas 3, 5, 9, 25, 31, 32 and raises an exception for 26 (priced 0.00); re-issuing the same request earns nothing more; the nine ledge removal lines on 6115758 assigned to #18 and #22 to #29 raise one sentence naming 49,275.00 with every board figure unchanged (Part 1, built).

### F08.3 · Pay application patch from the owner's pass  ☑ (patch under F08.1, from the owner's pass of 2026-10-07 on jobcost.dev, tenant `rye-beach`, 6115758; built 2026-10-07 in one commit; the owner's pass on jobcost.dev, tenant `rye-beach`, 6115758, passed 2026-10-08; see `docs/briefs/F08.3.md`)
The PDF fits the page (landscape Letter, fixed column widths, measured by a test; "Billed ahead by" with its amount in the amount column, D-40); a request never yields a negative amount (D-26: `BILLING_NEGATIVE` compares earned amounts whatever the source of the previous figure; the work area stays at its previous percent with 0.00 earned this application and the sentence beside it, the owner's answer A; the check runs on read too); the form starts from what has been billed (the prefill from the tied invoice lines, half up to two places; on a job's first application earned on previous applications is the scheduled value at that percent, question 2 option 3, delegated to the design partner); a draft is reopened with its values and can be discarded (its row and lines removed, one audit row, the number reused, answer B); Enter never submits the request or void form; the request form reads top to bottom (D-22). No table, no migration, no dependency. **Accept**: the brief's criteria on the 67 Elm Street fixture with `EST6115758_PMT2` and the two ledge invoices (billed ahead by 49,275.00 as prefilled; `BILLING_NEGATIVE` on #2 at 50.00 naming 10,830.68; the owner's case #2 to #4); the owner's pass on 6115758 (draft 5 reopened, #1 to #5 prefilled at 100.00, the three requests of 2026-10-07 to the cent, the PDF printed whole).

### F09 · Exceptions queue v1  ◐ (built 2026-10-08 in one commit, migration 0016, D-46; the owner's answers A to D of 2026-10-08; owner's pass on jobcost.dev, tenant `rye-beach`, open; see `docs/briefs/F09.md`)
Exception model (`exception`, `exception_event` append-only), the existing generators for the estimate, job, link and billing types in §10 persisted by one worker run per burst of changes (after a QuickBooks poll that applied a change, an import that loaded estimates, or a person's state-changing request), one computation per subject for the Jobs board, the job page, Home, the Estimates pages and the queue, assignment, notes, dismissal with a note (never a block-close exception; a dismissed one opens again when what it states changes), reopen, the Exceptions page, the count on the company picker (the console itself is F19). The four D-26 billing-request sentences and `CUSTOMER_FUZZY` are not queued (the owner, 2026-10-08).
**Accept**: resolving an exception's underlying cause clears it on next run; dismissing requires a note. As built: on the 67 Elm Street fixture one run holds `BILLING_UNAPPROVED_CO` at 49,275.00 on nine and nine `EST_UNIT_PRICED`, a second run writes nothing, approving the nine resolves the flag and withdrawing one raises the same exception at 5,475.00; every figure identical before and after; the run on 1,000 sold estimates and 200 jobs in 1.55 s.

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

### F13.1 · Pool allocation  ☐
Month-end allocation of each pool job (D-30) by its per-pool driver from `tenant_policy`; formatted reclass entry (DR 5n35 by job / CR 5n35 pool job) for the controller to post; pool-balance tie-out (§8.5 check 7); allocated vs synced lines agree to the cent.
**Accept**: on the Rye Beach fixture, a pooled bill allocated across eligible jobs sums to the bill to the cent, and the pool job shows 0.00 afterwards.

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
- Owned-equipment and fuel allocation by equipment hours (D-04 revisit); reuses F13.1's allocation machinery (D-30).
- Retainage: owed before any tenant that holds it; needs its decision first (D-43).
- Retainage UI and pay-application (AIA-style) support for GC clients.
- Pipeline aging; cash forecast from backlog; bonding-format WIP.
- Second ledger adapter (QuickBooks Desktop/Enterprise is the likely first ask in construction).
- Billing/subscription management for the practice.
