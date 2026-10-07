# Monthly gross margin reconciliation: what it is, how it runs, how to read it (2026-10-07)

For the Claude session that runs in the routine owner's account (casandra@spikeball.com),
and for the controller who reads its output. Everything that could be done from outside
that account is done. This file is the runbook for the new monthly job, the checklist
wording for the close schedule, and the steps only your session can finish.

## Why this exists

On 2026-10-06 the CFO ruled that Spikeball should report gross margin on the matched view
(every cost in the month the sale was earned) and asked for the gross margin review to
become a monthly close checklist item, so that timing differences and booking errors are
caught during the close instead of weeks later. The full-year review that prompted this
(`Spikeball_2026_GM_Review.xlsx`, shared 2026-10-04) found two kinds of problem:

- Booking errors: Amazon settlement journals with missing fee lines, an advertising
  credit sitting in cost of sales, a shareholder entry credited to cost of sales, fee
  lines posted to SG&A, true-up journals never reversed, orders invoiced twice.
- Timing: Amazon fees booked on the deposit date instead of the sales month, sourcing
  fees booked on the bill date instead of the purchase-order month, retailer claims
  booked when billed instead of in the program period, refunds booked when the credit
  memo was created.

The job below re-runs those checks for one closing month and writes the result in the
same shape as the review's Summary tab: margin as booked, corrected and matched; a table
of errors with the entry to make; a table of month-end entries (the fee accrual and its
reversal); items for the controller's judgment; what was checked and found fine.

## What it does and does not do

- Reads NetSuite (production, token authentication, SuiteQL, read-only), the Amazon order
  cache that the nightly dashboard keeps on Drive, and nothing else.
- Writes one workbook to Drive and sends one email. Appends one row to a `gm_recon_log`
  tab on the Spikeball Finance Sheet. Touches no other tab.
- Never writes to NetSuite, Celigo or Amazon. Never posts, edits or reverses a journal.
  Every entry it proposes is a proposal; the controller books it or does not.
- Never edits code, never commits, never touches `.env*` files, never prints a secret.

## Where it lives

- Code: `spike/gm_recon/` in this repository (`run_recon.py` is the entry point;
  `ns_queries.py` holds every query; `rules.py` the classification rules; `workbook.py`
  the writer; `deliver.py` the Drive, email and Sheet steps). Constants in
  `spike/config/gm_recon.json`. Tests in `tests/test_gm_recon_*.py`.
