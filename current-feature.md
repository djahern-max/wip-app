# current-feature.md

_No feature in flight (2026-10-05)._ **F04.1 · Policy screen a person can answer is closed**
(built 2026-10-05, ba0eda0; no migration; no table, key or default; the owner's pass on
jobcost.dev, tenant `rye-beach`, passed the same day: Eastern with no reference, the WIP basis
slots 10, 20, 30, 35, 40, 50, 60, 70, 90 with "D-04, D-05", the three waiting sentences, "Not
decided" on the left, Home's Policy line done). Its brief, with the Plan, the owner's answers
and the build notes, is `docs/briefs/F04.1.md`. ROADMAP F04.1 is ☑. On `rye-beach` the
`timezone` and `wip_basis` keys are now decided. **F07.3 · Home says what to do next** (built
2026-10-04, `docs/briefs/F07.3.md`) waits for its pass on `rye-beach`: Home names the next
step at each point while the owner takes 67 Elm Street from estimate upload to linked
project; ROADMAP F07.3 is ◐. **F07.2 · Pick what to work on** (`docs/briefs/F07.2.md`) waits
for its own pass on `rye-beach` (Jobs quiet on arrival; find 6115758 by its number, track,
link, see only it under Tracked); deploying it runs migration 0012, whose data step tracks
every row already linked as of the link. **F07.1** stays ◐ on its pass (`docs/briefs/F07.1.md`)
and **F07** on the pass job by job (67 Elm Street passed 2026-10-01; P0-1 gates only the
links). The dev `wip` database is at 0012.

**D-37** (a person picks the QuickBooks rows the platform works on) and **D-38** (an
inactive tracked row raises no review item) were decided 2026-10-04 and are in
`docs/DECISIONS.md` and BLUEPRINT §14. **F05.2**, **F06.1**, **F06** are closed. D-08 is still
open; a fuel surcharge decision is drafted and arrives with the F08 brief.

Next per ROADMAP: **F08 · Sold Jobs Board & Job Detail (billing side)**: reports 1 and 2
of §9, billing half only; the deposit identified by the `_DEP` document number and the
tenant's deposit item (D-02), counted in billed to date; the unapplied-payment review
item; XLSX/PDF export. It reads billed and collected by job through the `qbo_customer`
aliases F07 writes, shows money on untracked rows as one not-on-a-job figure (D-37), and
adds its own lines to Home's `JOB_NEEDS` (`app/domain/home/checklist.py`) rather than
rewriting the page. It is the feature that first reads `deposit_identification` and
`fuel_surcharge_treatment`: it removes their `waiting` sentence in `POLICY_KEYS`, gives
each key its control (a picker of QuickBooks items, once it confirms that synced invoice
lines carry the item id) and adds them to `REQUIRED_POLICY_KEYS` in the same change
(F04.1). Not started; the owner supplies the brief. Copy it here, expand it, and restate
the acceptance criteria before coding. After it, **F08.1 · Pay applications** as D-36
restates it.

Standing state from F07 to F07.3: the job is the reporting unit; matching is by alias
(`job_alias`), never by name; names reach only the pure suggestion functions
(`app/domain/jobs/suggest.py`, rules in `names.py`) and the picker's search (a read). The
revised contract, unapproved change orders and a job's EAC are computed on read
(`app/domain/jobs/contract.py`), never stored. A work-area kind is the platform's
suggestion until a person confirms it. Linking a QuickBooks row tracks it (D-37); untrack
waits for unlink. Every job, track and untrack action writes one audit row naming the
rows it touched. Review items are pure generators (`app/domain/jobs/issues.py`);
`LEDGER_PROJECT_NO_JOB` is raised for tracked, active rows with no job only (D-37, D-38);
F09 persists them. Home (`GET /api/home`) composes the functions the linked pages call;
its lines and the job-need order are pure (`app/domain/home/checklist.py`); the policy
keys needed now are `REQUIRED_POLICY_KEYS` there, extended by each feature that starts
reading a key. Policy (F04.1): no key has a default; the reference is optional and who
and when are always recorded; a key whose feature has not arrived carries `waiting` in
`POLICY_KEYS`, shown in place of "Decide" and returned by the route as a 409; `set_policy`
accepts any key. The owner's reviewed 67 Elm Street workbook
(`estimate_upload_EST6115758_reviewed_10.06.xlsx`, 29 work areas; it replaced the F06 and
v2 files on 2026-10-06, with the reviewed Turley workbook) is the job the owner is working;
later features are accepted against it.

## Discovered
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
