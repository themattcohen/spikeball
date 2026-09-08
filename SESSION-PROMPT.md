You are setting up the Spikeball Finance dashboard's scheduled refresh on this Claude
Code account. A human is attaching a zip packet to this message. Do the setup
described below yourself, in order, and stop exactly where told to stop and wait for
the human -- do not skip a stop point, do not guess a value you're told to ask for.

## 0. Get the packet open

If the attached zip isn't already extracted into your working directory, extract it
first. Confirm you can see these files before continuing: `README-HANDOFF.md`,
`ROUTINE-PROMPT.md`, `OPERATIONS.md`, `VERIFY.md`, `CUTOVER.md`. Read `ROUTINE-PROMPT.md`
in full now -- it is the exact text you will hand to the routine you create in step 3,
and its Environment section names every variable the routine's cloud environment must
have.

## 1. Confirm the cloud environment exists and is configured correctly

List your cloud environments. Find one named exactly `Spikeball Finance`.

- If no environment with that name exists, or it exists but its network access is not
  set to `Full`: **stop here.** Tell the human: create (or fix) a cloud environment
  named `Spikeball Finance` with network access `Full`, following `README-HANDOFF.md`
  step 1, then reply here when it's done. When they reply, list environments again and
  re-check before continuing.
- Once it exists with `Full` network access: ask the human to confirm they pasted the
  complete credentials block into that environment's Environment variables (you
  cannot read the variable values yourself to check this). Wait for their
  confirmation before continuing -- if a variable is missing, the routine's own first
  run will catch it and report exactly which name is missing (see step 5 below), but
  don't proceed to create the routine until the human has confirmed they pasted the
  block.

## 2. Note the environment id

From the same listing, record the id of the `Spikeball Finance` environment. You need
it for the next step.

## 3. Create the routine

Use your scheduling capability to create a routine with exactly these fields. The
`content` field is the full, verbatim text of `ROUTINE-PROMPT.md` you read in step 0 --
paste it in whole, unedited.

```
name: "Spikeball Finance refresh"
cron_expression: "0 0,10,13-23 * * *"
enabled: true
job_config.ccr:
  environment_id: <the id you recorded in step 2>
  session_context:
    model: "claude-sonnet-5"
    sources: []
    allowed_tools: ["Bash", "Artifact"]
  events:
    - data:
        uuid: <generate a new uuid>
        session_id: ""
        type: "user"
        parent_tool_use_id: null
        message:
          role: "user"
          content: <the full text of ROUTINE-PROMPT.md, verbatim>
```

No repository source is configured (`sources: []` is intentional, not an omission) --
the routine downloads its own code every run, per `ROUTINE-PROMPT.md` step 2.

## 4. Run it once now

Trigger a run of the routine you just created. Then list its runs and read the run's
log, polling until the run reaches a terminal state. If your routine tool offers no run-log action, open
https://claude.ai/code/routines in the browser, click the routine, open the latest run, and read
its log there; if you cannot open it, ask the human to paste the run's final lines. This first run has no prior
history, so it should actually execute the pipeline rather than skip -- expect it to
take several minutes.

## 5. Read the result

- **`ENV_MISSING <names>`**: the environment is missing one or more of those exact
  variable names. Tell the human which names, and ask them to add those to the
  `Spikeball Finance` environment. Once they confirm, go back to step 4 and run again.
- **`ENV_BLOCKED: oauth2.googleapis.com unreachable`**: the environment's network
  access is not actually `Full`. Tell the human to fix it, then go back to step 4.
- **`NIGHTLY_FAIL <reason>`**: something in the pipeline failed on a real attempt.
  Report the reason to the human as-is; this is not something to retry yourself.
- **`ARTIFACT_URL <url>` printed, with `NIGHTLY_OK` or `NIGHTLY_PARTIAL_OK`**: this is
  success. Record the url -- you'll need it in the next step and in your final
  summary. Continue to step 6.
- **`NIGHTLY_SKIP <reason>`**: unexpected on this very first run (there's no run
  history yet, so the gate should not have a reason to skip). Report the reason to the
  human and stop; don't proceed to step 6 until they've looked at it with you.

## 6. Stop and have the human set the artifact url

**Stop here.** Tell the human: "The dashboard is published at `<url from step 5>`.
Add `SPIKEBALL_ARTIFACT_URL=<that url>` to the `Spikeball Finance` environment's
variables now, then let me know when it's saved." Wait for their reply. Do not
continue to step 7 until they confirm.

## 7. Run it once more

Once the human confirms the variable is set, trigger the routine again and read its
log the same way as step 4. Either of these outcomes means the setup worked:

- `NIGHTLY_SKIP no_request` (most likely -- the gate correctly sees a recent success
  and nothing queued, so it skips without touching the artifact). This is a pass, not
  a failure.
- `NIGHTLY_OK` or `NIGHTLY_PARTIAL_OK` with the artifact updated at the same url as
  before (no new `ARTIFACT_URL` line -- if you see a second `ARTIFACT_URL` line, that
  means a second artifact was created by mistake and you should tell the human
  immediately rather than proceeding).

If you see `ENV_MISSING`, `ENV_BLOCKED`, or `NIGHTLY_FAIL` here, treat them the same
way as in step 5.

## 8. Confirm the page itself

Open the artifact at the url from step 5 and confirm both of the following are
present: a month range control (two dropdowns for a start and end month, next to the
existing MTD / YTD / 13m buttons) and a "Request data refresh" link. If either is
missing, tell the human -- this usually means the `Spikeball Finance` environment is
missing `SPIKEBALL_DASH_FEATURES=range_selector,refresh_control`.

## 9. Final summary

End your session with:

- The dashboard url.
- The schedule in Mountain Time, from `ROUTINE-PROMPT.md`'s Environment section
  (during MDT: hourly 07:00 through 18:00 MT plus the 04:00 MT nightly slot; during MST: one hour earlier
  than each of those).
- What to do if a run ever fails: open the routine's runs at
  claude.ai/code/routines, read the log of the failed run, and check `OPERATIONS.md`'s
  "When something looks wrong" section for the most common causes.
- A reminder to run through `VERIFY.md` before relying on this dashboard day to day,
  and to read `CUTOVER.md` when ready to retire the older routine.
