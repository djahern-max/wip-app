# current-feature.md

_No feature in flight (2026-09-17)._ F02.1 closed; its brief is `docs/briefs/F02.1.md`.
Open items carried from F02.1: the owner's browser pass (CLI `create-user` → link →
password → TOTP → recovery codes → login → tenant switcher → logout), and the owner's
approval of the CLAUDE.md sentence naming `read_as_user()` (D-18).

Next per ROADMAP: **F03 · Ingestion framework**. Copy its block here, expand it, and
restate the acceptance criteria before coding.

## Discovered
From the F02.1 enrolment idempotency fix (2026-09-17). Not fixed in passing.
- **Pending TOTP secret is per user, not per session.** Two *live* sessions of one
  client user (optional TOTP, started from a normal session) now get the same pending
  secret; before the fix each call made a new one. Someone holding that user's password
  and a concurrent session could read the secret the user then confirms. Firm users are
  not exposed: only a link opens their enrolment session, and issuing or redeeming a link
  ends every other session and clears the pending secret. Closing it means binding the
  pending secret to the session that started it (owner call; no UI reaches optional
  client enrolment today).
  **Deferred by the owner (2026-09-17); no code change.** Rule for later: "When optional client TOTP enrolment gets a UI, starting enrolment requires re-entering the password and ends the user's other sessions, so exactly one session exists when the secret is shown."
- ~~**`Activate.jsx` has no double-submit guard.**~~ Fixed 2026-09-17 (F02.1 · enrolment
  follow-ups): `busy` flag, submit disabled while the request is in flight. Login,
  TotpVerify and the TotpEnrol confirm form already had it.
- ~~**Proposed check, no new dependency.**~~ Built 2026-09-17:
  `backend/tests/test_frontend_effects.py`. A rendered StrictMode test (jsdom or a
  browser driver) still needs a new dependency: ask first.
- **No rendered-browser test dependency for now** (owner, 2026-09-17): no jsdom or
  browser driver is added. Revisit when an admin UI exists. Until then the static check
  above and the owner's browser pass cover the React screens.
