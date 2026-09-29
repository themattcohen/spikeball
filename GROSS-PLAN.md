# Gross revenue and the 2026 plan line: what changed, what to do (2026-09-29)

For the Claude session that runs in the routine owner's account (casandra@spikeball.com).
Everything that could be done from outside that account is done. This file lists what
changed, what is already live, and the steps only your session can finish.

## What changed

The CFO asked for two things: revenue on the dashboard as gross instead of net, and a
line on the monthly bar chart that tracks revenue against plan, by channel if possible.
Both are built and tested.

- Every revenue figure on the page is now gross revenue: gross sales plus tournaments
  plus shipping (accounts 40100000, 40104000, 40105000), before discounts, refunds and
  returns. Closed months tie to the cent to the CFO's own channel actuals. Gross profit
  and margin keep the net basis and their labels say "on net revenue".
- The "Revenue by channel" chart draws a plan line from a new protected Sheet tab,
  "Revenue Plan" (the 2026 Original Plan by channel). A Plan select next to the chart
  switches between the total and one channel; a plan-vs-actual table under the chart
  shows month, quarter, year-to-date and full-year rows. The chart opens on January
  through December of the plan year; the month selects still work.
- New output keys: `revenue_plan_meta`, `revenue_plan_month`, `plan_vs_actual_month`.
  Existing rows gained `gross_revenue` (and `gross_revenue_py`) in `rollup_by_month`,
  `pnl_by_channel_month`, `rollup_by_period` and `pnl_by_channel_period`.
- New checks q (gross ties to the account-grain build) and r (plan tab readable). Both
  are informational: a plan problem can never change the nightly's exit code or block
  any other section. The plan tab is on the protected list; the nightly never writes it.
- Files: `spike/revenue_plan.py` (new reader), `spike/extract.py`, `spike/checks_v2.py`,
  `spike/routine/run_nightly.py` (a read step before the extract), `spike/publish_sheet.py`,
  `spike/publish_bq.py` (two new views), `spike/config/rollups.json`,
  `design/mockup/template.html`, `design/mockup/build.py`, `spike/CONTRACT.md`,
  `OPERATIONS.md`, tests `tests/test_revenue_plan.py`, `tests/test_extract_gross.py`,
  and an extended `tests/test_dashboard_range.py`. In this repository the suite collects
  156 tests: 123 pass and the 33 browser DOM tests skip without a real
  `spike/data/latest.json`. (An earlier version of this note said 209; that was the
  maintainer's full suite, which includes tests that do not ship here.)

## Already live (done 2026-09-29, Mountain Time)

- The "Revenue Plan" tab exists on the Spikeball Finance Sheet (sheetId 2126203579),
  seeded from the CFO's "2026 Revenue Summary_Source of Truth_YTD through August"
  workbook, hidden tab "2026 Original Plan": Amazon, Spikeball.com and Wholesale, one
  Plan row each, January through December 2026. Full-year plan 18,258,297.38.
- A manual read-only run at 11:18 MT (as-of 2026-09-28, all checks pass, gross tie on
  all 108 channel-months) published the Sheet and BigQuery with the new tabs, tables and
  views (`v_revenue_plan_month`, `v_plan_vs_actual_month`). Its run_log row says
  trigger "manual".
- A preview of the page built from this run is published privately by the person who
  handed this over; ask the owner for the link if you want to compare against your
  routine's page after the merge.
- The code is on branch `feat/repo-source-routine` of the public mirror repository. The
  owner will give you the mirror URL; it is deliberately not written here.

## Steps only your session can do

1. Merge the branch into this repository's default branch.

   ```
   cd /home/user/dash        # or wherever your checkout of this repository lives
   git remote add mirror <mirror URL from the owner>   # skip if it already exists
   git fetch mirror feat/repo-source-routine
   git merge --no-ff mirror/feat/repo-source-routine
   ```

   No conflicts are expected: STATUS.md and HANDOFF-GAPS.md are yours and untouched.
   Only the files listed above change.

2. Run the tests from the merged checkout.

   ```
   pip install -r requirements-dev.txt
   python3 -m patchright install chromium
   python3 -m pytest tests -q
   ```

   The browser tests need a real `spike/data/latest.json`; they skip cleanly without one.
   Expected: 156 collected, 123 passed, 33 skipped.

3. Push the merge to the default branch. The routine checks the repository out fresh on
   every run, so no other deploy step exists.

4. Get a run: wait for the next scheduled fire, or queue a request from the "Request data
   refresh" control on the page (the gate honors it at the next check hour).

5. Verify the run (all from the Sheet and the page, not the routine console):
   - `run_log`: newest row has all_pass true.
   - `revenue_plan_meta_summary`: valid true, stale false, row_count 3, error empty.
   - `plan_vs_actual_month`: 72 rows (12 months x 5 rollup keys plus a total row each).
   - The page: the hero reads "Year to date sales" with no "(net)" suffix; the chart legend
     has "Plan" and a solid line with hollow markers; the Plan select lists Total, Amazon,
     Spikeball.com, Wholesale; a "Plan vs actual" table sits under the chart; the open
     month row is tagged MTD; margin labels say "on net revenue".
   - Until step 3 lands, the nightly runs the old code: it keeps publishing net figures,
     leaves the new tabs in place but stale, and does not touch the Revenue Plan tab.

6. Tell the CFO how the plan is edited (or point him at OPERATIONS.md, "Gross revenue and
   the plan line"): open the "Revenue Plan" tab, keep the header row (Channel, Series,
   then one YYYY-MM column per month), type gross dollars. Channel is Amazon,
   Spikeball.com or Wholesale; Series is Plan. A second row per channel with Series
   "Forecast" is read and stored for a later reforecast view but does not draw yet. Blank
   means no plan that month. Nothing writes to that tab but him.

## If something looks off

- "Plan not available for this run" on the page: the tab could not be read and no earlier
  good copy exists. Check the header row spelling and the `revenue_plan_meta_summary`
  tab's error column.
- "Plan is stale": this run's read failed and the last good copy was used. Same check.
- A row missing from the plan: it was dropped. `revenue_plan_meta_dropped_rows` names the
  row and the reason (unknown channel, unreadable amount, duplicate channel and series).
- Numbers differ from the CFO's own sheet: (a) his July cells omit the shipping component,
  so July on the dashboard is slightly higher by design; (b) postings entered after his
  export; (c) the dashboard total includes the Unassigned channel, his sheet has no such
  line; (d) the open month is month to date against a full-month plan.
- The plan line follows the chart's month selects; if the range excludes a month, that
  month's plan point is not drawn but the table's YTD and full-year rows still cover the
  whole year.

## Do not

- Never add the "Revenue Plan" tab to any write set, and never remove it from
  `protected_tabs` in `spike/config/rollups.json`.
- Never fold checks q or r into `all_pass` or `v2_pass`.
- Do not edit the plan numbers in code. The tab is the single source; the pipeline only
  reads what the CFO typed.
