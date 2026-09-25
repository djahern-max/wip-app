# current-feature.md

_No feature in flight (2026-09-25)._ **F05.1 · QBO production connection is built and in
close-out (ROADMAP ◐)**: Rye Beach's own QuickBooks company is connected read-only to tenant
`rye-beach` on jobcost.dev with Intuit production keys (2026-09-22, realm 722764240), 13 months
of billing totals tie to QuickBooks' reports to the cent (owner, 2026-09-23), webhooks are on
and proven on production, `qbo-sandbox` was removed from production with `delete_tenant.py`
(D-28), and `webhook_event` is the one tenant-less ingestion table (D-29). Its brief is
`docs/briefs/F05.1.md`; the runbooks are OPERATIONS.md "Intuit production keys", "Webhooks",
"Intuit-side disconnect", "Rye Beach connection record", "Environments" and "Deleting a tenant".

**Still open in `docs/briefs/F05.1.md`** (when the last one closes: tick it, flip ROADMAP F05.1
to ☑, one-line "F05.1 · close-out" CHANGELOG entry, one commit):
- Account numbers: attached 169 of 169 on production (its chart is the 169-account file of
  2026-09-23; no reload). Remaining: the 114 active QuickBooks accounts without a number,
  explained by group from `scripts/accounts_without_number.py` run on the droplet (OPERATIONS.md
  connection record); then the owner's one-time chart file and oracle update (2630 and the ten
  new numbers; the owner says when).
- S-01 question 4: answered yes 2026-09-25 (BLUEPRINT §13.7; ROADMAP S-01 ☑). Purchases are
  checked in F10 when Ramp card transactions begin.
- The owner's phone pass line: the memo-edit and Disconnect/Reconnect checks are done
  (2026-09-25); the August 2026 total from the phone is not yet confirmed.

Next per ROADMAP: **F06 · Estimate import**: the `EstimateSource` protocol, the LMN export
parser, the closing-report PDF parser (prices only) as fallback, a generic CSV template; dirty
rows load and raise exceptions; estimated cost by cost category per estimate (D-04), work
areas with kept/omitted and the change-order flag against the baseline (D-01), unit-priced
lines in a fixed-price estimate as an exception (D-24), no estimate expected for a pool
(D-30). The Phase B gate applies. Not started; the owner supplies the brief. Copy it here,
expand it, and restate the acceptance criteria before coding.

Standing state from F05.1: production holds the Production keys only (`QBO_ENVIRONMENT=
production`); the sandbox company stays connected to `qbo-sandbox` on the Mac with the
Development keys, never to `rye-beach`; the owner deploys by hand (`deploy.sh` over ssh, then
`prod_check.py`); migrations are forward-only in production (`wip` at 0009); polling every 15
minutes stays on beside webhooks; on dev, polling runs only while `make worker` runs.

## Discovered
F05.1 (2026-09-22 to 2026-09-25; details in `docs/briefs/F05.1.md`, Discovered):
- For F08 / D-02, from the Rye Beach tie-out (owner, 2026-09-25; none is a tie-out difference): 34 non-voided invoices at 0.00 (29 dated 2025-12-01); 13 zero-amount payments "Created by QB Online to link credits to charges"; sales receipt INV-2984 (2026-01-22, "Cleanup – Unidentified Deposits", deposit account deleted, income 4100), evidence for D-02; payments deposit to five accounts, two deleted; July 2025 credit memos of 257,994.17, one month before the tie-out window; credit memo INV-2464 (2025-12-10).
- The API service logs only uvicorn's access lines; the `app.*` INFO lines never reach journald because only the worker calls `basicConfig` (F23 or a patch).
- A webhook that arrives while a poll is running enqueues nothing; the change lands on the next scheduled poll. A "one more after this" follow-up would shorten that to seconds.
- `Merge` is a webhook operation on Customer, Account, Item, Vendor, Class, Department, Employee and PaymentMethod; what CDC returns after a customer merge needs checking in F07 before `job_alias` links are trusted across a merge.
- `tests/test_rls.py::test_f04_tables_read_zero_rows_of_another_tenant` seeds a `cost_category` slot from `uuid.hex[:2]`, which can collide with a D-23 slot about once in a few hundred params; use a slot outside the D-23 range. One unreproduced setup ERROR in `test_migrations.py::test_0003_data_step…`.

Carried from the stubs of 2026-09-21 and 2026-09-22 (F05, F04, F03, F02.1), still open:
- F05 (2026-09-21): Imports defaulting to "Unparsed file" cost the owner one upload; an empty "Choose a source" option is the fix. The nightly drift check's `drift` outcome and the "fresh backfill needed" state are proven in tests only. A `billing` row for a document whose customer is a sub-customer (not a project) carries the sub-customer's id; F07 decides how sub-customers relate to jobs. `ProjectRef` (a Projects-API id, S-01 (b)) is in the raw payloads only; F07 may want it as a second `job_alias`. A mapping from QuickBooks `AccountType` to `gl_account.ledger_type` is not built (owner answer 10).
- F04: no screen for suggestion rules (script / `PUT /api/config/suggest-rules`); a general "no money as a JSON number" response assertion is still per test; **for the owner**: the real Rye Beach chart has one account the fixture does not (2630); the owner supplies an updated fixture and oracle if it should be added.
- F03: money in API responses must be strings (F08; `formatMoney` is ready); reports (F08+) use `.table-wrap` with the first column held.
- F02.1: pending TOTP secret is per user, not per session (deferred; rule recorded there); no rendered-browser test dependency for now; a client user's optional enrol confirm records a second `login_success`.
