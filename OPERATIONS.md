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
  `run_log` tab has one row per run.
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
guaranteed nightly run; see `CUTOVER.md` for changing it). The routine above's environment does not set
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
labels unchanged since the prior run; closed months unchanged beyond 0.5% unless a known adjustment is
listed; the SKU method proof. A failing run writes nothing, leaves the previous night's page and Sheet in
place, and emails the alert address.

## When something looks wrong

- Page footer says "Data as of <date>, refresh overdue" or "Data checks failed": the previous night's run
  did not publish. The alert email names the failed check. Open the routine's run at
  https://claude.ai/code/routines to read the log. Most causes: a NetSuite credential or role change, an
  Amazon token expiry, a renamed channel or region picklist value (deliberately blocks publishing until
  acknowledged), or Google API access revoked.
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
placed. The dashboard page (Artifact) shows all six, labeled actual-only.

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
