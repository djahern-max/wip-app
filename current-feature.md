# current-feature.md

_No feature in flight (2026-10-01)._ **F07.1 · Job screens patch is built** (2026-10-01;
761 backend, 45 frontend) and waits for the owner's pass on jobcost.dev: a second job
through the review select and "Confirm all as suggested"; 67 Elm Street's rows reading
"…, confirmed" and its Sold on corrected. Its brief, with the Plan, the owner's answers
and the build notes, is `docs/briefs/F07.1.md`. ROADMAP F07.1 is ◐. **F07 · Job spine &
crosswalk** stays ◐ on the same pass, job by job (restated 2026-10-01; the owner ticks
it): 67 Elm Street passed on 2026-10-01; the other sold estimates are reviewed as the
owner reaches them; P0-1 (the QuickBooks projects for the jobs with money on them, D-35)
gates only the links. No migration since 0011; the dev `wip` database is at 0011.

**D-02** (a deposit is one advance invoice, deferred by the WIP entry; 2420 not created)
and **D-36** (the platform produces a pay application with a schedule of values; the
invoice refers to it) were decided 2026-10-01 and are in `docs/DECISIONS.md`; BLUEPRINT
§8.6, §13.4, §14 and ROADMAP F08.1 say so. **F06.1**, **F06** are closed; **F05.2 ·
Brand assets** stays ◐ until the owner's browser pass on jobcost.dev and the swap test
(`docs/briefs/F05.2.md`).

Next per ROADMAP: **F08 · Sold Jobs Board & Job Detail (billing side)**: reports 1 and 2
of §9, billing half only; the deposit identified by the `_DEP` document number and the
tenant's deposit item (D-02), counted in billed to date; the unapplied-payment review
item; XLSX/PDF export. It reads billed and collected by job through the `qbo_customer`
aliases F07 writes. Not started; the owner supplies the brief. Copy it here, expand it,
and restate the acceptance criteria before coding. After it, **F08.1 · Pay applications**
as D-36 restates it.

Standing state from F07 and F07.1: the job is the reporting unit; matching is by alias
(`job_alias`), never by name; names reach only the pure suggestion functions in
`app/domain/jobs/suggest.py` (rules in `names.py`). The revised contract, unapproved
change orders and a job's EAC are computed on read (`app/domain/jobs/contract.py`),
never stored. A work-area kind is the platform's suggestion until a person confirms it,
one at a time or all at once ("Confirm all as suggested", one audit row per work area);
a confirmed kind is carried to the next version when order number and name both match
(`estimates.normalize`). A job may hold several QuickBooks rows; one row belongs to at
most one job. Every job action writes one audit row (`entity_type = job`) naming the rows
it touched; "(set when created)" on Sold on is read from that log. Review items are pure
generators (`app/domain/jobs/issues.py`), §10 names where §10 has one; F09 persists them.
The second 67 Elm Street fixture (`estimate_upload_EST6115758_v2.xlsx`, 29 work areas) is
the job the owner is working; later features are accepted against it.

Standing state from F06.1 and F06: burden is computed on read from the active
`burden_rate` rows and never stored; the template is the only estimate source; per-estimate
exceptions are pure generators; file-level facts live in `import_batch.issues`.

## Discovered
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
