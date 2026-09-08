# Spikeball Finance dashboard: handoff packet

## What this is

A self-contained packet that sets up the Spikeball Finance dashboard to run entirely
on your own Claude Code account, on a schedule, with no dependency on any other
account or on the vendor's infrastructure. Once set up, a routine on your account
downloads the dashboard's code, pulls fresh data from NetSuite and Amazon, runs the
data checks, and publishes the results to a page you can bookmark and share. It runs
automatically every night and, if you ask it to, within about an hour of an on-demand
refresh request from the dashboard page itself.

This packet contains:

- `README-HANDOFF.md` -- this file.
- `SESSION-PROMPT.md` -- the message you paste into a new Claude Code session to do
  the setup for you.
- `ROUTINE-PROMPT.md` -- the instructions that session will hand to the scheduled
  routine it creates. You don't need to read this in detail; the setup session reads
  it and uses it automatically.
- `OPERATIONS.md` -- how the dashboard works day to day: what refreshes, when, what
  the numbers mean, and what to do if something looks wrong.
- `VERIFY.md` -- a checklist for confirming a run actually worked.
- `CUTOVER.md` -- when and how to retire the older dashboard routine once you're
  satisfied this one is working.

No credentials and no code are in this packet. Credentials come from a separate file
provided outside the zip; you paste its contents into the cloud environment you create
in step 1 below.

## Prerequisites

- A Claude Code account (claude.ai/code) with access to cloud environments, scheduled
  routines, and the Artifact tool. This is the account that will own the dashboard
  going forward.
- The Spikeball Google assets this dashboard already uses -- the "Spikeball Finance
  Data" Google Sheet, the BigQuery dataset, the Drive files holding the code bundle
  and the pipeline's carry-forward state, and the on-demand refresh request endpoint
  -- already exist and don't need to be recreated. This packet only points a new
  routine at them.
- The separate credentials file (`.env` format) mentioned above, ready to paste.

## Human steps, in order

1. In your browser, sign in to claude.ai/code, open the environment selector at the
   composer, choose Cloud, then "Add cloud environment...". Set:
   - **Name**: `Spikeball Finance`
   - **Network access**: `Full` (not the default `Trusted` -- Trusted blocks
     NetSuite, Amazon, and Google, and the routine will not run)
   - **Environment variables**: paste the full contents of the credentials file
     provided to you outside this packet, exactly as given
   - **Setup script**: leave empty
   Save the environment.
2. Start a new Claude Code session, attach this packet's zip file, and paste the full
   text of `SESSION-PROMPT.md` as your first message. The session will do the rest of
   the setup itself, including creating the schedule and running it once to prove it
   works.
3. The session will stop and ask you to do one thing partway through: after its first
   run, it prints a line like `ARTIFACT_URL <url>`. Add `SPIKEBALL_ARTIFACT_URL=<that
   url>` to the `Spikeball Finance` environment's variables (edit the cloud
   environment, add the one line, save), then tell the session you've done it so it
   can continue.
4. When the session finishes, it gives you the dashboard's url, its schedule in
   Mountain Time, and what to do if a run ever fails. Bookmark and share the url.
   Use `VERIFY.md` to double-check a run before you rely on it, then read
   `CUTOVER.md` when you're ready to retire the older routine.

## What the session does by itself

Once you paste `SESSION-PROMPT.md`, the session:

- Reads `ROUTINE-PROMPT.md` from the packet.
- Lists your cloud environments and confirms `Spikeball Finance` exists with network
  access set to Full (it stops and asks you to fix this if not, then re-checks).
- Creates the scheduled routine, named `Spikeball Finance refresh`, on the cron
  described in `OPERATIONS.md`, using `ROUTINE-PROMPT.md`'s full text as the
  routine's prompt, with no repository source and only the Bash and Artifact tools
  enabled.
- Runs the routine once immediately and waits for it to finish, reading its log.
- Reports the artifact url it published and stops to have you set
  `SPIKEBALL_ARTIFACT_URL` (step 3 above).
- Runs the routine once more to confirm the new setting is picked up (either a
  no-op skip or a fresh update, both are fine -- `SESSION-PROMPT.md` explains which
  is expected).
- Opens the published page and confirms the month range control and the "Request
  data refresh" link are present.
- Ends with a short summary: the dashboard url, the schedule in Mountain Time, and
  what to do if a run fails.

## Cutover rule

An older dashboard routine, on a different account, currently publishes its own page
every night at 03:00 MT and will keep doing so until someone disables it. Both
routines write to the same Google Sheet, the same BigQuery dataset, and the same
Drive state file, so this new routine's nightly run is deliberately scheduled an hour
apart from the older one (04:00 MT during MDT, rather than 03:00 MT) so the two never
write in the same hour. This is safe to leave running in parallel for as long as you
want to compare the two. When you're ready to standardize on this one, follow
`CUTOVER.md` to disable the older routine and move this one's nightly run back to its
final hour.

## Rollback

If anything about this routine needs to be undone, pause it at
claude.ai/code/routines. The dashboard page keeps showing its last published version
-- nothing disappears, it just stops refreshing until you re-enable the routine.
