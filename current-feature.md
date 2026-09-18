# current-feature.md

_No feature in flight (2026-09-17)._ F03 is built; its brief is `docs/briefs/F03.md`.
The owner's browser pass (last F03 acceptance criterion) is still open: with `make api`,
`make web`, `make worker` running, upload a file as `unparsed_file` → status reaches
`loaded` after a manual Refresh → upload the same file again and see the duplicate notice
and still one row → download it and get the same bytes → stop the worker, upload a
different file, see it wait at `received`, start the worker, see it complete. When done,
tick it in `docs/briefs/F03.md` and flip F03 to ☑ in `ROADMAP.md`. D-19–D-21 are in
`docs/DECISIONS.md` (the staged file `DECISIONS_D-19_to_D-21.md` at the repo root can go).

**Local dev database note (2026-09-17)**: the `wip` database was emptied by a migration
round-trip run during F03 close-out. Rebuild it with the four commands under
"Rebuild local dev data" in `docs/OPERATIONS.md` before the browser pass. Downgrades
are now guarded (CLAUDE.md Workflow 7).

**Still owed from the F03 review (2026-09-18)**: the D-22 text for `docs/DECISIONS.md`
and the CLAUDE.md "Interface conventions" section were never received; the owner
pastes them. `formatMoney`, the 390 px layout and the cursor function are built.

Next per ROADMAP: **F04 · Tenant configuration**. Copy its block here, expand it, and
restate the acceptance criteria before coding.

## Discovered
Carried from F03 (`docs/briefs/F03.md`, Discovered):
- **Money in API responses (F08).** FastAPI's default encoder turns `Decimal` into
  `float` on the way out; the first feature that returns money must serialize it as a
  string through the response schemas, with a response-scan assertion.
- **`sync_run` and `connection` have no API yet** (F05).

Carried from F02.1 (`docs/briefs/F02.1.md`, Discovered), still open:
- **Pending TOTP secret is per user, not per session.** Deferred by the owner
  (2026-09-17). Rule for later: when optional client TOTP enrolment gets a UI, starting
  enrolment requires re-entering the password and ends the user's other sessions.
- **No rendered-browser test dependency for now** (owner, 2026-09-17).
- **A client user's optional enrol confirm records a second `login_success`** (and a
  second `tenant_enter` when a tenant is active). Not fixed in passing.
