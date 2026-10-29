# current-feature.md

**F10 · GL cost sync** (built 2026-10-29 in one commit, migration 0017, D-51 appended, in
express mode; the owner's pass on jobcost.dev, tenant `rye-beach`, is open: deploying it runs
the migration and the worker's start-up cost run fills `ledger_line`; `docs/briefs/F10.md`).
Every QuickBooks line that posts to an expense-side account (Bill, Vendor Credit, Purchase,
Journal Entry, the cost lines of a Deposit) is held in `ledger_line` with its account, vendor,
the line's customer reference, the job it aliases to and the signed amount; division, cost
category, job cost and WIP-basis membership are read from `account_map` and the policy when a
figure is computed (D-51). The Cost page (firm roles and `client_admin`, under Connections):
the tie-out by month and account in five parts that equal the ledger total by construction
(on jobs, owned equipment and fuel on jobs, unassigned, labor in the ledger, outside job
cost), the unassigned list by document, labor in the ledger by month and division, the XLSX
export. The client's job page gains the "Cost to date" fact and a table by cost category.
`COST_UNASSIGNED` and `COST_DIVISION_MISMATCH` are in the F09 queue (the first on the
customer row, or on the company for "no customer"). Next per ROADMAP: **F08.4 · Pay
application per work area** (a patch: percent per work area, overrides per line, earlier
invoices netted per work area, rounding per line; the design partner's call of 2026-10-29,
before F11). Not started; the owner or the design partner supplies the brief. Copy it here,
expand it, and restate the acceptance criteria before coding.

**F09.6 · The job page for a client** (design; built 2026-10-27, no migration, D-50 appended;
the owner's pass on jobcost.dev is open, to be made signed in as a client role first;
`docs/briefs/F09.6.md`). **F09.5 · Jobs: the Sold Jobs Board** (design; built 2026-10-16,
D-49; the owner's pass is open; `docs/briefs/F09.5.md`). **F09.7 · The firm's set-up view of
a job** moved to directly after F12 on 2026-10-29 (express mode): the set-up view is designed
once, after F12 reshapes the job page.

Open passes carried: **F10** (`docs/briefs/F10.md`; Connections' held counts, the Cost page
for 2026-09 against the QuickBooks Profit and Loss by account, the unassigned list, one Ramp
card transaction on its job with its memo, 67 Elm Street as `client_pm` against the
Transaction Detail by Customer; `backend/scripts/cost_ramp_fields.py` answers Plan question 2
at the pass and is deleted at its close-out), **F09.6** (`docs/briefs/F09.6.md`; signed in as
a client role first), **F09.5** (`docs/briefs/F09.5.md`), **F09.4** (`docs/briefs/F09.4.md`),
**F09.3** (`docs/briefs/F09.3.md`), **F09.2** (`docs/briefs/F09.2.md`), **F07.3**
(`docs/briefs/F07.3.md`), **F07.2** (`docs/briefs/F07.2.md`; deploying it runs migration
0012), **F07.1** (`docs/briefs/F07.1.md`) and **F07** (the pass job by job; 67 Elm Street
passed 2026-10-01). They are taken together after F10 is deployed, on the pilot job.
Production was rebuilt from an empty database on 2026-10-15, so `rye-beach` holds no job
yet: the F07 passes need the estimates uploaded again first. D-08 is still open. Retainage
is owed as a later feature with its own decision before any tenant that holds it (D-43).

Standing state from F07 to F10: the job is the reporting unit; matching is by alias
(`job_alias`), never by name. The original contract, approved change orders, revised
contract, unapproved change orders, EAC, every billing figure and every cost figure are
computed on read (`app/domain/jobs/contract.py`, `app/domain/billing/figures.py`,
`app/domain/cost/figures.py`), never stored; the rows written are approvals and
withdrawals, assignments and clears, the pay applications with their schedule as it stood at
issue, the exceptions with their history, and the ledger lines as QuickBooks posted them
(`ledger_line`, rewritten by the normalizer, never by a person); nothing in `app/wip/` has
been touched. Billed to date is total − sales tax − fuel surcharge lines; collected to date
is cash (D-41); cost to date is Σ ledger lines on the job on in-job-cost accounts, by cost
category, in the WIP basis or memo (D-04, D-51); labor-slot lines are tied, never costed
(F11 costs labor from hours and rates). The copy and every month tie-out begin at the
history start (D-48). The interface follows D-47 and D-50; the Cost page is a firm screen.

## Discovered
Carried from `docs/briefs/F10.md` (copied unchanged 2026-10-29; nothing is dropped):
Carried from `docs/briefs/F09.6.md` and the live stub (copy unchanged from `git show HEAD:current-feature.md` at the start of the build; nothing is dropped). Closed by this brief when built: none.

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

From the design partner's working sessions, 2026-10-16 to 2026-10-29, for later briefs (express-mode calls recorded here so they are not asked again):
- **Pay application per work area (F08.4, a patch after F10)**: the application takes "percent per work area, overrides per line", nets earlier invoices per work area, and rounds per line; the hand workbook on a second pilot job went wrong twice. Owner's call made for him: F08.4 follows F10, before F11.
- **"C/O …" work areas** inside an LMN estimate: as loaded they are kept work areas and `BILLING_UNAPPROVED_CO` never fires on them. Call: a later F07.4 patch lets a person mark a kept work area as a change order on the review; names only suggest (D-01). Not now.
- **A work area with cost and 0.00 billed**: a new warn exception once cost per work area exists (F12), not in F10 (lines carry a job, not a work area).
- **The F13 revenue tie** must read every line posting to an income account from Deposit, JournalEntry, Expense, Check and Bill, not sales documents alone; Square counter sales are the unassigned income side; late fees on 8010 are outside Total Income.
- **The API's `can_manage_jobs` still includes `client_admin`**; a later brief aligns it with D-50.
- The unapproved change-order rows' billed and left-to-bill sums (F09.6) are a later API figure.
- Legacy COGS accounts outside the slot scheme (F05.1): postings to them read "Not in the chart" on the Cost page until the owner renumbers in QuickBooks.
- The tie-out's "Does not tie" has no colour (D-49); the Cost page's tick follows F09.5's tie-out line.
- "Days since last activity" reads "No activity" in a numeric column (`daysWords`).
- The Review queue and Unapproved change orders carry the shared paint only; design features after F09.7.
- A "needs attention" status colour needs its own decision line.
- `design-process.md` in the Claude Project; corrected outside the repo.

From this build (2026-10-29):
- Connections' "Could not be read" for the five cost entities re-reads every latest raw payload on each page load (Not verified); an SQL anti-join with a stored marker is the fix if it matters.
- An Item's `ExpenseAccountRef` change reaches item-based lines through the full cost run (queued by a poll that stored the Item), not through the document's own normalize task.
- The F09 Exceptions page opens a "No customer" subject on the Cost page only for a role that reads it; a `client_pm` pressing it goes nowhere. The brief gave the subject no page for a client.
- The Cost page's unassigned "Open the customer" opens the Customers page, not the row (the Customers page has no deep link to a row).
- `ROADMAP` F10's "with its receipt link" waits on Plan answer 2 at the owner's pass (`scripts/cost_ramp_fields.py`); if the link is an `Attachable`, a later patch fetches it.
- The nightly check's cost side (`sync_run.detail.cost`) names the QuickBooks account id, not the number; Connections prints it as "QuickBooks account <id>".

