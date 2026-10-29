# Cost fixture (F10)

Constructed raw payloads in the sandbox's shape (S-01; `tests/fixtures/qbo_sandbox/`),
keyed to the F07 job fixture: the 67 Elm Street estimate (`EST6115758`, `ELM_ID`) linked to
the synthetic project `201`, the Turley estimate (`EST6120638`) linked to project `202`
as a **Landscaping** job, and a pool job (D-30) linked to project `205`. The vendors
(`7001` to `7003`), the QuickBooks account ids (`301` to `309`, whose `AcctNum` match the
chart in `../chart_of_accounts.csv`), the items (`601` with expense account 5230, `602`
with none) and **every amount are the fixture's, not Rye Beach's**. The files are the
latest raw version of each record as the sync would hold it; three more files are later
versions and a delete stub.

| File | What |
|---|---|
| `Account.json`, `Vendor.json`, `Item.json` | The reference records the lines name |
| `Bill.json` | 98892 (two lines, 5240 and 5230, project 201); 98893 (one line, 5240, **no `CustomerRef`**); 98890 (the pool bill: seven lines on 5135 to project 205); 98894 (two item-based lines, project 201: item 601 → 5230, item 602 → no account); 98895 (one line, 5240, project 201; deleted later); 98908 (one line, 5240, project 202, the Landscaping job) |
| `VendorCredit.json` | 7701 against 98892: one line, 5240, project 201 |
| `Purchase.json` | 5001 (`CreditCard`, 5230, project 201); 5002 (`Check`, **`Credit = true`**, 5240, project 201) |
| `JournalEntry.json` | 9001, the payroll shape: debits to 5110 and 5210, credit to 2110 |
| `Deposit.json` | 3301: one `DepositLineDetail` line to 5240, entity customer 201 (a vendor refund banked) |
| `Bill_98893_assigned.json` | 98893 after a person's edit in QuickBooks: the line carries project 201 |
| `Bill_98892_one_line.json` | 98892 returned with one line fewer |
| `cdc_delete_stub.json` | The CDC delete stub for 98895 |

## The oracle, by hand (`cost_expected.csv`)

Signs (the brief): a Bill line is positive; a Vendor Credit line negative; a Purchase line
positive, negative when `Credit = true`; a Journal Entry line positive when `Debit`,
negative when `Credit`; a Deposit line negative. Lines to balance-sheet accounts (2000,
2110, 1010, 2510) are not stored. The state the oracle describes: every file's latest
version applied, 98895 not yet deleted, 98893 not yet edited, the three jobs linked.

**Ledger total per month and account** (every stored line, by document date):

| Month | Account | Lines | Total |
|---|---|---|---|
| 2026-09 | 5240 | 98892 L1 +1,250.00; 98893 +2,000.00; VC 7701 −150.00; Purchase 5002 −400.00 | **2,700.00** |
| 2026-09 | 5230 | 98892 L2 +480.50; Purchase 5001 +315.75 | **796.25** |
| 2026-09 | 5135 | 98890: 3,100.00 + 1,900.00 + 1,150.00 + 860.00 + 610.00 + 300.00 + 200.00 | **8,120.00** |
| 2026-09 | 5110 | JE 9001 L0 +12,500.00 | **12,500.00** |
| 2026-09 | 5210 | JE 9001 L1 +9,800.00 | **9,800.00** |
| 2026-10 | 5240 | Deposit 3301 −275.00; 98895 +500.00; 98908 +4,000.00 | **4,225.00** |
| 2026-10 | 5230 | 98894 L1 (item 601) +640.00 | **640.00** |
| 2026-10 | No account | 98894 L2 (item 602, no expense account) +90.00 | **90.00** |

**The four parts** (confirmed `account_map` from `../account_map_expected.csv`: every 5nxx
slot account is job cost; 5110 and 5210 are the Labor slot; the WIP basis is D-05's,
slots 10, 20, 30, 35, 40, 50, 60, 70, 90):

- 5240, 2026-09: on jobs 1,250.00 − 150.00 − 400.00 = **700.00** (67 Elm Street);
  unassigned **2,000.00** (98893); 700.00 + 2,000.00 = 2,700.00.
- 5230, 2026-09: on jobs **796.25**. 5135, 2026-09: on jobs **8,120.00** (the pool job).
- 5110 and 5210, 2026-09: labor in the ledger **12,500.00** and **9,800.00**; on no job
  and never unassigned.
- 5240, 2026-10: on jobs −275.00 + 500.00 + 4,000.00 = **4,225.00** (67 Elm Street
  225.00, Turley 4,000.00). 5230, 2026-10: on jobs **640.00**.
- No account, 2026-10: **90.00** outside job cost (the line has a job but no account to
  map); it is listed, never dropped.
- Memo (owned equipment, vehicles, fuel, equipment maintenance): **0.00** everywhere; no
  fixture line is on a 5n45, 5n47, 5n55 or 5n65 account.

**Cost to date on 67 Elm Street** (job 201, in the WIP basis, as of any day on or after
2026-10-09):

- Subcontractors (slot 40, account 5240): 1,250.00 − 150.00 − 400.00 − 275.00 + 500.00 =
  **925.00**.
- Materials (slot 30, account 5230): 480.50 + 315.75 + 640.00 = **1,436.25**.
- In the WIP basis: 925.00 + 1,436.25 = **2,361.25**. Memo: **0.00**. The 90.00 line on
  no account is in no category.

After the CDC delete of 98895: Subcontractors **425.00**, in basis **1,861.25**. After the
edit of 98893 (project 201 on its line): Subcontractors +2,000.00, the `COST_UNASSIGNED`
sentence resolves. After 98892 returns with one line fewer: Materials −480.50.

**The pool job** (205): Supplies (slot 35) **8,120.00**, seven lines, once a pool job is
linked to the row; unassigned until then. **The Turley job** (202, Landscaping):
Subcontractors **4,000.00**, and `COST_DIVISION_MISMATCH`: "Bill A-1052 posts 4,000.00 to
5240 Subcontractors - EX (Excavation) on a Landscaping job."