- Routine prompt: `spike/gm_recon/RECON_PROMPT.md`.
- Output: Drive folder `Spikeball GM Reconciliation` (created on the first run in the
  Google identity's Drive, shared read-only with the recipients), one Google Sheet per run
  named `Spikeball_GM_Recon_<YYYY-MM>_asof_<YYYYMMDD>` (the workbook is built as .xlsx and
  converted on upload so every formula shows its value in the browser; File > Download gives
  the .xlsx back). The email carries the link and the Summary text. A run takes about three
  minutes.

## Environment variables (two new ones, both optional)

Everything the nightly uses is reused as is (NetSuite token variables, the Google OAuth
variables, `SPIKEBALL_FINANCE_SHEET_ID`, `SPIKEBALL_DASH_STATE_FILE_ID`, `SPIKEBALL_ALERT_TO`).

- `SPIKEBALL_RECON_TO`: comma-separated recipients of the monthly email and readers of the
  Drive folder. Default: the value of `SPIKEBALL_ALERT_TO`. Who else receives it (the CFO,
  the owner) is the owner's call; add addresses here when that is decided.
- `SPIKEBALL_RECON_FOLDER_ID`: the Drive folder id. The folder "Spikeball GM Reconciliation"
  already exists (id `1ESl4AkZHr_LwRlQcUbc65Pk16cV4bJWB`, created 2026-10-07 with the first
  September workbook in it); set the variable to that id so every run targets the same folder
  even if someone renames it. When unset, a run finds the folder by name, or creates it and
  prints `RECON_FOLDER_ID <id>`.

## Schedule

A second routine on the same cloud environment, named `Spikeball GM reconciliation`,
cron `0 13 2,6,10 * *` (UTC): the 2nd, 6th and 10th of each month at 07:00 MT during
MDT (06:00 MT during MST). Each run reconciles the previous calendar month as of that
morning. Three runs, because Amazon settles every fourteen days: the day-2 run gives the
first view and the accrual to book, the day-6 run picks up the settlement that lands in
the first week, and the day-10 run is the one to keep with the closed month. Later runs
replace the estimate of unsettled fees with measured fees; the workbook always says which
is which.

On demand, from an interactive session in the checkout:

```
python3 spike/gm_recon/run_recon.py --month 2026-09
```

Options: `--asof YYYY-MM-DD` (exclude later-dated transactions), `--dry-run` (build the
workbook under `spike/data/gm_recon/`, upload nothing, send nothing), `--no-email`,
`--no-upload`, `--no-log`, `--skip-amazon-cache` (no estimate of unsettled fees).

## Close checklist wording

Two lines for the "Close Schedule" tab, performer Casandra, in the Description column:

| Day | Description | NS |
|---|---|---|
| Business day 2 | Gross margin reconciliation, first pass: open the day-2 workbook from the "Spikeball GM Reconciliation" email, book or reject each row of "Errors to correct", book the Amazon fee accrual and its next-day reversal from the "Entries" sheet, answer each "For controller review" row. | accrual JE and reversal JE numbers |
| Business day 8 to 10 | Gross margin reconciliation, final: open the latest workbook, confirm the "Errors to correct" table is empty or every row is answered, confirm matched margin is the figure reported to the CFO, file the workbook with the close. | |

The first line sits before the existing "Amazon Reconciliation/True up JE" row; the second
sits before "Final COGS/ Margin Findings".

## How to read the workbook

Start with `Summary`.

1. Answer: a few sentences generated from the month's data: margin as booked, corrected
   and matched; how many errors and their total; the accrual proposed; what was fine.
2. Margin table: three columns (as booked, corrected for errors, matched to period) with
   income, cost of sales and gross margin percent. The basis line says whether matched
   includes an estimate.
3. Errors to correct: one row per error with the records, the amount, the margin effect
   in points, the entry to make and why. Each row has a confidence: `measured` (the amount
   is read from posting lines and settlement records) or `needs_review`.
4. Entries to book at month end: the Amazon fee accrual (fees on the month's sales that
   Amazon settled after month end, measured, plus an estimate for sales not yet settled),
   its reversal dated the first day of the next month, and any sourcing-fee or retailer
   claim reclass with a measured period. The `Entries` sheet lists every proposed journal
   line (date, account, debit, credit, memo) ready to key in.
5. For controller review: manual journals on income or cost accounts whose counterpart
   is an equity or SG&A account, true-ups without a reversal, Wengo bills with no
   purchase order in the memo, retailer claims whose period cannot be read from the memo.
   These need a judgment, not a formula.
6. Checked and fine: what was tested and passed (no duplicate invoices or credit memos,
   fee lines all in cost of sales, Amazon revenue within tolerance of Amazon's own order
   data for the mature days, and so on).
7. Limitations: the as-of date, the Amazon cache cutoff, the fixed FX rates, the estimate
   method.

Every number on `Summary` is a formula over the detail sheets (`Settlements`,
`Fee timing`, `Manual journals`, `Late created`, `Wengo`, `Retailer claims`, `Refunds`,
`Duplicates`, `PnL`), so any figure can be traced to its rows; the Errors and Entries tables
also carry an Income effect and a COGS effect column, which is what the margin table sums.
`Notes` holds the basis, the rules, the fixed FX rates and the run details. `_checks` is
the job's own tie-out of every Summary formula against its Python figure.

Row ids: `E1-n` settlement journals with missing fee lines, `E4-n` fee lines on an expense
account outside cost of sales, `D1` duplicates, `T1` the Amazon fee accrual, `T3-n` Wengo
sourcing fees, `T4-n` retailer claims with a period read from the memo, `R1-n` manual
journals for review, `R-T2` legacy refund credit memos whose refund posted in another month,
`R-D1` credit memos that repeat on one order with different amounts, `R-E1` a settlement
journal that matched no settlement.

Two things the first runs will say: Amazon revenue completeness is "not measurable" because
the nightly's Drive state carries order totals but no item prices (the check switches on by
itself when item files appear); and the Wengo per-unit rate observed on the latest bill can
differ from the configured rate, in which case the T3 row states both and the controller
confirms before booking.

