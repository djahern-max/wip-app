# current-feature.md

_No feature in flight (2026-09-18)._ **F03 · Ingestion framework closed 2026-09-18**; its
brief is `docs/briefs/F03.md` (built 2026-09-17; close-out fixes, F03.1 interface baseline
and the owner's browser pass 2026-09-18). D-22 and the CLAUDE.md "Interface conventions"
section are in place (78aa6a0); the earlier "not received" and "still owed" notes are
closed, not deleted: see the correction lines in the CHANGELOG close-out entry.

**Local dev data**: rebuilt by the owner for the browser pass. If it is ever wiped again,
follow "Rebuild local dev data" in `docs/OPERATIONS.md`; downgrades are guarded
(CLAUDE.md Workflow 7).

Next per ROADMAP: **F04 · Tenant configuration**. Not started; the owner supplies the
brief. Copy it here, expand it, and restate the acceptance criteria before coding.

## Discovered
Carried from F03 (`docs/briefs/F03.md`, Discovered):
- **Money in API responses (F08).** FastAPI's default encoder turns `Decimal` into
  `float` on the way out; the first feature that returns money must serialize it as a
  string through the response schemas, with a response-scan assertion. The frontend
  side is ready: `formatMoney` in `frontend/src/money.js` takes the string.
- **`sync_run` and `connection` have no API yet** (F05).
- **Imports keeps cards below 640 px** (by design for an admin screen). Reports (F08+)
  must use the `.table-wrap` container-scroll pattern with the first column held.

Carried from F02.1 (`docs/briefs/F02.1.md`, Discovered), still open:
- **Pending TOTP secret is per user, not per session.** Deferred by the owner
  (2026-09-17). Rule for later: when optional client TOTP enrolment gets a UI, starting
  enrolment requires re-entering the password and ends the user's other sessions.
- **No rendered-browser test dependency for now** (owner, 2026-09-17).
- **A client user's optional enrol confirm records a second `login_success`** (and a
  second `tenant_enter` when a tenant is active). Not fixed in passing.
