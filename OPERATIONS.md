# Spikeball Finance dashboard: operations note

Audience: Spikeball finance team (owner of record: Matt Cohen, mcohen@spikeball.com). Written 2026-08-26
(MT). Everything below runs on Spikeball-owned Google assets and Anthropic's Claude Code cloud; nothing
runs on a private server.

## What refreshes, when

Every night at 02:00 MST / 03:00 MDT (09:00 UTC) a scheduled Claude Code routine named "Spikeball Finance
nightly refresh" runs the read-only extract against NetSuite (production, account 4201313) and the Amazon
Selling Partner API, runs the data checks, and, only if every check passes, writes the snapshot to:

- Google Sheet "Spikeball Finance Data" (mcohen@spikeball.com's Drive):
  https://docs.google.com/spreadsheets/d/1aLGh8fYVKGe-T08-1tZe1eTmWwmBRiPtS12ChmnjMHs
  One tab per dataset; the `meta` tab shows the as-of date, the pull time and every check result; the
  `run_log` tab has one row per run; the `alert_log` tab has one row per failure alert, whether it was
  emailed (`sent`) or held back as a repeat (`suppressed`), see "Checks that gate every refresh".
- BigQuery dataset `spikeball_finance` in the Google Cloud project `spikeball-coding-automation`
  (Spikeball-owned). Same tables as the Sheet plus Looker-ready views.
- The dashboard page (Claude Artifact, public unlisted link):
  https://claude.ai/code/artifact/dbe5fcb5-fd23-4f98-b227-c4412bffbbd0
- Looker Studio report on BigQuery, owned by mcohen@spikeball.com:
  https://lookerstudio.google.com/reporting/0d761565-7225-468a-ae65-8afd5341b332
  (also in the Sheet's `meta` tab as `looker_report_url`). Twelve BigQuery views feed it; it refreshes from the
  nightly load. Sharing is done from the report's Share button (Viewer access to the CFO and CEO addresses).
  The report's charts were placed by the build session's editor automation (`scripts/looker_build.py`); to
  change a chart, edit it in Looker Studio directly.

Period rule: month-to-date and year-to-date run through yesterday's close, Mountain Time. The open month is
labeled provisional because NetSuite postings (Amazon settlements, month-end COGS reclasses) can restate it.

### The gated routine: month range selector and on-demand refresh

A second routine, `Spikeball Finance refresh`, publishes its own page with two controls the routine above
does not have: a month range selector (pick any start and end month from the trailing 13 months, not just
the fixed MTD / YTD / 13-month buttons) and a "Request data refresh" link that queues an on-demand refresh
instead of waiting for the nightly run.

This routine runs on cron `0 0,10,13-23 * * *` UTC -- during MDT: hourly from 07:00 through 18:00 MT, plus the 04:00 MT nightly slot;
during MST: one hour earlier than each of those, hourly 06:00 through 17:00 MT, plus 03:00 MT. Most of those
hourly checks find nothing to do and exit without touching the Sheet, BigQuery, or the page; a full refresh
happens at the nightly hour, after an on-demand request, or after a 20-hour gap since the last success. Its
nightly run sits at 03:00 MST / 04:00 MDT rather than 02:00 MST / 03:00 MDT, one hour later than the routine
above, so the two never write the same Sheet, BigQuery dataset, or Drive state file in the same hour. See
`CUTOVER.md` for retiring the routine above and moving this one's nightly run to its final hour.

The on-demand request: clicking "Request data refresh" on this routine's page writes a row to the
`refresh_requests` tab on the Sheet (columns: `requested_at_utc`, `requested_at_mt`, `source`,
`user_agent`, `status`). The next hourly check (07:00 through 18:00 MT during MDT) honors it and rewrites that row's
`status` to `honored <timestamp>`. A request made outside that window, or within 10 minutes of the last
one, waits for the next check or shows a message that one is already queued rather than adding a second
row. A queued row older than the routine's last successful run is marked `superseded <timestamp>` instead
of `honored <timestamp>` the next time the gate runs, since a more recent successful pull already covers
it. `run_log` gains two columns for this routine's runs: `trigger` (`nightly` or `request`) and
`request_row` (which `refresh_requests` row number(s) a request-triggered run honored or superseded, blank
otherwise). The Sheet's `run_log` and `refresh_requests` tabs are the day-to-day health check for this
routine -- they show what actually happened on every run, not just how long a session took.

The request endpoint is an Apps Script web app owned by the Google account whose refresh token the
routine uses (the identity in RUNBOOK-google-identity.md). Its source is
`scripts/refresh_request_webapp/Code.gs`; a change there goes live only after `clasp push` and
`clasp deploy -i <deployment id>` as that account (the deployment id is in that folder's README). The
confirmation page derives its times from `SCHEDULE_UTC_HOURS`, which must equal the routine's cron hours.

### Environment variables and where the code runs from

The gated routine runs from a checkout of this project's GitHub repository, attached to its cloud
environment as a repository source; there is no code to download and nothing to publish separately. Every
credential the routine needs -- NetSuite, Google, and Amazon -- is a plain environment variable set on that
same cloud environment; there is no secrets manager or separate secrets service anywhere in the path. That
environment carries `SPIKEBALL_DASH_FEATURES=range_selector,refresh_control` (turns the two controls above
on for this routine's page only) and `SPIKEBALL_NIGHTLY_SLOT_UTC` (the UTC hour this routine treats as its
guaranteed nightly run; see `CUTOVER.md` for changing it). `SPIKEBALL_ALERT_DEDUPE_HOURS` is optional: the
number of hours during which a repeat of the same failure does not email again (default 24; 0 turns the
suppression off so every failing run emails). The routine above's environment does not set
`SPIKEBALL_DASH_FEATURES`, so its page stays exactly as it is today even though both routines run the same
code. The commands this routine's prompt runs are declared as allowed in the repository's own
`.claude/settings.json`, loaded automatically when the routine's session starts.

### Cutover note

Both routines are safe to run in parallel for as long as needed. See `CUTOVER.md` for when and how to
disable the routine above and move the gated routine's nightly run to its final hour once you're ready to
standardize on one dashboard.

## What the numbers are

- Revenue, COGS and gross margin come from the general ledger at transaction-line grain, in USD, net of
  discounts, refunds and returns (all inside the Income account type). Amazon selling fees post to COGS in
  this ledger, so Amazon margin is margin after Amazon fees.
- Channel roll-up: Amazon; Wholesale = Wholesale/Retail + Major Retail + SMB & Tradeshows & Events;
  Spikeball.com; Other B2B = Ambassadors, PE/Rec, Sports Development, Corporate, Other ECommerce Platforms,
  Fwango; Unassigned = lines with no channel tag (kept so the table foots to the ledger total). Corporate and
  Fwango margins are flagged rather than shown because their COGS and revenue are not coded consistently.
- Spikeball.com regions: US vs Other. Spreetail and Pattern are resellers, not regions; any revenue tagged
  to them appears as a data-quality flag.
- SKU sales count an item line as a sale only when it carries a revenue posting; component consumption lines
  of assembled sets are excluded. Kit-type SKUs have no on-hand record in NetSuite and are labeled "kit: see
  components".
- Amazon marketplaces outside NetSuite (MX, BR, IT, NL, PL, IE, SE, BE, TR, SA) come from the Amazon Orders
  API in native currency and are never summed across currencies. Amazon.de/.fr/.es and AU are not sold on.
- Company totals reconcile to the NetSuite Income Statement on closed months; the only known difference is
  the FX re-translation of foreign-currency COGS inside the Income Statement (measured 4,489.11 for Jan-Jul
  2026), recomputed per window.

## Checks that gate every refresh

Channel sum equals ledger total (within 0.01, re-run once if a posting lands mid-check); inventory replica
equals NetSuite item value; every section returned rows; the as-of date is yesterday MT; channel and region
labels unchanged since the prior run; closed months unchanged beyond 0.5% unless the movement is fully
explained by transactions created since the prior run (late postings into a month NetSuite still has open)
or a known adjustment is listed; the SKU method proof. A failing run writes nothing, leaves the previous night's page and Sheet in
place, and emails the alert address.

One email per distinct failing reason per 24 hours. The gated routine's hourly check keeps retrying a
failed day, so without this rule one bad night sent the same email every hour (five copies on 2026-10-07).
Now the first run that hits a reason emails; a later run inside the window that fails for the same reason
does not email again and instead adds a `suppressed` row to the Sheet's `alert_log` tab (columns:
`sent_at_utc`, `sent_at_mt`, `verdict`, `signature`, `action`; the emailed run is the `sent` row above it).
A different reason emails right away: another check failing, the same check on a different month, or
the closed-months check plus the channel foot together count as different reasons; the changing numbers
and timestamps inside an otherwise identical message do not. The window is the environment variable
`SPIKEBALL_ALERT_DEDUPE_HOURS` (default 24; 0 disables the suppression so every failing run emails). If
the `alert_log` tab cannot be read or written for any reason, the email is sent as before; the tab is
created on first use.

## When something looks wrong

- Page footer says "Data as of <date>, refresh overdue" or "Data checks failed": the previous night's run
  did not publish. The alert email names the failed check. Open the routine's run at
  https://claude.ai/code/routines to read the log. Most causes: a NetSuite credential or role change, an
  Amazon token expiry, a renamed channel or region picklist value (deliberately blocks publishing until
  acknowledged), or Google API access revoked. The email arrives once per distinct reason per 24 hours
  (`SPIKEBALL_ALERT_DEDUPE_HOURS`); every later run inside that window that fails the same way is a
  `suppressed` row on the Sheet's `alert_log` tab, so a quiet inbox after the first email does not mean the
  failure stopped. Check `alert_log` and `run_log` on the Sheet for what each hourly run did. A new reason
  emails immediately even inside the window.
- The alert names `g_closed_months_stable` with a residual: a prior month's revenue or transaction count
  moved by more than the transactions created since the last run can account for. The alert text gives, per
  month, the baseline, the current figure, the part explained by new transactions and the residual
  (revenue and count). A residual means history changed some other way: an older transaction was edited or
  deleted, or the extract returned a wrong figure. Nothing is published and the baseline does not advance,
  so every night repeats the failure until the cause is fixed. Find the transactions dated in that month
  whose last-modified date is after the last good run (or that no longer exist), correct or accept them, and
  rerun. Late postings dated into an older month (for example refund credit memos created after month end)
  are explained automatically and do not fail.
  How "created since the last run" is measured: transactions of that month whose `createddate` is after the
  last good run's income read (the `income_queried_at` stored in its state, minus 2 minutes of clock skew)
  and up to this run's income read. The first run after this rule shipped has a state without that time and
  uses its `pulled_at_mt` instead; that run passes only on a residual of exactly zero. NetSuite SuiteQL
  renders and compares `createddate` on the America/Chicago clock for this integration user (Mountain + 1
  hour, DST included), so the window is converted to that clock. Evidence, read-only probes 2026-10-03: the
  REST record `createdDate` (UTC) converted to America/Chicago equals the SuiteQL value for all 8 dates
  tested, which span both sides of the March and November changes (2026-03-05 offset 6h from UTC,
  2026-03-12 offset 5h, 2025-10-28 offset 5h, 2025-11-05 offset 6h); the newest transaction read 08:49
  while the Mountain wall clock was 07:58; a literal filter of 08:49:15 to 08:49:17 matched that
  transaction (1 row) and the same filter one hour earlier matched none. Mountain and Pacific were both
  tried first and are wrong.
- A channel or region was renamed in NetSuite: expected to block once. Acknowledge by running the extract
  with the previous state and confirming the new labels; the next run records the new names.
- A new channel id appears in NetSuite: add it to `spike/config/rollups.json` in the code repository under
  the right group; nothing else changes.
- Amazon AOV: the Orders API infrastructure is built and dormant. Set `features.amazon_aov` to true in
  `spike/config/rollups.json` to show the Amazon order count and AOV tiles.
- Alert address: `SPIKEBALL_ALERT_TO`, an environment variable on the gated routine's cloud environment
  (`casandra@spikeball.com` once UNBLOCK.md step 2 is complete; until then it is the previous address).
- Google access: the gated routine's refresh runs as mcohen@spikeball.com through a one-time consent, for
  now (see `RUNBOOK-google-identity.md` for moving this to Casandra later). If that account's
  password or security settings change and the token is revoked, re-run `spike/routine/google_consent.py`
  and approve once.

## Deploying a code change

Before opening a pull request, run the sandbox import guard so a local-only dependency (like openpyxl) can
never crash the nightly again: `python spike/routine/sandbox_import_check.py` (must print
SANDBOX_IMPORT_OK). The gated routine runs from a checkout of this project's GitHub repository, attached to
its cloud environment as a repository source -- it does not download a bundle and there is nothing to
publish separately. A code change takes effect once it's merged to the repository's default branch: the
routine's next session starts from a fresh checkout of that branch automatically, on its own schedule,
with no manual step in between.

## Manual refresh

From a checkout of the repository, with this environment's variables loaded (they are plain environment
variables, not a secrets service, so any shell that has them exported works):
`python spike/routine/run_nightly.py` (prints `NIGHTLY_OK` or `NIGHTLY_FAIL <reason>`), then republish
`design/mockup/dashboard.artifact.html` to the artifact URL above. `--skip-amazon` skips the Amazon
Orders API leg; `--diagnose` only tests network reachability.

## v2 actual-only sections (added 2026-08-27)

Six new sections now refresh alongside the v1 dashboard, each tied to the cent against the CFO's own
workbook for closed months (the only differences are ledger postings entered after his workbook export):

- Monthly P&L by GL account (income statement), with gross and net revenue split by channel.
- EBITDA and Adjusted EBITDA by month (Net Income + bank interest + other interest + D&A, then add back
  GL 50200000 Inventory Adjustments).
- Balance sheet by account, cash by bank account, and the indirect cash flow statement.
- Working capital: AR and AP aging by bucket, open sales and purchase orders, item unit cost.

Where they appear: the Google Sheet (one tab per section) and BigQuery (one table + a Looker view per
section) refresh every night with the rest. The Looker report gains a page per section once its charts are
placed. The dashboard page (Artifact) shows all six, labeled actual-only. The Demand Plan tab and its
Sheet and BigQuery outputs (demand vs actual units, cost coverage) still refresh and publish every night;
the dashboard page has no Demand vs actuals section (plan-versus-actual on the page is gross dollars by
channel and in total, the blocks of the CFO's revenue summary, nothing SKU-level).

Balance sheet accuracy (important, updated 2026-08-28): the balance sheet is built from a nightly snapshot
of NetSuite's own native account balances, pulled over the API (`spike/extract_v2_bs_snapshot.py`) -- the
same read-only credentials the rest of the pipeline already uses. It does not need a NetSuite browser
session, a manual report pull, or anyone's two-factor login; nothing about refreshing this section requires
a human to sign in anywhere. An earlier version of this dashboard anchored to a trusted month-end and
rolled forward by ledger activity because a raw ledger sum did not reproduce this company's bank balances
(processor and line-of-credit accounts); that method is retired for months the snapshot method covers, and
is kept only for months before snapshotting began. Closed months tie to the cent against the CFO's own
workbook, the same as the other v2 sections; the current open month can still move as new postings land,
the same as every other open-month figure on this dashboard.

Cash flow note: the statement always foots to the actual bank movement. Shareholder distributions and other
non-earnings equity movements, which the CFO's own workbook method does not itemize, appear on one
"Distributions & Other Equity" line so the statement reconciles and the omission is visible.

## Gross revenue and the plan line (added 2026-09-29)

Every revenue figure on the dashboard is now GROSS revenue: gross sales plus tournaments plus shipping
(accounts 40100000, 40104000, 40105000), before discounts, refunds and returns. This is the same basis the
CFO uses in his revenue summary, and closed months tie to his channel actuals to the cent. Gross profit and
margin keep their net basis (revenue net of discounts, refunds and returns, less COGS) and are labeled "on
net revenue". Orders, AOV and the returns rate are unchanged. SKU revenue is line-level and is not
comparable to the gross totals; the page says so where it appears.

The monthly "Revenue by channel" chart draws a plan line from the "Revenue Plan" tab of the Spikeball
Finance Sheet: the 2026 Original Plan by channel. The Plan select next to the chart switches between the
total and one channel; the table under the chart shows plan, actual, variance and variance percent by
month with quarter, year-to-date and full-year rows. The open month is month to date against its
full-month plan and is tagged MTD. Months after the as-of month show the plan only. The chart opens on the
plan's calendar year (January through December); the start and end month selects still change it.

Editing the plan: open the "Revenue Plan" tab, keep the header row (Channel, Series, then one YYYY-MM
column per month) and type gross dollars into the cells. Channel is Amazon, Spikeball.com or Wholesale
(Other B2B is accepted). Series is Plan for the original plan. A row with Channel Total and Series Forecast
draws as a second, dotted line in the Total view of the chart and fills the Forecast, Var vs forecast and
Var vs forecast % columns of the table; a Forecast row for a channel draws in that channel's view. The Fcst
vs plan and Fcst vs plan % columns are forecast minus plan, filled for every month that has both (months
after the as-of month included); quarter, year-to-date and full-year rows carry the same difference over the
months they cover. Where no
Forecast row covers the selected channel, the legend and the table caption say so and the forecast cells
are blank. Blank means no plan or forecast for that month. The nightly reads
the tab and never writes it. A row it cannot read is dropped and listed on the revenue_plan_meta tabs; if
the header is missing the whole tab is treated as unavailable, the last good copy is used and the page
says "Plan is stale"; with no good copy at all the plan controls hide and the page says "Plan not
available for this run". A plan problem never blocks the rest of the refresh (checks q and r are
informational).

## Monthly gross margin reconciliation (added 2026-10-07)

A second routine on the same cloud environment, "Spikeball GM reconciliation", runs on the 2nd, 6th and
10th of each month at 07:00 MT (06:00 MT in winter) and reconciles the previous calendar month. It is the
monthly close checklist item the CFO asked for on 2026-10-06 after the full-year gross margin review:
margin as booked, corrected for booking errors, and matched to the period each cost was earned, with the
entries to make. It reads NetSuite (read-only) and the Amazon order cache the nightly keeps on Drive, and
writes exactly three things: one workbook to the Drive folder "Spikeball GM Reconciliation", one email to
the addresses in `SPIKEBALL_RECON_TO` (default `SPIKEBALL_ALERT_TO`), and one row on the Sheet tab
`gm_recon_log`. It never writes to NetSuite and never touches `run_log` or any tab the nightly writes.

What it checks each month: Amazon settlement journals with missing or partial fee lines (and settlements
with no journal), fee lines posted outside cost of sales, manual journals on income or cost accounts whose
counterpart is an equity or SG&A account, true-ups without a reversal, duplicate Amazon invoices or
credit memos, entries created after month end, Amazon fees by sales month with the month-end accrual to
book (measured where the settlement has landed, an estimate for sales not yet settled, labelled as such),
Wengo sourcing fees by purchase-order month, retailer claims by program period where the memo states it,
refunds by the month Amazon posted them, and Amazon revenue against Amazon's own order data for the
mature days. Every Summary figure is a formula over the detail sheets.

Reading it and running it on demand: `spike/gm_recon/GM-RECON.md`. The routine prompt is
`spike/gm_recon/RECON_PROMPT.md`. Three runs a month by design: the day-2 run gives the first view and the
accrual to book, the later runs replace the estimate with measured fees as Amazon settlements land; the
day-10 workbook is the one to file with the close.
