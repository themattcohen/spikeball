# Spikeball GM reconciliation -- routine prompt

This is the self-contained prompt for the Spikeball GM reconciliation routine, scheduled on
cron `0 13 2,6,10 * *` (UTC): the 2nd, 6th and 10th of each month at 07:00 MT during MDT and
06:00 MT during MST. Each run reconciles the previous calendar month as of that morning and
delivers one workbook and one email. **This routine runs from a repository source**, the same
repository and the same cloud environment as the Spikeball Finance refresh routine: the session
starts inside a checkout of the default branch, there is nothing to download, and every
credential is a plain environment variable on the environment.

## Why this design

The repository's own `.claude/settings.json`, loaded when the session starts, carries a
`permissions.allow` list of prefix rules, one per command this prompt runs, in the style
`Bash(python3 spike/gm_recon/run_recon.py:*)`. That list is what lets an unattended session
execute the code. A wrapped command does not inherit the wrapped command's own rule, so
`nohup python3 spike/gm_recon/run_recon.py` and `timeout` are listed as their own entries.
Every bash block below is written to match those rules exactly: always `python3`, never
`python`; run each block verbatim, not paraphrased, even when a rewrite would be equivalent.
If a future run is denied a command, the fix is an additional `permissions.allow` entry in
`.claude/settings.json`, made by the maintainer through the usual merge; never an
`autoMode.*` setting anywhere in this repository.

## What you are

A scheduled, unattended run. Nobody is watching. Follow this prompt exactly, in order, and
stop the moment a step tells you to stop. Do not improvise beyond what is written here, and
do not attempt to fix, investigate, or work around a step that fails: report it and stop.

## Rules (binding, no exceptions)

- **Read-only against NetSuite, Amazon, and every live account.** Nothing here writes to
  NetSuite, Celigo, or Amazon, ever. The job proposes journal entries in a workbook; it never
  books one.
- **The code is never edited by the routine.** This session reads the checkout as it stands.
- **No fake data, ever.** If a step fails, report the failure. Never substitute a placeholder
  or invented value.
- **No secrets in output, ever.** Never print, log, or echo a token, refresh token, client
  secret, or API key value, including inside a relayed error message.
- **Never commit or push anything.**
- **Never touch `.env*` files.**
- **Never run the Spikeball Finance refresh** (`spike/routine/run_nightly.py`) from this
  routine; it has its own schedule and its own gate.

## Environment (this routine's cloud configuration)

Every value below is a plain environment variable on the cloud environment this routine
shares with the nightly refresh. The environment's Setup script runs
`cd /home/user/spikeball && python3 -m pip install -r requirements.txt || true` before this
session starts.

- `NETSUITE_ACCOUNT_ID`, `NETSUITE_CONSUMER_KEY`, `NETSUITE_CONSUMER_SECRET`,
  `NETSUITE_TOKEN_ID`, `NETSUITE_TOKEN_SECRET`: NetSuite token-based authentication for the
  read-only queries. `NETSUITE_ACCOUNT_ID` is one of the two sentinel variables step 1 checks.
- `SPIKEBALL_OAUTH_CLIENT_ID`, `SPIKEBALL_OAUTH_CLIENT_SECRET`,
  `SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN`: Google OAuth for Drive, Sheets and the email.
  `SPIKEBALL_OAUTH_CLIENT_ID` is the other sentinel.
- `SPIKEBALL_ALERT_TO`: the address that receives a failure alert, and the default
  recipient of the monthly email.
- `SPIKEBALL_RECON_TO` (optional): comma-separated recipients of the monthly email and
  readers of the Drive folder. Defaults to `SPIKEBALL_ALERT_TO`.
- `SPIKEBALL_RECON_FOLDER_ID` (optional): the Drive folder the workbook is uploaded to. When
  unset, the run finds or creates a folder named `Spikeball GM Reconciliation` and prints
  `RECON_FOLDER_ID <id>`; set the variable to that id afterwards.
- `SPIKEBALL_FINANCE_SHEET_ID`: the Spikeball Finance Sheet; the run appends one row to its
  `gm_recon_log` tab and touches no other tab.
- `SPIKEBALL_DASH_STATE_FILE_ID`: the Drive file holding the nightly's carry-forward state,
  which includes the Amazon order cache this run reads (and never writes).
- `SP_API_*` variables are present on the environment for the nightly; this routine does not
  call Amazon directly.
- Network access: **Full** (the default "Trusted" mode blocks NetSuite and Google).
- Cron: `0 13 2,6,10 * *` (UTC). MT equivalents: 07:00 MT on the 2nd, 6th and 10th during
  MDT; 06:00 MT during MST. Three runs per month by design: the day-2 run gives the first
  view and the accrual to book, the day-6 and day-10 runs replace the estimate of unsettled
  Amazon fees with measured fees as settlements land.

## Steps

### 1. Pre-flight: are the credentials present and Google reachable?

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
`ENV_MISSING <names>` and a one-sentence note that this routine's cloud environment is
missing those variables. Do not retry, do not attempt any other step.

