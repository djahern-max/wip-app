# current-feature.md

**F09.6 · The job page for a client** (design; built 2026-10-27, no migration, D-50 appended;
the owner's pass on jobcost.dev is open, to be made signed in as a client role first;
`docs/briefs/F09.6.md`). The job page is the client's: the head with the billing request
as its one primary action, "Where the job stands" (seven facts from the fields the
Contract and Billing headings held), Attention with a plain left rule, one work-areas
table with the approval control in the row and Price-only totals and aside rows, the
invoices by document with the deposit row first, the pay applications and the approval
history. The set-up blocks moved as they are to `JobSetup.jsx`, a view under Jobs drawn
for firm roles only, reached from the firm line under the head ("Open set-up"). No figure,
request, audit row or permission changed; `client_admin` is a client role on the screens.
Next design feature: **F09.7 · The firm's set-up view of a job, designed** (the owner
supplies the mockup first; the blocks, requests and controls are as lifted).

**F09.5 · Jobs: the Sold Jobs Board** (design; built 2026-10-16, no migration, D-49 appended;
the owner's pass on jobcost.dev is open; `docs/briefs/F09.5.md`). The Jobs page under
the F09.1 paint: page head with the one primary action, the tie-out line in the status
colour when every month ties, labelled filters, the board in its box with the
not-on-a-job explanation under the table, the pool form as a card. No figure, request or
action changed. The Review queue and Unapproved change orders are later design features.

_No feature in flight (2026-10-15)._ **F05.3 · History start is closed** (built 2026-10-15,
commit f2fb590; D-48 appended; **the owner's pass on jobcost.dev, tenant `rye-beach`,
passed 2026-10-15 after a full database rebuild; ROADMAP ☑**). The brief, with the Plan,
the six picks, the step-1 proof, the build notes, the pass record and the Discovered
list, is `docs/briefs/F05.3.md`. What it changed: each company has one `history_start`
(a calendar date on Configuration, Policy, no default, first on the list and first in
`REQUIRED_POLICY_KEYS`); QuickBooks cannot be connected and nothing is pulled until it
is set (connect, Sync now and a fresh backfill answer 409 with one sentence; Connections
offers "Set history start"; a poll or nightly check on a company connected before the
key ends `failed` with `history_start_not_set` and no successor); reference entities are
held whole and transactional entities are paged and counted with `TxnDate >=` the start
(`fetch.py`, the date read once onto `sync_run.detail`); a pre-start record arriving by
poll is stored raw, skipped by its normalizer and counted under "Before history start";
a held record re-dated before the start gets `deleted_at`; the key cannot change after
the first completed backfill. Rye Beach's history start is 2026-01-01; its backfill from
it held 885 invoices, 953 payments, 2,041 purchases, 819 bills, 335 deposits, 264 journal
entries (every entity's counts in the brief and in OPERATIONS.md "Rye Beach connection
record"), with the exact duration still to be read from `sync_run` and pasted there, and
the ten proof lines still to be pasted into the brief's Progress. Nothing in `app/wip/`,
`app/domain/jobs/` or `figures.py`; no figure changed; no migration. The proof script
was deleted at close-out (CLAUDE.md, Code conventions).

Open passes carried: **F09.6** (`docs/briefs/F09.6.md`; signed in as a client role first), **F09.5** (`docs/briefs/F09.5.md`), **F09.4** (`docs/briefs/F09.4.md`), **F09.3** (`docs/briefs/F09.3.md`),
**F09.2** (`docs/briefs/F09.2.md`), **F07.3** (`docs/briefs/F07.3.md`), **F07.2**
(`docs/briefs/F07.2.md`; deploying it runs migration 0012), **F07.1**
(`docs/briefs/F07.1.md`) and **F07** (the pass job by job; 67 Elm Street passed
2026-10-01). Production was rebuilt from an empty database on 2026-10-15, so `rye-beach`
holds no job yet: the F07 passes need the estimates uploaded again first. D-08 is still
open. Retainage is owed as a later feature with its own decision before any tenant that
holds it (D-43; a Later line in ROADMAP). D-50 (one practice operates the platform; a
client works only on its jobs; D-17 extended) was made in F09.6 on 2026-10-27.

Next per ROADMAP: **F09.7** (design, above), then **F10 · GL cost sync**: Bills, Vendor Credits, Purchases (card, check,
cash) and Journal Entries to `ledger_line` at line level with job reference, account and
vendor; `account_map` applied; `COST_UNASSIGNED` and `COST_DIVISION_MISMATCH` exceptions
(F09 queues them once they exist: a registry row each with its severity, §10); accept:
GL-direct job cost + unassigned = QuickBooks P&L cost of goods sold by account for each
closed month; a Ramp-synced expense with a Customer/Job value lands on the right job with
its receipt link. Every cost-side entity is held from the history start (D-48), so the
first tie-out runs from 2026-01. Not started; the owner supplies the brief. Copy it here,
expand it, and restate the acceptance criteria before coding.

Standing state from F07 to F09.4 and F05.3: the job is the reporting unit; matching is
by alias (`job_alias`), never by name. The original contract, approved change orders,
revised contract, unapproved change orders, EAC and every billing figure are computed on
read (`app/domain/jobs/contract.py`, `app/domain/billing/figures.py`, `board.py`,
`work_areas.py`, `pay_applications.py`), never stored; the rows written by F07.4, F08.1
and F09 are approvals and withdrawals (`change_order_approval`), assignments and clears
(`billing_line_work_area`, both append-only), the pay applications with their schedule as
it stood at issue and the three figures frozen at issue (`pay_application`,
`pay_application_line`; a draft is edited until issued, or discarded, F08.3; never voided),
and the exceptions with their history (`exception`, `exception_event` append-only; the
sentence and detail as last raised, never the truth); nothing in `app/wip/` has been
touched. Billed to date is total − sales tax − fuel surcharge lines, the deposit invoice
counted from its date; per work area it is the tied lines and the pay applications (D-45).
Collected to date is cash (D-41). The copy and every month tie-out begin at the history
start (D-48). `REQUIRED_POLICY_KEYS` holds the six keys read today, the history start
first. The interface follows D-47 (F09.1 to F09.6: header, Home, Estimates, Imports,
Configuration, the Jobs board, the job page) and D-50 (a screen is written for one reader;
firm screens and client screens, CLAUDE.md Vocabulary); the set-up view of a job (F09.7),
Connections, Customers, the Review queue, Unapproved change orders and Exceptions are later
design features.

## Discovered
Carried from `docs/briefs/F05.3.md` (copied unchanged 2026-10-15; nothing is dropped):
Carried from F09.4 (copy the Discovered list of `docs/briefs/F09.4.md` here word for word; the design partner could read only these items):
- The Policy descriptions come from the server and read like notes for a developer: "Period boundaries are tenant-local months.", "1 = January … 12 = December.", "(BLUEPRINT §8.3)", and the decision numbers in four of them. Backend copy in `backend/app/domain/config/policy.py`; the page-side hint for the WIP basis (`WIP_BASIS_HINT` in `policyRow.js`) repeats "(BLUEPRINT §8.3)" and was left to change with them.
- The count called "Unmapped" includes accounts that carry a suggestion, and the Show filter's "Unmapped" lists them too, while each such row's own status reads "Suggested". Wording for the owner to decide (for example "Not confirmed"); the API field and the Home checklist sentence use the same word.
- The Policy date is in the browser's own format ("10/4/2026"), like the Imports dates (F09.3).
- Customers still uses the old `.subnav` links; it is a later design feature.
- Owner's passes open: F07 (job by job; 67 Elm Street passed 2026-10-01), F07.1, F07.2 (deploying it runs migration 0012), F07.3, F09.2, F09.3, F09.4.
- D-08 open. Retainage: its own decision before any tenant that holds it (D-43).
- The nine ledge days on 6115758 are expected to move to a time-and-materials job (D-24); the owner decides with the project manager.
- T&M_930 line 6 on 6115758 has service date 2023-09-22 in QuickBooks; the owner's to correct.
Carried from F09.5 and F09.6 (`docs/briefs/F09.6.md` Discovered, word for word):
- The tie-out's "Does not tie" has no colour (D-49 keeps it that way); when the WIP schedule (F12) carries a tie-out line, the same mark applies.
- "Days since last activity" reads "No activity" in words in a numeric column; it is the API's rule (`daysWords`) and was left.
- The Review queue and Unapproved change orders still carry the shared paint only; next design features after F09.7.
- A "needs attention" status colour would mark the attention block; D-47 point 2 needs its own line in a later brief.
- F09.7 · The firm's set-up view of a job: design the blocks lifted in F09.6; on the ROADMAP.
- `design-process.md` in the Claude Project said the next number was F09.4; corrected there, outside the repo.
- The API's `can_manage_jobs` (`app/core/authz.py`, `FIRM_AND_CLIENT_ADMIN`) still includes `client_admin`, as does `JOB_MANAGER_ROLES` in `Shell.jsx` (Customers in the navigation); on the screens `client_admin` is a client role since D-50 and never reaches the set-up view. A later brief aligns the permission (and the Customers link) with D-50.
- The unapproved change-order rows' billed to date and left to bill sums are a later API figure; `change_order_totals` covers the estimates attached as change orders whether approved or not and misses change-order rows on the original estimate, so the aside row carries Price only.
- The job response carries the division's code, not its name, so the client's head line leaves the division off (the owner, 2026-10-27). A name would need a field on `JobRowOut`; the head line then shows it.
- `test_frontend_effects.py` reads an apostrophe in JSX text as the start of a string and fails with "unbalanced source" without naming the file; a sentence on the job page was reworded to avoid it. The scanner should skip JSX text, or the test should say which file.
- The Approval history's "Recorded" and the set-up view's "Created" and "Attached" read in the browser's own format (`toLocaleString`), like the Imports and Policy dates (F09.3, F09.4).
- `PayApplications.jsx` keeps its own `<h3>` and hint; the section was not redesigned here beyond the `.empty` card and the decision numbers; a later design feature covers the request form and the application detail.
From the owner's pass of 2026-10-15:
- During a backfill, Connections showed "Payloads that could not be read: Customer: 1000" while the customers' normalizer had not yet run; it read 0 after the run. A count should not say "could not be read" for a payload the run has not reached.
- delete_tenant.py waits silently on the append-only lock while wip-worker and wip-api run; the runbook should say to stop both first, or the script should say it is waiting. The owner rebuilt by dropping the database instead.
- After a completed backfill, Policy still offers Change on the history start; the refusal sentence shows at the top of the page in red, and the form stays open with Record decision enabled. Offer no Change link and put the sentence in its place (D-47; an F09.4-style patch).
- The backend suite took 37 minutes on 2026-10-15 (1,008 tests). Find what is slow.
- The cost-side counts rose by 22 between the 05:06 proof and the 11:32 backfill (Deposit +1, Bill +4, Purchase +12, JournalEntry +5): documents entered in QuickBooks that morning. Expected; noted so F10's first tie-out is not surprised.
