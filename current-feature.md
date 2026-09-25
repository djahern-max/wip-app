# current-feature.md

_No feature in flight (2026-09-25)._ **F06 · Estimate import is built** (2026-09-25; ROADMAP ◐
until the owner's production pass and the 80-row fixture close): estimates, work areas and
cost lines by cost code enter the spine from the platform's own estimate template (D-32),
with versions, the D-01 baseline, the `EST_*` sentences, the Estimates page and the runbook.
Its brief is `docs/briefs/F06.md` with two criteria open for the owner: the production pass
(upload `estimates_2026-09-17.xlsx` and the two template fixtures on `rye-beach`; the
Estimates page shows 80 estimates; the 67 Elm Street detail shows EAC in the basis
286,634.20 once `wip_basis` is set for the tenant; re-uploading one file says nothing
changed) and the 80-row workbook itself (`backend/tests/fixtures/rye_beach/estimates/
estimates_2026-09-17.xlsx`, Estimates sheet only, anonymized client names, the Mijal row
with a blank id; two tests skip until it exists). Migration 0010 is applied on dev `wip`;
production is migrated by the owner's deploy.

**Owner work that follows from F06, outside any feature** (the owner says when):
- Set the `wip_basis` policy on `rye-beach` (Configuration → Policy, `firm_admin`; D-04)
  so the Estimates detail shows EAC in the basis rather than "Not decided".
- The estimating change D-32 describes: the team estimates on the cost codes from spring
  2027; in-flight sold estimates get their cost lines from the item detail, one at a time.

Next per ROADMAP: **F07 · Job spine & crosswalk**: `job`, `job_alias`, `job_estimate` with
roles; the "sold estimate → new job or attach as change order" review screen; link job ↔
QuickBooks project with fuzzy suggestions (id-in-name first, then customer + address
similarity); customer merge suggestions. The Phase B gate applies. Not started; the owner
supplies the brief. Copy it here, expand it, and restate the acceptance criteria before
coding.

Standing state from F06: the template is the only estimate source (no vendor parser,
ever); `estimate_cost` hangs off `estimate_work_area` and keeps the cost code as text; the
per-estimate exceptions are pure generators computed on read (F09 persists them); file-level
facts live in `import_batch.issues`; a test module that drives the worker must import the
task module it needs (`tests/estimate_helpers.py` does for `estimates.normalize`).

## Discovered
F06 (2026-09-25):
- For F07: `estimate.client_name` and `jobsite` are text; the "sold estimate → job" review
  needs `job_alias('lmn_estimate', external_id)` and the customer link by id. The 67 Elm
  fixture carries no estimator, client or jobsite (blank on the Estimates sheet).
- For F09: the generators in `app/domain/estimates/exceptions.py` return `Issue(code,
  message, detail)`; `EST_NO_ID` and the rows-not-loaded sentences are on the batch, not
  per estimate; F09 reads both.
- The Imports page still defaults to "Unparsed file" (F05 Discovered); with a third
  production source kind the empty "Choose a source" option is worth doing.
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
- `Merge` is a webhook operation on Customer, Account, Item, Vendor, Class, Department, Employee and PaymentMethod; what CDC returns after a customer merge needs checking in F07 before `job_alias` links are trusted across a merge.
- CI #33 (2026-09-25): the NOTIFY wake-up test raced the worker's `LISTEN` registration; fixed (listener opened before the first pass, `Worker.listening` readiness event, test waits on it). Two other intermittents remain open, below.
- `tests/test_rls.py::test_f04_tables_read_zero_rows_of_another_tenant` seeds a `cost_category` slot from `uuid.hex[:2]`, which can collide with a D-23 slot about once in a few hundred params; use a slot outside the D-23 range. One unreproduced setup ERROR in `test_migrations.py::test_0003_data_step…`.

Carried from the stubs of 2026-09-21 and 2026-09-22 (F05, F04, F03, F02.1), still open:
- F05 (2026-09-21): Imports defaulting to "Unparsed file" cost the owner one upload; an empty "Choose a source" option is the fix. The nightly drift check's `drift` outcome and the "fresh backfill needed" state are proven in tests only. A `billing` row for a document whose customer is a sub-customer (not a project) carries the sub-customer's id; F07 decides how sub-customers relate to jobs. `ProjectRef` (a Projects-API id, S-01 (b)) is in the raw payloads only; F07 may want it as a second `job_alias`. A mapping from QuickBooks `AccountType` to `gl_account.ledger_type` is not built (owner answer 10).
- F04: no screen for suggestion rules (script / `PUT /api/config/suggest-rules`); a general "no money as a JSON number" response assertion is still per test; **for the owner**: the real Rye Beach chart has one account the fixture does not (2630); the owner supplies an updated fixture and oracle if it should be added.
- F03: money in API responses must be strings (F08; `formatMoney` is ready); reports (F08+) use `.table-wrap` with the first column held.
- F02.1: pending TOTP secret is per user, not per session (deferred; rule recorded there); no rendered-browser test dependency for now; a client user's optional enrol confirm records a second `login_success`.

From this brief, for F07: EST6281138 (Sanford, pending, 998.71) and EST6346291 (Hess, sold, 39,032.44) are two estimates for the same project under two client names; the Turley pair EST6366990 and EST6120638; the Mijal row once it has an id; "Mighty Roots - Makarov | Site Work" has no LMN id in its QuickBooks project name (§13.2).