## The accrual, in one paragraph

Amazon pays every fourteen days and the settlement journal books fees on the deposit
date, so part of each month's fees lands in the next month. The May 2026 precedent
(JE5604 dated 5/31, debit 50100000 cost of sales, credit 20106400 Misc Accrued; JE5605
dated 6/1 reversing it) is the pattern the job proposes: at month end, debit the fee cost
account and credit 20106400 for the fees on that month's sales that settle after month
end; reverse it the next day. The measured part is read from the settlement rows that
have already landed; the estimate for sales not yet settled is the count of those orders
times the median fee per settled order in the same marketplace that month, and the
workbook labels it as an estimate. The fee cost account follows the month's settlement
journals (50300000 Merchant Account Fees since September; 50100000 before).

## Steps only your session can do

1. Merge the branch into this repository's default branch.

   ```
   cd /home/user/spikeball        # or wherever your checkout of this repository lives
   git fetch mirror feat/repo-source-routine
   git merge --no-ff mirror/feat/repo-source-routine
   ```

   No conflicts are expected: STATUS.md, FORECAST-STATUS.md and HANDOFF-GAPS.md are yours
   and untouched. Files that change: everything under `spike/gm_recon/`,
   `spike/config/gm_recon.json`, `tests/test_gm_recon_*.py`, `requirements.txt`
   (adds `openpyxl`), `.claude/settings.json` (two allowed commands), `OPERATIONS.md`.
   The mirror remote is the one you already have; the owner gives you its URL if it is
   missing.

2. Run the tests from the merged checkout.

   ```
   python3 -m pip install -r requirements.txt
   python3 -m pip install -r requirements-dev.txt
   python3 -m pytest tests -q
   ```

   Expected counts are in the paste that accompanies this file.

3. Push the merge to the default branch.

4. Set `SPIKEBALL_RECON_FOLDER_ID` (value above) in the cloud environment's variables, then
   run the September reconciliation once, interactively, so the first email arrives and the
   folder is shared with the recipient:

   ```
   python3 spike/gm_recon/run_recon.py --month 2026-09
   ```

   Expect a last line `RECON_OK <link>`.

5. Create the routine: name `Spikeball GM reconciliation`, same cloud environment
   (`Spikeball Finance`), cron `0 13 2,6,10 * *`, prompt = the contents of
   `spike/gm_recon/RECON_PROMPT.md`, repository source = this repository's default branch,
   tools Bash only.

6. Report: the merge commit, the test counts, the `RECON_OK` line, the folder id, the
   routine's trigger id.

## If something looks off

- `RECON_FAIL env not loaded`: a NetSuite or Google variable is missing on the cloud
  environment. Names only are printed.
- `RECON_FAIL netsuite ...`: NetSuite refused the query or timed out. Nothing was written.
  The next scheduled run retries on its own.
- `RECON_PARTIAL_OK ...`: the workbook was built but the upload, the email or the log row
  failed; the reason names which. The file is under `spike/data/gm_recon/` in that
  session's sandbox and is lost when the session ends, so re-run rather than recover.
- An error row you disagree with: the `Why` column states the rule that fired and the
  detail sheet holds the rows. Reject it in your own notes; the job does not learn, it
  re-reports until the ledger changes.
- The estimate looks large: the `Fee timing` sheet lists the unsettled orders and the
  median fee used. The day-6 and day-10 runs shrink it as settlements land.
- Amazon revenue completeness says "not measurable": the Drive order cache does not cover
  the month yet, or the nightly has not run. It is informational and never blocks the run.

## Do not

- Do not book an estimate as if it were measured; the Entries sheet separates the two.
- Do not add `gm_recon_log` rows by hand, and never write to `run_log`; the nightly's gate
  reads `run_log` to decide whether to run.
- Do not change the FX rates, the account map or the fee-bearing item prefixes in
  `spike/config/gm_recon.json` without the owner; they match the review the CFO accepted.

## Open decisions (owner's call, not required to run)

- Who receives the monthly email beyond the controller.
- Whether the accrual stays on 20106400 Misc Accrued (the May precedent) or moves to an
  Amazon-specific accrued-fees account.
- Whether Wengo sourcing fees are a period cost (today) or part of inventory cost.
- Retailer claim periods stay a controller judgment unless the memo states the period.