If `ENV_BLOCKED` printed: **stop.** End your response with exactly
`ENV_BLOCKED: oauth2.googleapis.com unreachable` and a one-sentence note that the network
access setting needs to be **Full**. Do not retry.

### 2. Verify the checkout

```bash
missing=""
for f in spike/gm_recon/run_recon.py spike/routine/alert.py requirements.txt .claude/settings.json; do
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

If `CHECKOUT_MISSING` printed any paths: **stop.** Name the exact paths you looked for, then
end your response with exactly `CHECKOUT_MISSING <paths>`. Do not fetch, clone, or download
anything to work around it.

Log the commit hash; it ties this run's numbers to the code that produced them.

### 3. Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

Treat "already satisfied" as success. If the install fails:

```bash
python3 spike/routine/alert.py --subject "Spikeball GM reconciliation: dependency install failed" \
  --body "python3 -m pip install -r requirements.txt failed in the reconciliation routine's session. <paste the last few lines of pip's output, with any token or key value removed>."
```

Then **stop.**

### 4. Diagnose: check every host the job needs

```bash
python3 spike/gm_recon/run_recon.py --diagnose
```

Read the line matching `^ENV_(OK|BLOCKED)`:

- `ENV_OK`: continue to step 5.
- `ENV_BLOCKED: <host list>`: send the alert and stop.
  ```bash
  python3 spike/routine/alert.py --subject "Spikeball GM reconciliation: environment blocked" \
    --body "run_recon.py --diagnose reported ENV_BLOCKED: <paste the host list>. This routine's network access needs to be set to Full."
  ```

### 5. Run the reconciliation (foreground, with bounded polling)

The sandbox is torn down the moment this session stops making tool calls, so the job must
never be left running in the background while you wait. Do not use ScheduleWakeup, Monitor,
or any background mechanism. Start the job detached, then poll it with blocking commands of
at most 9 minutes each until it exits:

```bash
mkdir -p spike/data/gm_recon
nohup python3 spike/gm_recon/run_recon.py > spike/data/gm_recon/recon.log 2>&1 &
echo $! > spike/data/gm_recon/recon.pid
```

Then repeat this command until it prints `DONE` (a run takes about three minutes, so
usually one iteration; the NetSuite queries are the slow part):

```bash
timeout 540 tail --pid=$(cat spike/data/gm_recon/recon.pid) -f /dev/null; if kill -0 $(cat spike/data/gm_recon/recon.pid) 2>/dev/null; then echo STILL_RUNNING; tail -3 spike/data/gm_recon/recon.log; else echo DONE; fi
```

When it prints `DONE`, read the verdict:

```bash
grep -E "^(RECON_(OK|PARTIAL_OK|FAIL)|RECON_FOLDER_ID)" spike/data/gm_recon/recon.log | tail -2; tail -12 spike/data/gm_recon/recon.log
```

With no `--month`, the job reconciles the previous calendar month in Mountain Time as of
today. It reads every credential from the environment, downloads the nightly's Drive state
to read the Amazon order cache (and never uploads it), queries NetSuite read-only, builds
the workbook, uploads it to the Drive folder, shares the folder read-only with the
recipients, sends the email, and appends one `gm_recon_log` row. The verdict is the last
line: `RECON_OK <link>`, `RECON_PARTIAL_OK <reason>`, or `RECON_FAIL <reason>`.

### 6. On `RECON_OK <link>`

a. The email is already sent and the workbook is on Drive. Log one line with the link and
   the commit hash.
b. If the output contained `RECON_FOLDER_ID <id>` and `SPIKEBALL_RECON_FOLDER_ID` is not set
   on this environment, end your summary with the sentence "Set SPIKEBALL_RECON_FOLDER_ID to
   <id> in this routine's environment so every run targets the same folder."
c. Stop. Do not commit or push anything.

### 7. On `RECON_PARTIAL_OK <reason>`

The workbook was built but the upload, the email or the log row failed; the reason names
which. Send one alert so the controller knows to expect a re-run, then stop:

```bash
python3 spike/routine/alert.py --subject "Spikeball GM reconciliation: PARTIAL" \
  --body "The reconciliation built its workbook but a delivery step failed: <paste the RECON_PARTIAL_OK line>. The next scheduled run (2nd, 6th or 10th at 07:00 MT) will produce a fresh copy."
```

Do not retry, do not investigate.

### 8. On `RECON_FAIL <reason>`

Nothing was written anywhere. Send one alert and stop:

```bash
python3 spike/routine/alert.py --subject "Spikeball GM reconciliation: FAILED" \
  --body "The reconciliation did not complete: <paste the RECON_FAIL line, with any token or key value removed>. Nothing was written. The next scheduled run will retry."
```

Do not retry, do not investigate NetSuite, Drive or Sheets by hand, do not attempt a fix.

## Sandbox environment notes

- Nothing in this routine needs a browser or GUI.
- The workbook under `spike/data/gm_recon/` in this sandbox is lost when the session ends;
  the Drive copy is the record.
- This routine shares the environment with the nightly refresh but never runs it and never
  takes its lock; the two can fire in the same hour without interfering.
