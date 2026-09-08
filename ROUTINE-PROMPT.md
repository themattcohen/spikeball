# Spikeball Finance dashboard -- gated routine prompt

This is the self-contained prompt for the Spikeball Finance refresh routine
(PRD-month-refresh.md Section 5 M5 / Section 8), scheduled on cron
`0 0,10,13-23 * * *` (UTC) -- see the Environment section below for the MT equivalents.
**This routine has no repository source** -- you download the code yourself, every
run, as a zip bundle from Spikeball's Google Drive (see step 2). Every credential this
routine needs is set directly as an environment variable on this routine's cloud
environment (see the Environment section below); there is no separate secrets
manager or token exchange beyond the Google OAuth refresh in step 2.

## What you are

A scheduled, unattended run. Nobody is watching. Follow this prompt exactly, in order,
and stop the moment a step tells you to stop. Do not improvise beyond what is written
here, and do not attempt to fix, investigate, or work around a step that fails --
report it and stop.

## Rules (binding, no exceptions)

- **Read-only against NetSuite, Amazon, and every live account.** Nothing here writes
  to NetSuite, Celigo, or Amazon, ever.
- **No fake data, ever.** If a step fails, report the failure. Never substitute a
  placeholder or invented value.
- **No secrets in output, ever.** Never print, log, or echo a token, deploy key,
  refresh token, client secret, or API key value -- including inside a relayed error
  message. If a command's output might contain one (a `curl` response with an
  `Authorization` header echoed back, a verbose git clone line), don't include that
  output verbatim in anything you write.
- **Never commit or push anything.** This routine only reads the code bundle.
- **Never touch `.env*` files.**

## Environment (this routine's cloud configuration)

Every value below is set as a plain environment variable on this routine's cloud
environment (Environment variables, `.env` format, in the environment's settings).
Nothing is read from a secrets service; the variable is simply present or it is not.

- `NETSUITE_ACCOUNT_ID`, `NETSUITE_CONSUMER_KEY`, `NETSUITE_CONSUMER_SECRET`,
  `NETSUITE_TOKEN_ID`, `NETSUITE_TOKEN_SECRET` -- NetSuite token-based-authentication
  credentials for the read-only extract. `NETSUITE_ACCOUNT_ID` is also one of the two
  sentinel variables step 1 checks.
- `SPIKEBALL_OAUTH_CLIENT_ID`, `SPIKEBALL_OAUTH_CLIENT_SECRET`,
  `SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN` -- Google OAuth credentials used to mint
  short-lived access tokens for Drive and Sheets (step 2 and throughout the pipeline).
  `SPIKEBALL_OAUTH_CLIENT_ID` is the other sentinel variable step 1 checks.
- `SPIKEBALL_ALERT_TO` -- the email address that receives the failure alert.
- `SPIKEBALL_FINANCE_SHEET_ID` -- the Google Sheet id for "Spikeball Finance Data".
- `SPIKEBALL_DASH_BUNDLE_FILE_ID` -- the Drive file id of the code bundle step 2
  downloads.
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

### 2. Download this project's code bundle

The routine's sandbox has no `ssh` binary and rewrites GitHub SSH URLs to HTTPS, so the code is not
cloned from GitHub. The owner publishes a zip of the project tree to Spikeball's own Google Drive, and
this step downloads it with a short-lived Google access token minted from the refresh token already in
this environment. Nothing is printed except the byte count.

```bash
python3 - <<'PY'
import io, json, os, urllib.parse, urllib.request, zipfile
client_id = os.environ["SPIKEBALL_OAUTH_CLIENT_ID"]
client_secret = os.environ["SPIKEBALL_OAUTH_CLIENT_SECRET"]
refresh_token = os.environ["SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN"]
body = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret,
                               "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode()
at = json.load(urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body), timeout=60))["access_token"]
fid = os.environ["SPIKEBALL_DASH_BUNDLE_FILE_ID"]
z = urllib.request.urlopen(urllib.request.Request(f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media",
                                                  headers={"Authorization": "Bearer " + at}), timeout=120).read()
zipfile.ZipFile(io.BytesIO(z)).extractall("dash")
print("BUNDLE_OK", len(z), "bytes")
PY
cd dash
```

The extracted folder `dash/` is the dashboard project: `spike/`, `design/`, `PRD.md` sit at the top.
If the download fails, stop and report the HTTP error (it does not contain a credential).

### 3. Install dependencies

```bash
pip install requests requests-oauthlib
```

### 4. Diagnose -- check every host the pipeline needs, before running it

```bash
python3 spike/routine/run_nightly.py --diagnose
```

This needs no credentials -- it's a pure reachability check, and it's fast (a few
seconds). Read its first output line:

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
   (`design/mockup/dashboard.artifact.html`, relative to `dash/`), the `asof_date`, and
   (for `NIGHTLY_PARTIAL_OK`) which store(s) succeeded vs. failed.
b. Publish that file's contents with the Artifact tool. Which artifact depends on the
   environment variable `SPIKEBALL_ARTIFACT_URL`:
   - If `SPIKEBALL_ARTIFACT_URL` is set (every run after the first): pass it as `url` so
     this is an update of the existing page, `file_path`
     `dash/design/mockup/dashboard.artifact.html` (adjust if you did not `cd dash` in
     step 2), `label` `gate-<asof_date>` (e.g. `gate-2026-09-08`); omit `title`,
     `favicon`, `description`, and never pass `force`. Do not target any other artifact.
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
  needed beyond the Google OAuth refresh in step 2.
- Nothing in this routine needs a browser, X server, or GUI of any kind.
- If `spike/routine/run_nightly.py` doesn't exist after extracting the bundle (an
  out-of-date bundle or an extraction that landed on the wrong path), that's a
  stop-and-report condition: name the exact path you looked for and what's actually
  there.
