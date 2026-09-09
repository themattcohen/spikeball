# Spikeball Finance dashboard -- gated routine prompt

This is the self-contained prompt for the Spikeball Finance refresh routine
(PRD-month-refresh.md Section 5 M5 / Section 8), scheduled on cron
`0 0,10,13-23 * * *` (UTC) -- see the Environment section below for the MT equivalents.
**This routine runs from a repository source.** The session starts already inside a
checkout of that repository's default branch -- there is nothing to download, no zip,
no Drive bundle. Every credential this routine needs is set directly as an environment
variable on this routine's cloud environment (see the Environment section below); there
is no separate secrets manager or token exchange beyond the Google OAuth refresh used
throughout the pipeline.

## Why this design

Earlier versions of this routine downloaded the pipeline code as a zip from Drive on
every run. That worked for the credential and network checks, but the session's own
auto-mode permission classifier would not execute code it had just downloaded as an
unreviewed bundle: it denied `pip install` and `python3 spike/routine/run_nightly.py`
even on runs where every credential was present and every host was reachable, so the
routine could never get past its own permission check to run the pipeline -- and since
`alert.py` was inside that same unreviewed bundle, the routine could not even report
its own failure. Attaching this repository as the routine's source removes the
download entirely: the code this session runs is the same code checked into the
repository, already reviewed there, and this repository's own `.claude/settings.json`,
loaded automatically when the session starts, carries a `permissions.allow` list of
prefix rules (one entry per command this prompt runs, in the style
`Bash(python3 spike/routine/run_nightly.py:*)`) that the auto-mode classifier honors.
That `permissions.allow` list is the entire fix -- the routine's own stored
`auto_mode_allow` / `auto_mode_environment` / `auto_mode_soft_deny` fields (visible in
its configuration) are a separate mechanism that only an account's own user settings
or organization policy can set; a repository cannot populate them, and nothing in this
repository tries to. Do not confuse the two: if a future run is still denied a command,
the fix is an additional `permissions.allow` entry in `.claude/settings.json`, never an
`autoMode.*` setting anywhere in this repository. Nothing about what the pipeline does
changes -- only how the session reaches the code and how its permissions are granted.

Because a wrapped command does not inherit the wrapped command's own allow rule,
`.claude/settings.json` lists `nohup python3 spike/routine/run_nightly.py` and
`timeout` (used in step 5 below) as their own separate entries, alongside every other
command these steps run (`git`, `pip install`, `curl`, `tail`, `cat`, `kill`, `grep`,
`mkdir`, `echo`, `ls`, `date`, `head`, `wc`, `test`, `cd`, `python3 --version`, and each
`python3 spike/routine/*.py` invocation). Every bash block below is written to match
those rules exactly -- always `python3`, never `python` (a rule for one does not match
the other) -- and should be run verbatim, not paraphrased or rewritten inline, even
when the rewrite would be functionally identical.

## What you are

A scheduled, unattended run. Nobody is watching. Follow this prompt exactly, in order,
and stop the moment a step tells you to stop. Do not improvise beyond what is written
here, and do not attempt to fix, investigate, or work around a step that fails --
report it and stop.

## Rules (binding, no exceptions)

- **Read-only against NetSuite, Amazon, and every live account.** Nothing here writes
  to NetSuite, Celigo, or Amazon, ever.
- **The code is never edited by the routine.** This session reads the repository
  checkout as it stands; it never modifies, patches, or writes to any file inside it.
- **No fake data, ever.** If a step fails, report the failure. Never substitute a
  placeholder or invented value.
- **No secrets in output, ever.** Never print, log, or echo a token, deploy key,
  refresh token, client secret, or API key value -- including inside a relayed error
  message. If a command's output might contain one (a `curl` response with an
  `Authorization` header echoed back, a verbose install log), don't include that
  output verbatim in anything you write.
- **Never commit or push anything.** This routine only reads the checked-out code.
- **Never touch `.env*` files.**

## Environment (this routine's cloud configuration)

