# current-feature.md

_No feature in flight (2026-09-21)._ **F05 · QBO connection & sync (sandbox) is closed**: the
owner's pass on the live sandbox was done on 2026-09-21 and every criterion is ticked. Its brief
is `docs/briefs/F05.md`; findings from the spike are `docs/spikes/S-01.md`.

Next per ROADMAP: **F05.0 · First deployment (jobcost.dev)**: the platform live at jobcost.dev
with the privacy policy and terms pages Intuit requires for production keys (D-25). Not
started; its acceptance criteria are still to be written; the owner supplies the brief. Copy
it here, expand it, and restate the acceptance criteria before coding. After it, **F05.1 · QBO
production connection** (Intuit's production questionnaire and keys, Rye Beach connected
read-only, monthly totals tied to Rye Beach's own reports, webhooks, S-01's Ramp line check).

Standing state from F05: the sandbox company stays connected to tenant `qbo-sandbox` and never
to `rye-beach`; polling and the nightly check run only while `make worker` runs; the fixtures
under `backend/tests/fixtures/qbo_sandbox/` are re-recorded with
`scripts/qbo_record_fixtures.py` (delete a file to record it again).

## Discovered
Carried from F05 (`docs/briefs/F05.md`, Discovered), plus what F05 added:

Carried from the stub of 2026-09-20:

Carried from F04 (`docs/briefs/F04.md`, Discovered): no screen for suggestion rules
(script / `PUT /api/config/suggest-rules`); `gl_account.ledger_type` is source text, QBO
types arrive with F05; a general "no money as a JSON number" response assertion is still
per test; Imports defaults to "Unparsed file" on a first visit (an empty "Choose a
source" option would make the choice explicit); **for the owner**: the real Rye Beach
chart has one account the fixture does not (2630), and the owner supplies an updated
fixture and oracle if it should be added.

Carried from F03 (`docs/briefs/F03.md`, Discovered): money in API responses must be
strings (F08; `formatMoney` is ready); `sync_run` and `connection` have no API yet (F05);
Imports keeps cards below 640 px, reports (F08+) use `.table-wrap` with the first column
held.

Carried from F02.1 (`docs/briefs/F02.1.md`, Discovered), still open: pending TOTP secret
is per user, not per session (deferred; rule recorded there); no rendered-browser test
dependency for now; a client user's optional enrol confirm records a second
`login_success`.

Notes on the carried items (2026-09-20): "`sync_run` and `connection` have no API yet" is
answered by this feature (`GET /api/qbo/status`). "QBO types arrive with F05" is **not** in
this brief's scope: F05 stores Account raw and attaches the id only; what `ledger_type`
should become once QuickBooks account types are on hand is left for the owner (plan
question 10).

F05 (2026-09-20): a mapping from QuickBooks `AccountType` to the chart file's type text
(`gl_account.ledger_type`) is not built here; F05 attaches `external_id` only (owner answer 10).

F05 (2026-09-21): Imports defaulting to "Unparsed file" cost the owner one upload (the sandbox
chart went in unparsed the first time); the empty "Choose a source" option is still the fix.
The nightly drift check has not yet run live (first run queued for 07:00 UTC on 2026-09-21);
its `drift` outcome and the "fresh backfill needed" state are proven in tests only. A
`billing` row for a document whose customer is a sub-customer (not a project) carries the
sub-customer's id; F07 decides how sub-customers relate to jobs. `ProjectRef` (a Projects-API
id, S-01 (b)) is in the raw payloads only; F07 may want it as a second `job_alias`.
