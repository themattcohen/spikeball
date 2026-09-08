# Verifying a run

Use this checklist after the initial setup, and any time you want to confirm the
dashboard and the on-demand refresh are actually working, not just that a routine
exists.

## 1. The dashboard page

Open the dashboard url.

- The footer or freshness pill shows an as-of date and a pull time. It should not say
  "refresh overdue" or "Data checks failed" (see `OPERATIONS.md`'s "When something
  looks wrong" if it does).
- Next to the existing MTD / YTD / 13m buttons, there are two dropdowns for a start
  month and an end month. Pick two different months (for example, three months
  apart). The KPI tiles, the channel table, and the moving-SKUs section should update
  to that range, and the label above them should read something like "Mar 2026 to May
  2026".
- Sections that don't respond to a custom range -- inventory, working capital, the
  Amazon marketplace card, returns, orders and AOV, the life-to-date tile, demand --
  should each show a small caption reading either "Not range-aware: MTD and YTD only"
  or "Not range-aware: as of `<date>` MT". That's expected; it's not a bug.
- Set the range back to MTD or YTD and confirm the numbers match what you'd expect
  from the prior fixed-button behavior -- a custom range should never change what MTD
  or YTD show.

## 2. The refresh request round trip

- Click "Request data refresh." It opens a new tab. You should see either:
  - A confirmation page: "Refresh requested at `<time>` MT. The dashboard republishes
    within the next hourly check (07:00 to 18:00 MT) or at 03:00 MT."
  - Or, if a refresh was already requested in the last 10 minutes: "A refresh was
    already requested at `<time>` MT; the next check honors it." Both are correct
    behavior, not an error.
- Open the "Spikeball Finance Data" Google Sheet and go to the `refresh_requests` tab.
  You should see a new row with `status` = `queued`, with `requested_at_utc` and
  `requested_at_mt` filled in and `source` = `dashboard`.
- Wait for the next hourly check (07:00 through 18:00 MT during MDT) or the nightly run, then reload
  the Sheet. That row's `status` should now read `honored <timestamp>`, and the
  dashboard's as-of pill should show a newer pull time after you reload the page.

## 3. The run log

Still in the Sheet, open the `run_log` tab. Each run -- whether it actually refreshed
data or just checked and skipped -- can add a row here (a full run always does; a
skip does not write a row at all, by design). For a run that did refresh:

- The row's verdict should be `NIGHTLY_OK` (or `NIGHTLY_PARTIAL_OK` with a reason
  naming which store didn't publish -- the dashboard itself still updated).
- The `trigger` column reads `nightly` for a run that fired at the scheduled nightly
  hour, or `request` for a run that fired because of a queued refresh request.
- When `trigger` is `request`, the `request_row` column names the row number(s) in
  `refresh_requests` that run honored.

## 4. The routine's own run history

At claude.ai/code/routines, open the `Spikeball Finance refresh` routine and look at
its recent runs. Most should end quickly with `NIGHTLY_SKIP <reason>` in the log --
that's the routine correctly deciding there's nothing to do at that hour. A run that
actually refreshes takes longer (NetSuite alone takes roughly 10 minutes) and should
end with one of the verdicts above, never with an unhandled error. If a run's log ends
with `ENV_MISSING`, `ENV_BLOCKED`, or `NIGHTLY_FAIL`, see `OPERATIONS.md`'s "When
something looks wrong" section.

## One artifact, not two

Open https://claude.ai/code/artifacts while signed in as the routine's account. Exactly one artifact
named `Spikeball Finance` should exist for this routine, with the chart icon and the description
`Spikeball sales, margin, EBITDA, balance sheet and cash flow, refreshed nightly and on request.`
If a second one appears after a run, `SPIKEBALL_ARTIFACT_URL` was not set (or was set to the wrong
URL) before that run; set it to the URL of the newest artifact, and unpublish the older duplicate
from its share menu.
