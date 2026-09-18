# current-feature.md

_No feature in flight (2026-09-18)._ **F04 · Tenant configuration is built**; its brief is
`docs/briefs/F04.md`. The owner's browser pass (last F04 acceptance criterion) is still
open: load Rye Beach's rules (`scripts/load_suggest_rules.py --tenant rye-beach --file
tests/fixtures/rye_beach/account_suggest_rules.json`), then with `make api`, `make web`,
`make worker` running: upload the Rye Beach chart → 159 suggestions, 29 in job cost, the
unconfirmed count at the top → Cost codes grid shows 410 against 5410 → correct one
suggestion by hand → confirm all and see the count reach zero → set one policy key and
see it recorded with your name → add a burden rate → Accounts page at phone width. When
done, tick it in `docs/briefs/F04.md` and flip F04 to ☑ in `ROADMAP.md`.

Next per ROADMAP: **F05 · QBO connection & sync**. Not started; the owner supplies the
brief. Copy it here, expand it, and restate the acceptance criteria before coding.

## Discovered
Carried from F04 (`docs/briefs/F04.md`, Discovered): no screen for suggestion rules
(script / `PUT /api/config/suggest-rules`); cost categories seeded lazily, not at tenant
creation; `gl_account.ledger_type` is source text, QBO types arrive with F05; a general
"no money as a JSON number" response assertion is still per test.

Carried from F03 (`docs/briefs/F03.md`, Discovered): money in API responses must be
strings (F08; `formatMoney` is ready); `sync_run` and `connection` have no API yet (F05);
Imports keeps cards below 640 px, reports (F08+) use `.table-wrap` with the first column
held.

Carried from F02.1 (`docs/briefs/F02.1.md`, Discovered), still open: pending TOTP secret
is per user, not per session (deferred; rule recorded there); no rendered-browser test
dependency for now; a client user's optional enrol confirm records a second
`login_success`.
