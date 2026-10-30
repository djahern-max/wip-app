# current-feature.md

**F08.4 · Split an earlier invoice line across work areas** (built 2026-10-30 in one commit,
migration 0018, D-52 appended, in express mode; the owner's pass on jobcost.dev, tenant
`rye-beach`, is open: deploying it runs the migration (every existing assignment row takes
its line's amount), then the pilot job's lump-sum line is split by the amounts in its
billing workbook; `docs/briefs/F08.4.md`). A person splits one earlier invoice, credit-memo
or sales-receipt line across work areas by amount, the parts summing to the line to the
cent; billed to date per work area reads the parts, "Percent billed" per work area is on
the job page for every role, the first pay application's prefill and "earned on previous
applications" read what the parts stand for, and no job-level figure moves. One audit row
per line, listing the parts. Next per ROADMAP: **F11 · Labor import & costing**
(`LaborSource` protocol, the LMN timesheet parser and the isolved register parser →
`employee_rate` effective-dated, encrypted and restricted, `labor_entry`, burden applied,
overtime where the source provides it; accept: computed unburdened job labor against GL
5x10 by division per month with the unallocated difference, exceptions for rows without a
job or a rate, `client_pm` sees totals only). Not started; the owner or the design partner
supplies the brief. Copy it here, expand it, and restate the acceptance criteria before
coding.

**F10 · GL cost sync** (built 2026-10-29, migration 0017, D-51; the owner's pass on
jobcost.dev is open: deploying it runs the migration and the worker's start-up cost run
fills `ledger_line`; `docs/briefs/F10.md`). **F09.6 · The job page for a client** (design;
built 2026-10-27, no migration, D-50 appended; the owner's pass on jobcost.dev is open, to
be made signed in as a client role first; `docs/briefs/F09.6.md`). **F09.5 · Jobs: the
Sold Jobs Board** (design; built 2026-10-16, D-49; the owner's pass is open;
`docs/briefs/F09.5.md`). **F09.7 · The firm's set-up view of a job** moved to directly
after F12 on 2026-10-29 (express mode): the set-up view is designed once, after F12
reshapes the job page.

Open passes carried: **F08.4** (`docs/briefs/F08.4.md`; the pilot job's lump-sum line split
by the workbook's amounts, Percent billed against the workbook, the first application's
"earned on previous applications" to the cent, the audit row on `GET /api/audit`), **F10**
(`docs/briefs/F10.md`; Connections' held counts, the Cost page for 2026-09 against the
QuickBooks Profit and Loss by account, the unassigned list, one Ramp card transaction on
its job with its memo, 67 Elm Street as `client_pm` against the Transaction Detail by
Customer; `backend/scripts/cost_ramp_fields.py` answers Plan question 2 at the pass and is
deleted at its close-out), **F09.6** (`docs/briefs/F09.6.md`; signed in as a client role
first), **F09.5** (`docs/briefs/F09.5.md`), **F09.4** (`docs/briefs/F09.4.md`), **F09.3**
(`docs/briefs/F09.3.md`), **F09.2** (`docs/briefs/F09.2.md`), **F07.3**
(`docs/briefs/F07.3.md`), **F07.2** (`docs/briefs/F07.2.md`; deploying it runs migration
0012), **F07.1** (`docs/briefs/F07.1.md`) and **F07** (the pass job by job; 67 Elm Street
passed 2026-10-01). They are taken together after F08.4 is deployed, on the pilot job.
Production was rebuilt from an empty database on 2026-10-15, so `rye-beach` holds no job
yet: the F07 passes need the estimates uploaded again first. D-08 is still open. Retainage
is owed as a later feature with its own decision before any tenant that holds it (D-43).

Standing state from F07 to F08.4: the job is the reporting unit; matching is by alias
(`job_alias`), never by name. The original contract, approved change orders, revised
contract, unapproved change orders, EAC, every billing figure (per work area too: the
parts of a split, D-52) and every cost figure are computed on read
(`app/domain/jobs/contract.py`, `app/domain/billing/figures.py`,
`app/domain/billing/work_areas.py`, `app/domain/cost/figures.py`), never stored; the rows
written are approvals and withdrawals, assignments, splits and clears (one event's rows
share `recorded_at`; each carries its signed `amount`), the pay applications with their
schedule as it stood at issue, the exceptions with their history, and the ledger lines as
QuickBooks posted them (`ledger_line`, rewritten by the normalizer, never by a person);
nothing in `app/wip/` has been touched. Billed to date is total − sales tax − fuel
surcharge lines; collected to date is cash (D-41); cost to date is Σ ledger lines on the
job on in-job-cost accounts, by cost category, in the WIP basis or memo (D-04, D-51);
labor-slot lines are tied, never costed (F11 costs labor from hours and rates). The copy
and every month tie-out begin at the history start (D-48). The interface follows D-47 and
D-50; the Cost page and the set-up view are firm screens.

## Discovered
Carried from `docs/briefs/F08.4.md` (copied unchanged 2026-10-30; nothing is dropped):
Carried from `docs/briefs/F10.md` and the live stub (copy unchanged from `git show HEAD:current-feature.md` at the start of the build; nothing is dropped). Closed by this brief when built: none.

From this build (2026-10-30):
- `_write_billing` (`app/domain/billing/sync.py`) rewrites a changed document's lines by delete and insert, while `billing_line_work_area.billing_line_id` is `ON DELETE RESTRICT`: a new version of a document with an assigned or split line (a void, a corrected amount) would be refused at the delete and the poll would report the document as failed. Not seen on `rye-beach` yet (no assigned document has been re-applied). Fix in a later patch: carry the assignment rows across the rewrite by `external_line_id`, or update lines in place.
- D-28 names deleting a whole tenant as the one time the append-only triggers are suspended; migration 0018's data step is a second (the owner's eye; see "Where the build differs").
- The audit page is a JSON read (`GET /api/audit`, F02); a viewer screen is still owed. The split row's `detail.summary` is written so a screen can print it as it is.
- `Jobs.jsx` and `Shell.jsx` are routed by state, not by URL, so a page cannot be opened by address for a render or a link from an e-mail.
- The set-up view's split form lists every pickable work area (29 on 67 Elm Street); a filter by number or a shorter list for a long estimate is a design question for F09.7.

Added 2026-10-30 (from the design partner's pilot sheets):
- The job page should show the estimate version an invoice line was assigned under; a line assigned under version 1 reads against version 2's scheduled value today (pilot 6541802, "Plantings" 0.5 × 6,824.28 against 8,335.92).
- The pilot sheets of 2026-10-21 and 2026-10-30 proposed a warn for a `_DEP` invoice not on the deposit item; `DEPOSIT_NOT_IDENTIFIED` already does it (F08). The sheets are corrected outside the repo. OPERATIONS should say plainly that both marks are needed (Docs, below).
