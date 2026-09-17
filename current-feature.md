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
- **`Activate.jsx` has no double-submit guard.** A double click posts the single-use
  token twice; the second answer is 400 "invalid or expired link" although the first
  succeeded. Not a mount effect. Fix: a `busy` flag like the other forms.
- **Proposed check, no new dependency**: a static test (pytest, in the style of
  `test_hygiene.py`) that fails when a `useEffect` body in `frontend/src` calls
  `api(` with a method other than GET outside an allow-list of guarded files.
  A rendered StrictMode test (jsdom or a browser driver) needs a new dependency: ask first.