Every value below is set as a plain environment variable on this routine's cloud
environment (Environment variables, `.env` format, in the environment's settings).
Nothing is read from a secrets service; the variable is simply present or it is not.
The environment's Setup script runs `pip install -r requirements.txt` before this
session starts, using the same checkout this session sees.

- `NETSUITE_ACCOUNT_ID`, `NETSUITE_CONSUMER_KEY`, `NETSUITE_CONSUMER_SECRET`,
  `NETSUITE_TOKEN_ID`, `NETSUITE_TOKEN_SECRET` -- NetSuite token-based-authentication
  credentials for the read-only extract. `NETSUITE_ACCOUNT_ID` is also one of the two
  sentinel variables step 1 checks.
- `SPIKEBALL_OAUTH_CLIENT_ID`, `SPIKEBALL_OAUTH_CLIENT_SECRET`,
  `SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN` -- Google OAuth credentials used to mint
  short-lived access tokens for Drive and Sheets throughout the pipeline.
  `SPIKEBALL_OAUTH_CLIENT_ID` is the other sentinel variable step 1 checks.
- `SPIKEBALL_ALERT_TO` -- the email address that receives the failure alert.
- `SPIKEBALL_FINANCE_SHEET_ID` -- the Google Sheet id for "Spikeball Finance Data".
- `SPIKEBALL_DASH_STATE_FILE_ID` -- the Drive file id holding the pipeline's
  carry-forward state (for example, the Amazon order watermark).
- `SPIKEBALL_REFRESH_REQUEST_URL` -- the refresh-request endpoint url, embedded in the
  published page's "Request data refresh" link.
- `SPIKEBALL_LOOKER_REPORT_URL` -- the Looker Studio report url shown in the Sheet's
  `meta` tab.
- `SP_API_LWA_CLIENT_ID`, `SP_API_LWA_CLIENT_SECRET`, `SP_API_REFRESH_TOKEN_NA`,
  `SP_API_REFRESH_TOKEN_EU` -- Amazon Selling Partner API credentials for the North
  America and Europe marketplaces.
- `SPIKEBALL_DASH_FEATURES=range_selector,refresh_control` -- turns on the month range
  selector and the on-demand refresh control for the artifact this routine publishes
  (`meta.features.*` in the built page).
- `SPIKEBALL_NIGHTLY_SLOT_UTC` -- the UTC hour `refresh_gate.py` treats as the
  guaranteed nightly run (the decision table's nightly-slot rule, PRD-month-refresh.md
  Section 5). Set to `10` while the existing nightly routine on the other Spikeball
  account still runs at UTC hour 9, so the two never fire in the same hour; move it to
  `9` only after that routine is disabled, and change the cron's `10` to `9` at the
  same time so the guaranteed run and the cron slot stay aligned (see the handoff
  packet's `CUTOVER.md`).
- `SPIKEBALL_ARTIFACT_URL` -- the artifact this routine updates. Leave it unset for the
  very first run under a new environment; that run publishes a fresh artifact and
  prints `ARTIFACT_URL <url>`; set the variable to that url before the next run
  (step 7b).
- `SPIKEBALL_DASH_BUNDLE_FILE_ID`, used by the earlier zip-download design, is no
  longer read by this prompt. Leaving it set on the environment is harmless; it can be
  removed whenever convenient.
- Network access: **Full** (not the default "Trusted" mode -- Trusted blocks NetSuite,
  Amazon, and Google's OAuth endpoint; see step 1 and step 4).
- Cron: `0 0,10,13-23 * * *` (UTC, five-field, minimum one-hour granularity -- this
  platform has no sub-hour trigger). MT equivalents:
  - During MDT (summer): hourly from 07:00 through 18:00 MT, plus the 04:00 MT nightly slot.
  - During MST (winter): one hour earlier than each of those: hourly 06:00 through 17:00 MT,
    plus 03:00 MT.
  At most 13 sessions/day; most exit at step 6 with `NIGHTLY_SKIP` and never reach the
  pipeline (`refresh_gate.py`'s decision table -- PRD-month-refresh.md Section 5).

## Steps

### 1. Pre-flight: are the credentials present and Google reachable?

Nothing below this line is possible without the credentials in this environment and a
reachable Google OAuth endpoint. Check both first, before touching Python at all:

```bash
missing=""
for v in NETSUITE_ACCOUNT_ID SPIKEBALL_OAUTH_CLIENT_ID; do
  if [ -z "${!v}" ]; then
    missing="$missing $v"
  fi
done
if [ -n "$missing" ]; then
  echo "ENV_MISSING$missing"
else
  echo "ENV_VARS_OK"
fi

if curl -sS -o /dev/null --max-time 8 https://oauth2.googleapis.com/; then
  echo "Google OAuth endpoint reachable"
else
  echo "ENV_BLOCKED: oauth2.googleapis.com unreachable"
fi
```

If `ENV_MISSING` printed any names: **stop.** End your response with exactly
`ENV_MISSING <names>` (the names the check printed) and a one-sentence note that this
routine's cloud environment is missing those variables. Do not retry, do not attempt
any other step.

If `ENV_BLOCKED` printed: **stop.** End your response with exactly `ENV_BLOCKED:
oauth2.googleapis.com unreachable` and a one-sentence note that this routine's network
access setting needs to be **Full**, not the default. Do not retry, do not attempt any
other step.

### 2. Verify the checkout

This session starts inside a checkout of the repository this routine is attached to,
at the repository's default branch. Confirm the checkout is actually what it should be
before installing anything or touching Python:

```bash
missing=""
for f in spike/routine/run_nightly.py requirements.txt .claude/settings.json; do
  if [ ! -f "$f" ]; then
    missing="$missing $f"
  fi
done
if [ -n "$missing" ]; then
  echo "CHECKOUT_MISSING$missing"
else
  echo "CHECKOUT_OK"
fi
git rev-parse --short HEAD 2>/dev/null || echo "no git metadata in this checkout"
```

If `CHECKOUT_MISSING` printed any paths: **stop.** This means the routine's repository
source is misconfigured, or missing, or pointed at the wrong branch. Name the exact
path(s) you looked for and confirm nothing is present at that path, then end your
response with exactly `CHECKOUT_MISSING <paths>`. Do not attempt to fetch, clone, or
download anything yourself to work around it.

Log the commit hash `git rev-parse --short HEAD` printed (not a secret, safe to
include in your summary) -- it ties this run's numbers to the exact version of the
code that produced them, which matters if a later run's output ever needs explaining.

### 3. Install dependencies

This environment provisions in order: the environment itself, then the repository
checkout, then the Setup script (`pip install -r requirements.txt`, configured on the
environment), and only then does this session start, inside that checkout. Whether
packages the Setup script installs persist into this session is not documented, so
this step re-runs the install defensively rather than assuming they do. Run it every
time, and treat a run that reports everything already satisfied as success, not as
evidence the Setup script didn't work:

```bash
pip install -r requirements.txt
```

If this fails for any reason, the pipeline cannot run. `alert.py` lives in this same
checkout, so it is available even here:

```bash
python3 spike/routine/alert.py --subject "Spikeball Finance nightly: dependency install failed" \
  --body "pip install -r requirements.txt failed in the routine's session. <paste the last few lines of pip's output, with any token or key value removed>."
```

Then **stop.** Do not attempt to work around a missing or broken dependency yourself.

### 4. Diagnose -- check every host the pipeline needs, before running it

```bash
python3 spike/routine/run_nightly.py --diagnose
```

This needs no credentials -- it's a pure reachability check, and it's fast (a few
seconds). Read the line matching `^ENV_(OK|BLOCKED)` (an environment notice may print before it):

- **`ENV_OK`** -- every host is reachable. Continue to step 5.
- **`ENV_BLOCKED: <host list>`** -- one or more hosts aren't reachable (most likely:
  the routine's network access is still set to its default "Trusted" mode, which
  blocks NetSuite, Amazon, and Google; it needs to be **Full**). Since step 1 already
  confirmed the environment variables are set and Google's OAuth endpoint is
  reachable, `alert.py` can still send the operator email even here (it uses the same
  Google credentials step 1 just confirmed):
  ```bash
  python3 spike/routine/alert.py --subject "Spikeball Finance nightly: environment blocked" \
    --body "run_nightly.py --diagnose reported ENV_BLOCKED: <paste the host list>. This routine's network access needs to be set to Full."
  ```
  Then **stop** -- do not run `run_nightly.py` itself; it would only fail slowly at
  the same blocked hosts.

### 5. Run the gated orchestration (foreground, with bounded polling)

The sandbox is torn down the moment this session stops making tool calls, so the pipeline must never
be left running in the background while you wait. Do not use ScheduleWakeup, Monitor, or any
background/`run_in_background` mechanism in this run. Start the job detached, then poll it with
blocking commands of at most 9 minutes each until it exits:

```bash
mkdir -p spike/data
nohup python3 spike/routine/run_nightly.py --gate > spike/data/nightly.log 2>&1 &
echo $! > spike/data/nightly.pid
```

Then repeat this command (a normal, foreground Bash call) until it prints `DONE`; it blocks for up to
9 minutes per call. Most sessions finish almost immediately (a skip decision needs no NetSuite/Amazon
call at all); when the decision is to run, expect 2 to 5 iterations (NetSuite ~10 minutes; the Amazon
leg is seconds once its backfill is complete):

```bash
timeout 540 tail --pid=$(cat spike/data/nightly.pid) -f /dev/null; if kill -0 $(cat spike/data/nightly.pid) 2>/dev/null; then echo STILL_RUNNING; tail -3 spike/data/nightly.log; else echo DONE; fi
```

When it prints `DONE`, read the verdict:

```bash
grep -E "^NIGHTLY_(OK|PARTIAL_OK|FAIL|SKIP)" spike/data/nightly.log | tail -1; tail -12 spike/data/nightly.log
```

`run_nightly.py --gate` reads every credential it needs directly from this environment's variables
(Environment section above); no secrets manager, wrapper command, or extra API call is involved. Before
touching NetSuite, Amazon, Sheets, or any local state, it reads `run_log` and `refresh_requests` on the
Spikeball Sheet and decides whether this slot should run at all (`refresh_gate.py`'s decision table,
PRD-month-refresh.md Section 5, with the nightly-slot hour read from `SPIKEBALL_NIGHTLY_SLOT_UTC`): most
sessions print `NIGHTLY_SKIP <reason>` and exit 0 having written nothing. When it decides to run, it
restores last night's state from Spikeball's Drive at start and uploads the new state after a passing
run, exactly as the nightly routine does. The verdict is one of `NIGHTLY_SKIP <reason>`, `NIGHTLY_OK`,
`NIGHTLY_PARTIAL_OK <reason>`, or `NIGHTLY_FAIL <reason>`. The Sheet publish, the BigQuery publish and
the artifact build run independently; `NIGHTLY_PARTIAL_OK` means the artifact is built and at least one
store published. On `NIGHTLY_FAIL` and `NIGHTLY_PARTIAL_OK` the script has already sent the operator
alert; do not send a second one.

### 6. On `NIGHTLY_SKIP <reason>`

This is the normal outcome for most sessions -- the gate decided this slot needs no run (already
covered by a recent success, no queued refresh request, not the nightly slot, lock busy, or within the
55-minute retry cap on a stale run). Nothing was downloaded or written.

a. Log one line naming the reason (e.g. `NIGHTLY_SKIP no_request`) and stop.
b. Do **not** republish anything -- there is no new artifact to publish.
c. Do not retry, do not investigate, do not run `run_nightly.py` again this session.

### 7. On `NIGHTLY_OK` or `NIGHTLY_PARTIAL_OK <reason>`

Both mean the same thing for this step: the artifact page is built and ready. Treat
them identically here (the only difference is whether every store also published
cleanly -- `NIGHTLY_PARTIAL_OK`'s reason text says which one didn't, but that's
informational, not something to act on).

a. Read the summary/reason text -- it names the artifact file path
   (`design/mockup/dashboard.artifact.html`, relative to this checkout's root), the
   `asof_date`, and (for `NIGHTLY_PARTIAL_OK`) which store(s) succeeded vs. failed.
b. Publish that file's contents with the Artifact tool. Which artifact depends on the
   environment variable `SPIKEBALL_ARTIFACT_URL`:
   - If `SPIKEBALL_ARTIFACT_URL` is set (every run after the first): pass it as `url` so
     this is an update of the existing page, `file_path`
     `design/mockup/dashboard.artifact.html` (relative to this checkout's root),
     `label` `gate-<asof_date>` (e.g. `gate-2026-09-08`); omit `title`, `favicon`,
     `description`, and never pass `force`. Do not target any other artifact.
   - If `SPIKEBALL_ARTIFACT_URL` is empty or unset (the first run under a new
     environment): publish a NEW artifact with the same `file_path` and `label`,
     `title` `Spikeball Finance`, `favicon` `📈`, `description`
     `Spikeball sales, margin, EBITDA, balance sheet and cash flow, refreshed nightly and on request.`
     Then print, on its own line, `ARTIFACT_URL <the url the tool returned>` and end the
     session summary with the sentence "Set SPIKEBALL_ARTIFACT_URL to that url in this
     routine's environment so the next run updates it instead of creating another." Do
     this at most once per session; never create a second artifact in the same run.
c. Stop. The run is complete either way. Do not commit or push anything (see Rules);
   do not attempt to fix or retry a failed store on a `NIGHTLY_PARTIAL_OK` -- the
   already-sent alert covers that, and the next scheduled slot will retry it naturally.

### 8. On `NIGHTLY_FAIL <reason>`

This means checks failed, extract crashed, or **neither** store published (unlike
`NIGHTLY_PARTIAL_OK`, where at least one did).

a. Do **not** republish the artifact -- the previous good version stays live on its
   own, you don't need to do anything to preserve it.
b. The alert has already gone out (`run_nightly.py` calls `alert.py` itself). Do not
   send a second one.
c. Stop. Do not retry, do not investigate NetSuite/Amazon/BigQuery/Sheets by hand, do
   not attempt a fix. This routine's job is to run and report, not to debug.

## Sandbox environment notes

- This routine reads every credential from its own environment variables
  (Environment section above); no secrets manager or additional token exchange is
  needed beyond the Google OAuth refresh used throughout the pipeline.
- Nothing in this routine needs a browser, X server, or GUI of any kind.
- If the checkout is missing `spike/routine/run_nightly.py`, `requirements.txt`, or
  `.claude/settings.json` (step 2), that's a stop-and-report condition, not something
  to work around: name the exact path you looked for and what's actually there.
