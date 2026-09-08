# Cutover: retiring the older routine

## Why this matters

Right now, two routines can both be refreshing the dashboard: the older one, on a
different account, still runs every night at 03:00 MT and publishes its own page; the
new one this packet set up runs on the schedule in `OPERATIONS.md`, with its nightly
run deliberately placed an hour apart (04:00 MT during MDT, rather than 03:00 MT) so
the two never write in the same hour. Both routines write to the same Google Sheet,
the same BigQuery dataset, and the same Drive file that holds the pipeline's
carry-forward state. If they ever ran in the same hour, the two writes could
overwrite each other, and the state file in particular could revert to an older
Amazon order watermark, risking a duplicated or skipped order window on the next run.
Keeping the nightly runs an hour apart avoids that entirely, so there's no urgency --
run both in parallel for as long as you want to compare them.

There's no fixed waiting period before cutting over. Do it once you've watched a few
nightly runs and at least one on-request run succeed on the new routine (`VERIFY.md`
covers what to check), and you're ready to treat this dashboard as the one of record.

## Step 1: disable the older routine

The older routine is named "Spikeball Finance nightly refresh" and publishes its own,
separate page. Find it at claude.ai/code/routines on the account that owns it, and
pause or disable it. Its page stops refreshing at that point but keeps showing its
last published version -- nothing is deleted. From here on, point anyone who used that
older page's link at the new dashboard's url instead.

## Step 2: move the new routine's nightly run to its final hour

With the older routine disabled, there's no longer a collision to avoid, so move the
new routine's nightly run from 04:00 MT (MDT) back to the older routine's original
03:00 MT slot:

1. At claude.ai/code/routines, open `Spikeball Finance refresh` and edit its cron
   expression from `0 0,10,13-23 * * *` to `0 0,9,13-23 * * *` (only the `10` changes
   to `9`).
2. Edit the `Spikeball Finance` cloud environment's variables and change
   `SPIKEBALL_NIGHTLY_SLOT_UTC` from `10` to `9`. Both changes need to happen together
   -- the cron controls when the routine's session starts, and
   `SPIKEBALL_NIGHTLY_SLOT_UTC` controls which of those hours the routine treats as the
   guaranteed nightly run rather than an optional check.
3. Save both changes.

## Step 3: confirm the change took

After the next scheduled nightly run (03:00 MT during MDT, 02:00 MT during MST),
check the Sheet's `run_log` tab: the new row's `trigger` column should read `nightly`.
If a run at that hour instead reads `NIGHTLY_SKIP`, something is off with the slot
hour change -- recheck that both the cron and `SPIKEBALL_NIGHTLY_SLOT_UTC` were
updated to `9`.

## Rollback

If anything about this routine needs to be undone, at any point, pause it at
claude.ai/code/routines. The dashboard page keeps showing its last published version.
