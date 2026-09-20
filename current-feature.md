# current-feature.md

_No feature in flight (2026-09-20)._ **F04 · Tenant configuration is closed**: the owner's
browser pass was done on 2026-09-19 and the follow-up fixes were checked on 2026-09-20.
Its brief is `docs/briefs/F04.md`.

Next per ROADMAP: **F05 · QBO connection & sync**. Not started; the owner supplies the
brief. Copy it here, expand it, and restate the acceptance criteria before coding. Spike
S-01 (QBO read-only: projects as customers, line-level refs, CDC deletes) has not run and
is the first task of F05; the BLUEPRINT §13.7 developer setup (Intuit developer account,
app, Development keys, a QuickBooks Online Plus sandbox with Projects on and one test
project carrying one invoice and one expense) is done.

## Discovered
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
