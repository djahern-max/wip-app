# current-feature.md

_No feature in flight (2026-10-06)._ **F08.2 · Board tie-out on the collected side, speed, and
three screen fixes is built** (2026-10-06, two commits: items 2 to 5 and the item 1 diagnostic,
then item 1 on **D-41**; no migration, no index, no dependency) and waits for the owner's pass
on jobcost.dev, tenant `rye-beach` (the tie-out line ties in every month; the Jobs page, a job
page and Connections open quickly enough to work with; the deposit pick-list shows active items
and finds "Deposit" by search; linking a row shows it is working; 67 Elm Street's figures
unchanged at 288,618.87, 250,293.87, 38,325.00 and 176,850.72). Its brief, with the Plan, the
owner's answers, the finding, the build notes, the speed before and after and the Discovered
list, is `docs/briefs/F08.2.md`. ROADMAP F08.2 is ◐. **D-41** (collected to date is cash; a
payment's remainder on its own row; "Other credits applied"; `PAYMENT_OTHER_CREDIT`) was decided
2026-10-06 on the diagnostic's finding and is in `docs/DECISIONS.md` and BLUEPRINT §14.

Open passes carried: **F08 · Sold Jobs Board** (built 2026-10-06, `docs/briefs/F08.md`; the
job figures passed to the cent on 6115758 on 2026-10-06; the pass closes with F08.2's; the
§13.6 spreadsheet criterion is open), **F07.3 · Home says what to do next** (built 2026-10-04,
`docs/briefs/F07.3.md`; ROADMAP ◐), **F07.2 · Pick what to work on** (`docs/briefs/F07.2.md`;
deploying it runs migration 0012), **F07.1** (`docs/briefs/F07.1.md`) and **F07** (the pass job
by job; 67 Elm Street passed 2026-10-01). The dev `wip` database is at 0012; F08 and F08.2 add
no migration. D-08 is still open.

Next per ROADMAP: **F08.1 · Pay applications (billing requests)**, as D-36 restates it and
D-39 extends it: capture the operations billing request per job (cumulative percent complete
per work area, D-26), produce the pay application with its schedule of values on screen and
as PDF (reportlab, D-40), the summary (earned to date, less retainage, less billed to date
before this application: the deposit, D-02, and earlier applications; amount due), the fuel
surcharge choice on each application with the printed line "Fuel surcharge (5.00%)" and the
two-part tie of the invoice to its application (D-39), issue, void and re-issue audited,
`job.retainage_pct` (a migration: wait for a yes), tables `pay_application` and
`pay_application_line` (tenant-scoped, RLS forced). Not started; the owner supplies the brief.
Copy it here, expand it, and restate the acceptance criteria before coding. It reads the rate
from `fuel_surcharge_treatment` (`policy.surcharge_treatment`) and requires it.

Standing state from F07 to F08.2: the job is the reporting unit; matching is by alias
(`job_alias`), never by name (a QuickBooks row is suggested when its name begins with or
contains an attached estimate's number, within the F07 scope). The revised contract,
unapproved change orders, EAC and every billing figure are computed on read
(`app/domain/jobs/contract.py`, `app/domain/billing/figures.py` and `board.py`), never
stored; nothing in `app/wip/` has been touched. Billed to date is total − sales tax − fuel
surcharge lines, the deposit invoice (`<estimate number>_DEP` on a deposit item, both marks)
counted from its date. Collected to date is cash (D-41): a payment's total less its unapplied
amount; invoice, sales receipt and credit memo lines follow the document's job, dated by the
payment; the net of every other line is the payment's remainder on the payment's own row; a
credit remainder is "Other credits applied". The board reads the listed jobs' own rows; the
"Not on a job" row (D-35, D-37), every per-month sum and the Connections month totals are
summed by the database; the tie-out is `GET /api/jobs/tie-out`, over every job, and holds to
the cent; the sign of a payment line is `CREDIT_TXN_TYPES` beside `INVOICE_TXN_TYPES` in
`figures.py`, one place. Review items are pure generators (`app/domain/jobs/issues.py`: the
F07 ones, `PAYMENT_UNAPPLIED`, `DEPOSIT_NOT_IDENTIFIED`, `BILLED_OVER_CONTRACT`,
`PAYMENT_OTHER_CREDIT`); F09 persists them. Home composes the pages' functions; `JOB_NEEDS`
has the three F08 rules after the F07 ones (not `PAYMENT_OTHER_CREDIT`, Discovered);
`REQUIRED_POLICY_KEYS` holds the four keys read today. Policy: no key has a default; the two
item keys take the company's QuickBooks items from the raw `Item` versions
(`app/domain/config/items.py`: active, inactive and deleted, each with its flag and its label
built on the server); `small_job_threshold` still waits (D-08). XLSX money cells are written
as numeric strings holding the Decimal's digits (`export.py`, `_write_money`); PDFs are
reportlab, uncompressed. `scripts/collected_tieout.py` (read-only, no customer name) and
`scripts/seed_board_load.py` (dev only) are in OPERATIONS, "The Sold Jobs Board". The owner's
reviewed 67 Elm Street workbook (`estimate_upload_EST6115758_reviewed_10.06.xlsx`; revised
contract 465,469.59 after "Confirm all as suggested") is the job the owner is working; later
features are accepted against it.

## Discovered
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
