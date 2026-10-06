# current-feature.md

_No feature in flight (2026-10-06)._ **F07.4 · Change order approval is built** (2026-10-06,
one commit; migration 0013 `change_order_approval`; D-42 and D-43 pasted by the owner and
D-44 made by the brief are in `docs/DECISIONS.md`; ROADMAP ◐ until the owner's pass on
jobcost.dev, tenant `rye-beach`). The brief, with the Plan, the owner's answers A to D, the
build notes and the Discovered list, is `docs/briefs/F07.4.md`. A change-order work area
joins the revised contract when a `client_pm` or `firm_admin` records that the customer
agreed (the date, optionally who, a reference when the new policy key
`change_order_evidence` requires one, a note), at its price that day; a withdrawal with a
reason takes it out; both are append-only history with one audit row each; an approval
ends for good when a later version changes the work area's name or price (rule C;
`CO_APPROVAL_NOT_CARRIED`). Revised contract = original contract + approved change orders;
EAC unchanged (D-44). The unapproved change orders list is a page from Jobs. Dev `wip` is
at 0012: deploying F07.4 runs 0013.

Open passes carried: **F07.4** (set the evidence key; a project manager's login,
`scripts/create_user.py create-user … --membership rye-beach:client_pm`; one approval on
6115758 under that login: approving #18 moves the revised contract from 465,469.59 to
470,944.59 and unapproved from 53,704.13 to 48,229.13; withdrawing restores them; the list
shows what is left), **F07.3** (`docs/briefs/F07.3.md`), **F07.2** (`docs/briefs/F07.2.md`;
deploying it runs migration 0012), **F07.1** (`docs/briefs/F07.1.md`) and **F07** (the pass
job by job; 67 Elm Street passed 2026-10-01). D-08 is still open. Retainage is owed as a
later feature with its own decision before any tenant that holds it (D-43; a Later line in
ROADMAP).

Next per ROADMAP: **F08.1 · Pay applications (billing requests)**, as D-36 restates it, D-39
extends it and D-42 and D-43 qualify it: capture the operations billing request per job
(cumulative percent complete per work area, D-26), produce the pay application with its
schedule of values (every kept original and **approved** change-order work area, D-42) on
screen and as PDF (reportlab, D-40), the summary (earned to date, less billed to date
before this application: the deposit, D-02, and earlier applications; amount due; **no
retainage line**, D-43), the fuel surcharge choice on each application with the printed
line "Fuel surcharge (5.00%)" and the two-part tie of the invoice to its application
(D-39), issue, void and re-issue audited, tables `pay_application` and
`pay_application_line` (tenant-scoped, RLS forced; a migration: wait for a yes). Not
started; the owner supplies the brief. Copy it here, expand it, and restate the acceptance
criteria before coding. It reads the rate from `fuel_surcharge_treatment`
(`policy.surcharge_treatment`) and requires it.

