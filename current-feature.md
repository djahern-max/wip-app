# current-feature.md

_No feature in flight (2026-09-22)._ **F05.0 · First deployment (jobcost.dev) is closed**: the
platform runs at https://jobcost.dev (one droplet, Managed Postgres, Space `jobcost-files`;
D-27), the owner's pass from a phone on 2026-09-22 is done and every criterion is ticked. Its
brief is `docs/briefs/F05.0.md`; the runbooks are OPERATIONS.md "Production (jobcost.dev)".

Next per ROADMAP: **F05.1 · QBO production connection**: Intuit's production questionnaire
and keys, the launch and disconnect URLs (`/qbo/disconnected` exists; the server-side handling
does not), Rye Beach connected read-only with monthly totals tied to Rye Beach's own
QuickBooks reports, webhooks, S-01's Ramp line check. The Phase B gate applies. Not started;
the owner supplies the brief. Copy it here, expand it, and restate the acceptance criteria
before coding.

Standing state from F05.0: the owner deploys by hand (`deploy.sh` over ssh, then
`prod_check.py`); migrations are forward-only in production; the sandbox company stays
connected to tenant `qbo-sandbox` on production and on dev, never to `rye-beach`; on dev,
polling runs only while `make worker` runs. Production holds test tenants that a
delete-tenant command (Discovered below) will remove.

## Discovered
Test isolation (2026-09-22, from CI #24): `tests/test_worker.py` asserts `run_once() == 1`
/ `== 2` with a worker that sweeps every tenant, so a scheduled change poll seeded by an
earlier QBO test's tenant (next 15-minute wall-clock slot) can be claimed in that count;
under `refuse_all_transport` it would fail and retry. Same cause as the `_drain` fix in
`tests/test_qbo_tasks.py`; pin `_worker()` to the seed tenants when it shows.

F05.0 (2026-09-22, owner pass and droplet work; carry into the F05.1 brief or later):
- Imports download should send `Content-Disposition` with the original filename (the signed Spaces URL serves the object under its content-addressed key).
- The TOTP enrolment page does not recover from a reload mid-enrolment; issuing a new activation link was the workaround.
- A delete-tenant command is needed (test tenants on production).
- OPERATIONS.md needs a short "environments" note: Mac dev, CI, production, and which env file each reads.
- Found on the D-21 check and fixed in 0b703bb (not carried): `S3ObjectStore.open` yielded boto3's non-seekable body; it now spools to a temporary file.

Carried from the stub of 2026-09-21 (F05, `docs/briefs/F05.md`, Discovered; and older), still open:
- F05 (2026-09-21): Imports defaulting to "Unparsed file" cost the owner one upload; an empty "Choose a source" option is the fix. The nightly drift check's `drift` outcome and the "fresh backfill needed" state are proven in tests only. A `billing` row for a document whose customer is a sub-customer (not a project) carries the sub-customer's id; F07 decides how sub-customers relate to jobs. `ProjectRef` (a Projects-API id, S-01 (b)) is in the raw payloads only; F07 may want it as a second `job_alias`. A mapping from QuickBooks `AccountType` to `gl_account.ledger_type` is not built (owner answer 10).
- F04: no screen for suggestion rules (script / `PUT /api/config/suggest-rules`); a general "no money as a JSON number" response assertion is still per test; **for the owner**: the real Rye Beach chart has one account the fixture does not (2630); the owner supplies an updated fixture and oracle if it should be added.
- F03: money in API responses must be strings (F08; `formatMoney` is ready); reports (F08+) use `.table-wrap` with the first column held.
- F02.1: pending TOTP secret is per user, not per session (deferred; rule recorded there); no rendered-browser test dependency for now; a client user's optional enrol confirm records a second `login_success`.