Standing state from F07 to F08.2 and F07.4: the job is the reporting unit; matching is by
alias (`job_alias`), never by name (a QuickBooks row is suggested when its name begins with
or contains an attached estimate's number, within the F07 scope). The original contract,
approved change orders, revised contract, unapproved change orders, EAC and every billing
figure are computed on read (`app/domain/jobs/contract.py`, `app/domain/billing/figures.py`
and `board.py`), never stored; the only F07.4 rows are approvals and withdrawals
(`change_order_approval`, keyed by `(estimate_id, order_no)`, the name and price as the
read-time guard; `contract.approval_state` decides whether one applies, rule C); nothing in
`app/wip/` has been touched. Billed to date is total − sales tax − fuel surcharge lines, the
deposit invoice (`<estimate number>_DEP` on a deposit item, both marks) counted from its
date. Collected to date is cash (D-41): a payment's total less its unapplied amount;
invoice, sales receipt and credit memo lines follow the document's job, dated by the
payment; the net of every other line is the payment's remainder on the payment's own row;
a credit remainder is "Other credits applied". The board reads the listed jobs' own rows;
the "Not on a job" row (D-35, D-37), every per-month sum and the Connections month totals
are summed by the database; the tie-out is `GET /api/jobs/tie-out`, over every job, and
holds to the cent; the sign of a payment line is `CREDIT_TXN_TYPES` beside
`INVOICE_TXN_TYPES` in `figures.py`, one place. Review items are pure generators
(`app/domain/jobs/issues.py`: the F07 ones, `PAYMENT_UNAPPLIED`, `DEPOSIT_NOT_IDENTIFIED`,
`BILLED_OVER_CONTRACT`, `PAYMENT_OTHER_CREDIT`, `CO_APPROVAL_NOT_CARRIED`); F09 persists
them. Home composes the pages' functions; `JOB_NEEDS` has the three F08 rules after the F07
ones and the two F07.4 rules after those (an ended approval, then unapproved change orders;
not `PAYMENT_OTHER_CREDIT`, Discovered); `REQUIRED_POLICY_KEYS` holds the five keys read
today. Policy: no key has a default; the two item keys take the company's QuickBooks items
from the raw `Item` versions (`app/domain/config/items.py`); `change_order_evidence` is a
`choice` with its two values' words on the registry entry; `small_job_threshold` still
waits (D-08). Roles: `client_pm` and `firm_admin` approve and withdraw
(`can_approve_change_orders`); `can_manage_jobs` is unchanged. XLSX money cells are written
as numeric strings holding the Decimal's digits (`export.py`, `_write_money`); PDFs are
reportlab, uncompressed. The owner's reviewed 67 Elm Street workbook
(`estimate_upload_EST6115758_reviewed_10.06.xlsx`; original contract 465,469.59 and twelve
change orders 53,704.13 after "Confirm all as suggested") is the job the owner is working;
later features are accepted against it.

## Discovered
Carried from `docs/briefs/F07.4.md` (copied unchanged 2026-10-06; nothing is dropped):

Carried from the live `current-feature.md` stub of 2026-10-06 (copied unchanged 2026-10-06 from `git show HEAD:current-feature.md`; nothing is dropped):

Carried from `docs/briefs/F08.2.md` (copied unchanged 2026-10-06; nothing is dropped):

Carried from the live `current-feature.md` stub of 2026-10-06 (copied unchanged 2026-10-06 from `git show HEAD:current-feature.md`; nothing is dropped). Closed by F08: the estimate number on the Jobs row (owner's pass, 2026-10-06).

Carried from `docs/briefs/F08.md` (copied unchanged 2026-10-06; nothing is dropped):

Carried from the live `current-feature.md` stub of 2026-10-05 (copied unchanged 2026-10-06 from `git show HEAD:current-feature.md`; nothing is dropped):

From the owner's pass of 2026-10-06 (not fixed here):
- Jobs list: a job is shown by its name only; the estimate number (for example 6120638) is not on the row, so a job whose name does not describe it cannot be identified without opening it. Show the estimate number of the job's original estimate beside the name.

Carried from the live `current-feature.md` stub of 2026-10-04 (copied unchanged 2026-10-05; nothing is dropped):

From the owner's session of 2026-10-04, after F07.2 (recorded in the F07.3 brief):
- Configuration, Accounts: with no suggestion rules loaded the page gives no reason for "0 suggested"; one sentence would do.
- Two pytest sessions against `wip_test` at once destroy each other without warning (a second run's teardown drops the first run's tables); a lock or a per-run database.
- Each client's suggestion-rules file needs a home outside `tests/fixtures/` (a client pack); Rye Beach's is the only one today.
- D-38 (an inactive tracked row raises nothing) was drafted after the F07.2 commit; confirm it is in `docs/DECISIONS.md`. **Confirmed 2026-10-04: in `docs/DECISIONS.md` and BLUEPRINT §14 (commit a45c6e5).**

From the owner's session of 2026-10-04 (recorded in the F07.2 brief):
- Connections, no chart loaded: "Numbered in QuickBooks but not in the chart" lists every numbered account; it should say that no chart is loaded and where to load one.
- Connections: "Accounts without a number" gives a count only; a list of the names would save a trip to QuickBooks.
- Chart of accounts for a connected tenant: build it from the synced QuickBooks accounts, accept the QuickBooks Account List export as it comes, or offer a template on Imports. The owner decides; a separate F04 patch.
- OPERATIONS.md has no runbook for resetting production while pre-launch (drop and recreate `wip`, redeploy, bootstrap, tenants); the local reset steps stop before `create-tenant` and `add-entry`.

F07.1 (2026-10-01):
- The job screens were not rendered in a browser by Claude Code (the owner's pass checks 390 px; `.actions` wraps on a narrow row).
- A kept work area never lacks a kind suggestion (`suggested_kind` is total over F06's boolean flag); the "no suggestion" branch of "Confirm all as suggested" is proven on the pure selector only. If the suggestion rule ever yields "none", add an API test with a workbook.
- `_sold_on_set_by_person` reads `audit_log` by `entity_id` with no index on it (the index leads with `tenant_id, occurred_at`); fine at a tenant's size today; F09 adds one if it ever matters.
- `JobReview` jumps once per mount (`started` ref); the Jobs page's own "Review sold estimates" button always opens at the start.

F07 (2026-09-29):
- Fixed 2026-09-29 (owner's request before the push): `tests/test_rls.py::test_f04_tables_read_zero_rows_of_another_tenant` no longer seeds random keys; `_seed_f04_rows` writes one fixed set per tenant (`F04_PROBE`, slot `Q1` outside the D-23 slots) and a second call adds nothing. The suite ran three times clean (745 passed each).
- The job screens were not rendered in a browser by Claude Code (owner pass).
- For a later F06 patch (owner's answer 12): a newer upload with a blank `client`, `jobsite` or `estimator` cell clears the stored field (`_header_values`), so the order files are loaded in decides what an estimate's header says (the Turley template has none of the three; loaded after the 80-row sheet, EST6120638 loses its client and jobsite text). Should a blank cell keep the stored value?

F06.1 (2026-09-29):
- The Estimates list's Attention column and the detail can differ by the burden sentences
  (the list endpoint was left unchanged); F09, persisting issues per estimate, should
  compute both sets in one place.
- The estimate detail was not rendered in a browser at 390 px by Claude Code; the new
  columns use the existing `.table-wrap` pattern (the owner pass checks it).
- Thirteen sold estimates on `rye-beach` have no cost lines and sit off the WIP schedule by
  D-04; the owner supplies their cost lines one at a time through the template (D-32). Not
  a platform change.

F06 (2026-09-25):
- For F09: the generators in `app/domain/estimates/exceptions.py` return `Issue(code,
  message, detail)`; `EST_NO_ID` and the rows-not-loaded sentences are on the batch, not
  per estimate; F09 reads both. `EST_BURDEN_LINE`, `EST_NO_BURDEN_RATE` and
  `EST_NO_BURDEN_DATE` (F06.1) join the per-estimate set.
- `followup_message` composes "Loaded. Nothing changed: this file was already loaded.":
  two sentences where one would do; a per-outcome sentence table in `batch_message` would
  read better (D-22).
- A work-area row rejected at parse takes its cost lines with it (each with its own
  sentence): five sentences for one bad cell. One combined sentence per work area would be
  quieter.
- `Worker` registers task kinds only through `load_task_modules()`; a test module that
  runs the worker before any module imported the task's module gets a retrying
  `LookupError`. Making `run_until_quiet` call `load_task_modules()` would remove the trap.

F05.1 (2026-09-22 to 2026-09-25; details in `docs/briefs/F05.1.md`, Discovered):
- For F08 / D-02, from the Rye Beach tie-out (owner, 2026-09-25; none is a tie-out difference): 34 non-voided invoices at 0.00 (29 dated 2025-12-01); 13 zero-amount payments "Created by QB Online to link credits to charges"; sales receipt INV-2984 (2026-01-22, "Cleanup – Unidentified Deposits", deposit account deleted, income 4100), evidence for D-02; payments deposit to five accounts, two deleted; July 2025 credit memos of 257,994.17, one month before the tie-out window; credit memo INV-2464 (2025-12-10).
- 12 active Cost of Goods Sold and 11 active Income accounts in QuickBooks carry no number (ids in OPERATIONS.md connection record): postings to them are outside `account_map` and the §8.5 tie-outs until numbered or inactive (F10, F13). An unnumbered Customer Deposits liability (QuickBooks id 1150040036) exists: D-02 / P0-3 evidence (F08). Bank account 219 beside 1010 may be a duplicate.
- The API service logs only uvicorn's access lines; the `app.*` INFO lines never reach journald because only the worker calls `basicConfig` (F23 or a patch).
- A webhook that arrives while a poll is running enqueues nothing; the change lands on the next scheduled poll. A "one more after this" follow-up would shorten that to seconds.
- `Merge` is a webhook operation on Customer, Account, Item, Vendor, Class, Department, Employee and PaymentMethod; what CDC returns after a customer merge needs checking before `job_alias` links are trusted across a merge (put back on the carried list by the owner, F07 answer 19: a sandbox merge by the owner and one poll).
- CI #33 (2026-09-25): the NOTIFY wake-up test raced the worker's `LISTEN` registration; fixed (listener opened before the first pass, `Worker.listening` readiness event, test waits on it). Two other intermittents remain open, below.
- One unreproduced setup ERROR in `test_migrations.py::test_0003_data_step…` (F05.1); not seen in the three clean runs of 2026-09-29.

Carried from the stubs of 2026-09-21 and 2026-09-22 (F05, F04, F03, F02.1), still open:
- F05 (2026-09-21): The nightly drift check's `drift` outcome and the "fresh backfill needed" state are proven in tests only. A mapping from QuickBooks `AccountType` to `gl_account.ledger_type` is not built (owner answer 10).
- F04: no screen for suggestion rules (script / `PUT /api/config/suggest-rules`); a general "no money as a JSON number" response assertion is still per test; **for the owner**: the real Rye Beach chart has one account the fixture does not (2630); the owner supplies an updated fixture and oracle if it should be added.
- F03: money in API responses must be strings (F08; `formatMoney` is ready); reports (F08+) use `.table-wrap` with the first column held.
- F02.1: pending TOTP secret is per user, not per session (deferred; rule recorded there); no rendered-browser test dependency for now; a client user's optional enrol confirm records a second `login_success`.

From this brief, for F07: EST6281138 (Sanford, pending, 998.71) and EST6346291 (Hess, sold, 39,032.44) are two estimates for the same project under two client names; the Turley pair EST6366990 and EST6120638; the Mijal row once it has an id; "Mighty Roots - Makarov | Site Work" has no LMN id in its QuickBooks project name (§13.2).

New, from the owner's session of 2026-10-05, for later features and not fixed here:
- F08: confirm that synced invoice and credit-memo lines keep the QuickBooks item id. BLUEPRINT §13.2 records line-level customer references on bills and expenses only. The deposit item (D-02) and the fuel surcharge treatment both depend on it.
- F08: are QuickBooks items (products and services) held in a form a picker can list, or only inside raw payloads?
- A fuel surcharge decision is drafted and waits on two owner answers; it arrives with the F08 brief. Until then `fuel_surcharge_treatment` stays undecided on every tenant.
- D-08 (small job threshold) is still open; the key waits on it.
- Home counts `fiscal_year_start_month` among the keys that wait for their features ("4 more keys are decided when their features arrive") although it can be set today; the sentence is out of scope here (owner, 2026-10-05: add, do not fix).

Found while building F08 (2026-10-06), not fixed here:
- openpyxl writes numbers with `%.16g`, so a Decimal with two places can land in a sheet as 72570.85000000001; `export.py` writes money cells as numeric strings holding the digits. The second report should move that writer to a shared `app/reports/` module rather than copy it.
- `load_board` reads every billing, line, payment and application row of the tenant on each Jobs, Home and job-page request; fine at a tenant's size today; a per-job read or a short cache if it ever matters.
- The board's Attention column and Home compute the three billing items in two reads; F09, persisting issues, computes them once.
- The Jobs page was not rendered in a browser by Claude Code (the owner's pass checks the 19-column board at 390 px and the two export links).
- `tests/test_frontend_effects.py` reads an apostrophe in JSX text ("job's") as an unterminated string and fails with "unbalanced source"; later screens avoid apostrophes in JSX text or the scanner learns JSX text.
- The policy picker lists items from the latest raw `Item` versions: on a tenant with no backfill it says "No QuickBooks items are held yet"; the Home policy line does not say that the items must be created and polled first (OPERATIONS does).

New, from the owner's sessions of 2026-10-05 and 2026-10-06, not fixed here:
- Burden rates: the "To (exclusive)" date led the owner to enter 2026-12-31 for a rate meant to cover the whole year, leaving 2026-12-31 uncovered. A hint under the field, or showing the last covered day, would prevent it.
- OPERATIONS has the rule for a shared purchase (the pool, D-30) and none for a purchase that belongs to no job at all. The owner's practice (2026-10-05): a tool or piece of equipment the crew keeps goes to overhead (6430 under 2,500.00, else 1510) with no project; a consumable used across jobs goes to the pool; a purchase for one job goes to the job. A coding note for OPERATIONS, not a decision (D-04 covers the reasoning).
- lmn-convert (outside the repo) produces one placeholder cost line per work area on code n90 when it has no item detail; the owner rebuilt EST6120638's 74 cost lines by hand from the estimating system's item screens. What the converter would need to read is for the owner and the design partner to specify.
- EAC includes the cost of unapproved change-order work areas while the revised contract excludes their price (F07, until the approval feature). On 6115758 that is 30,369.40 of EAC against 0.00 of contract. As specified; the change-order approval decision is the fix.
- The Rye Beach chart gained 1350, 2410, 4190 and 4290 on 2026-10-06 (P0-3); the chart fixture and its oracle do not have them (the same kind of gap as 2630 on the carried list).
- Retainage: D-39 takes the surcharge on the application's amount due; revisit when the retainage decision is made.

New, from the owner's pass of 2026-10-06, not fixed here:
- A job's "Sold on" is set to the day the job was created; for a job sold months earlier it is wrong until a person edits it. Whether the estimate carries a sold date the job could take is for a later brief.
- Syncing less: the owner asked whether only tracked projects, or only data from a start date, should be brought in. Not done: D-37 keeps money on untracked rows in "Not on a job" so the tie-out proves nothing was dropped, and a start date would leave open A/R needing an opening balance. A start date is the owner's decision to make if this patch does not make the pages fast enough.
- "Not on a job" on `rye-beach` shows 25.00 of unapplied payments; the owner looks for it in QuickBooks.
- 6115758: the nine "CO: Ledge Removal per Day" work areas raise `EST_UNIT_PRICED` and, on the owner's facts, are time-and-materials work (D-24). They were billed on two invoices of their own (10,950.00 and 38,325.00). The split waits on the owner's talk with the project manager; until then the job's billed to date includes them.
- There is no way to mark an `EST_UNIT_PRICED` sentence as resolved; dismissal with a note arrives with the exceptions queue (F09).

Found while building F08.2 (2026-10-06), not fixed here:
- Home's `JOB_NEEDS` does not list `PAYMENT_OTHER_CREDIT` (D-41 names a review sentence; the board's Attention column and the job page carry it); F09 or a Home patch decides whether Home should.
- The PDF export's time at 10,000 documents (376 ms on the Mac) is reportlab laying out the 308-row tie-out table, not a read; a later report feature may paginate or shorten that tab.
- The SQL month sums leave out a month whose collected sum is 0.00, while the pure per-job figures may keep such a key from zero-amount lines; harmless to the tie-out (the ledger side lists the month) and compared on non-zero months in the test; F13's tie-outs should settle on one rule.
- The F08.2 screens (the pick-list's search box and switch, the busy labels, the tie-out line, the "Other credits applied" column) were not rendered in a browser by Claude Code; the owner's pass checks them at 390 px as well.
- `test_collected_tieout.py` imports the two scripts by path like `test_accounts_without_number.py`; a `scripts` package with tests importing it as one would be tidier.

New, from the owner's sessions of 2026-10-06, not fixed here:
- A job has no project manager of its own: any `client_pm` on the tenant can approve any job's change orders. Assigning a project manager to a job, and reports by project manager, are for a later brief.
- An evidence document cannot be uploaded; the reference is text. Upload is for a later brief if a tenant requires it.
- Retainage is owed as a feature with its own decision (D-43); the fuel surcharge base (D-39) is revisited then.
- 6115758: nine of its eleven priced change orders are the ledge removal days that the owner expects to move to a time-and-materials job (D-24). Approving them here would put 49,275.00 into a fixed-price contract; the owner decides with the project manager before any is approved.
- Burden rates entered with "To" 2026-12-31 stop a day short of year end (carried from 2026-10-06).

Found while building F07.4 (2026-10-06), not fixed here:
- Home shows a job's unapproved change orders only once the F07 rules are quiet: a sold, unlinked job reads as backlog first (D-35) and its unapproved change orders appear on the list page and the job, not on Home. Whether backlog with unapproved change orders deserves its own Home sentence is for a later brief.
- `GET /api/jobs` computes the unapproved count with a second read of every job's views; fine at a tenant's size today; a later patch could count from the page's rows.
- The job page reads the policy list to know whether a reference is required; a field on the job detail would save that request.
- The F07.4 screens (the Approval column, the in-row form, the change-order estimates table, the history, the list page) were not rendered in a browser by Claude Code; the owner's pass checks them at 390 px as well.
- An estimate attached as a change order with no work areas loaded counts its header price as unapproved and has nothing to approve; loading its work areas (D-32) is the way to approve it.
