# Unblock: getting the scheduled routine actually running

This is the single ordered checklist from today's state (routine created, credentials
in place, one successful pipeline run, but the scheduled routine itself has never
completed one) to a working scheduled routine. Follow it in order. Steps marked
**HUMAN** need a person to act in a browser or on GitHub; the rest are for the Claude
Code session working this checklist to run directly.

Routine: `Spikeball Finance refresh`, id `trig_012sYmV5ZRzkcaRvHgTcxgTc`.
Environment: `Spikeball Finance`, id `env_01XTN5CezsGWv8FTYZLEVn61`.

## 0. What is true today

Read this before touching anything -- it corrects two things the earlier gap analysis
got wrong, and explains what is actually still broken.

- One pipeline run has succeeded end to end, recorded in the Sheet's `run_log` tab at
  22:35:52 UTC on 2026-09-08. That run happened from an interactive session with a
  local, one-off permission override -- it proves the pipeline itself works, not that
  the scheduled routine works.
- The scheduled routine has never completed a run. Two attempts got through the
  environment-variable check and the Google-reachability check, then were blocked: the
  session's own permission classifier refused to run `pip install` and
  `python3 spike/routine/run_nightly.py` because that code had just been downloaded as
  an unreviewed zip. Because `alert.py` was inside that same zip, the routine could not
  even email that it had failed. This is the actual blocker, and it is a permissions
  problem, not a credentials or network problem -- the credentials and network access
  are already correct.
- The fix is a repository: the code now lives in a GitHub repository, with a
  `.claude/settings.json` file whose `permissions.allow` list declares the exact
  commands this routine runs as allowed (prefix rules in the style
  `Bash(python3 spike/routine/run_nightly.py:*)`). Attaching that repository to the
  routine as its source means the session starts inside a reviewed checkout instead of
  an unreviewed download, and that `permissions.allow` list loads automatically, so
  the classifier no longer blocks the pipeline. Steps 1 through 4 below carry that out.
  **This is the whole fix** -- the routine's own stored `auto_mode_allow`,
  `auto_mode_environment`, and `auto_mode_soft_deny` fields, which the earlier gap
  analysis found empty, are a different mechanism: only an account's own user settings
  or organization policy can set them, a repository cannot, and nothing in this
  repository or this checklist tries to. If a future command is still denied, the fix
  is another `permissions.allow` entry in the repository's `.claude/settings.json`,
  never an `autoMode.*` setting anywhere.
- Two corrections to the earlier gap analysis:
  - The balance sheet section does **not** need a NetSuite browser session or
    two-factor login, and never has since 2026-08-28. It is built from a nightly
    snapshot of NetSuite's own account balances taken over the API, the same way
    every other section is. An older note describing "exact through June, provisional
    after" and a manual NetSuite report pull was stale; `OPERATIONS.md` has been
    rewritten to match.
  - The on-demand refresh round trip (clicking "Request data refresh" on the page,
    the Sheet queuing a row, a run honoring it) has already been proven end to end:
    a request row was honored, with `run_log`'s `trigger` column reading `request`.
    What has not been proven is that the scheduled routine can run *at all* -- that is
    exactly what this checklist fixes.
- There is no secrets manager on this project's side, and never has been. Every
  credential the routine needs is a plain environment variable on its cloud
  environment. Any earlier reference to a "secrets store" was leftover phrasing, not a
  real system; `OPERATIONS.md` has had it removed.
- A run that finishes in about 90 seconds outside the nightly slot, with no newer
  queued refresh request, is the normal `NIGHTLY_SKIP` outcome, not a failure -- most
  of the routine's 13 scheduled sessions a day are expected to do exactly this. The
  Sheet's `run_log` and `refresh_requests` tabs are the health check that matters, not
  how long a session took.

## 1. HUMAN (repository owner): make the repository private, merge the pull request

**Who**: the owner of the GitHub account `mcohen-spikeball`.
**Where**: `https://github.com/mcohen-spikeball/spikeball`, Settings tab for the
private/public toggle; Pull requests tab for the merge.

- Set the repository's visibility to Private (Settings > General > Danger Zone > Change
  visibility). It is currently Public.
- Merge the pull request that adds the runtime code (`spike/`, `design/mockup/build.py`
  and `template.html`, `scripts/refresh_request_webapp/`, `tests/`, `requirements.txt`,
  `.claude/settings.json`, `README.md`, `.gitignore`) into the default branch,
  `claude/spikeball-finance-refresh-setup-giglgf`, which already holds the handoff
  packet documents and `HANDOFF-GAPS.md`.

**Success looks like**: the repository shows Private next to its name, and the default
branch's file listing includes `spike/routine/run_nightly.py`, `requirements.txt`, and
`.claude/settings.json` at the paths this prompt expects. Open `.claude/settings.json`
and confirm it has a `permissions.allow` list of `Bash(...)` prefix rules covering
every command `ROUTINE-PROMPT.md` runs (`nohup python3 spike/routine/run_nightly.py`,
`timeout`, `pip install`, `git`, `curl`, and each `python3 spike/routine/*.py`
invocation among them) -- it must not rely on an `autoMode` field of any kind, since
those are ignored when set inside a repository.

**If it fails**: if the merge has conflicts or fails checks, that is a code-review
matter for whoever owns the pull request, not something to work around here -- stop and
report which check failed. Do not proceed to step 2 with an unmerged pull request; the
routine has nothing to attach until this lands.

## 2. Environment: Setup script and the alert address

**Who**: Casandra, or the Claude Code session if the environment's fields are reachable
through its tools; otherwise a human, in the browser.
**Where**: claude.ai/code, environment selector, Cloud, the `Spikeball Finance`
environment's settings (`env_01XTN5CezsGWv8FTYZLEVn61`).

- Set the **Setup script** to:
  ```bash
  pip install -r requirements.txt
  ```
  A routine with a repository source provisions in a fixed order: the environment,
  then the repository checkout, then this Setup script, and only after that does the
  Claude Code session itself start, inside that checkout -- the permission classifier
  that gated the earlier zip design does not exist yet while the Setup script runs, so
  nothing here is subject to it. Whether the packages this installs persist into the
  session that follows is not documented, which is why `ROUTINE-PROMPT.md` step 3 runs
  `pip install -r requirements.txt` again regardless; expect that to be a fast no-op
  confirmation, not the first time dependencies are installed.
- Change the environment variable `SPIKEBALL_ALERT_TO` from its current value to
  `casandra@spikeball.com`. Today it points at an address outside this project; the
  failure alert should reach the person operating this routine.
- Leave every other environment variable as it is (network access stays `Full`; the 20
  existing variables, including `SPIKEBALL_NIGHTLY_SLOT_UTC=10` and
  `SPIKEBALL_ARTIFACT_URL=https://claude.ai/code/artifact/ddb97f42-87c0-416f-9a55-d114b69db878`,
  are already correct). `SPIKEBALL_DASH_BUNDLE_FILE_ID` is no longer read by anything;
  it is harmless to leave set and can be deleted whenever convenient.

**Success looks like**: the environment's settings page shows the Setup script saved
and `SPIKEBALL_ALERT_TO` reading `casandra@spikeball.com`.

**If it fails**: if the Setup script field or the variable editor rejects the change or
isn't visible, this environment's UI has changed since this checklist was written --
stop and report exactly what the settings page shows instead of guessing at a
workaround.

## 3. Routine: attach the repository as the source

**Who**: the Claude Code session, using its routine-management tool, if that tool
exposes a `sources` (repository) field; otherwise a human, in the routines UI.
**Where**: the session's own routine tool, or `https://claude.ai/code/routines`, the
`Spikeball Finance refresh` routine's edit screen.

- Attach `https://github.com/mcohen-spikeball/spikeball` as the routine's repository
  source, tracking its default branch.
- Set the routine's allowed tools to Bash and Artifact. `allowed_tools` is a genuine
  field of the routine's create/update body, at
  `job_config.ccr.session_context.allowed_tools` -- pass `["Bash", "Artifact"]` there
  if recreating or updating the routine through the session's own routine-management
  tool. If that tool rejects the field (the earlier gap analysis found it exposing only
  `name`, `cron`, `environment`, `prompt`, `model`, `enabled`), set it instead at
  `claude.ai/code/routines`, which does expose both the repository source and the
  permission settings.
- Keep the routine's cron, environment attachment (`Spikeball Finance`,
  `env_01XTN5CezsGWv8FTYZLEVn61`), and prompt text (`ROUTINE-PROMPT.md`, i.e. the
  repository's `spike/routine/PROMPT_gate.md`, verbatim) unchanged.
- If neither the tool nor the UI can add a repository source to an existing routine,
  delete this routine and recreate it with the same name, cron, environment, and
  prompt, selecting the repository at creation time instead.

**Success looks like**: the routine's configuration shows the repository attached as
its source, on the default branch, with `Bash` and `Artifact` (or whatever the UI calls
them) in its allowed tools.

**If it fails**: if recreating the routine is the only option, note the new routine's
id in place of `trig_012sYmV5ZRzkcaRvHgTcxgTc` above before continuing, since every
later reference to "the routine" in this checklist and in `VERIFY.md` means whichever
id is live now.

## 4. Run the routine once, from the routine itself

**Who**: the Claude Code session, using the routine tool's trigger/run action (or, if
that action isn't exposed, a human at `https://claude.ai/code/routines`, opening the
routine and starting a run from there).
**Where**: the routine, not an interactive session. Running the pipeline by hand again
proves nothing new -- the interactive run on 2026-09-08 already proved the pipeline
works. What is unproven is whether *this routine* can run, and that can only be shown
by triggering the routine itself and reading its own run log.

- Trigger a run. A remote routine session never shows a "trust this folder" prompt for
  its checkout, so there is nothing to click through or pre-approve for that; the only
  permission question that matters here is whether `.claude/settings.json`'s
  `permissions.allow` list covers the commands the run needs (step 1).
- Poll until it reaches a terminal state, then read its log.
- Open the "Spikeball Finance Data" Sheet's `run_log` tab. The newest row's `trigger`
  column tells the story:
  - `nightly` or `request`, with a verdict of `NIGHTLY_OK` or `NIGHTLY_PARTIAL_OK`:
    the routine ran the pipeline and it passed. This is unambiguous success.
  - No new row at all, and the routine's own log ends with `NIGHTLY_SKIP <reason>`:
    also fine -- this run landed outside the nightly slot with nothing queued, so the
    gate correctly did nothing. A skip does not, by itself, prove the routine can run
    the pipeline; it only proves the routine can reach the gate decision. The decisive
    check is a run that lands at or covers the 10:00 UTC nightly slot (see the
    Environment section of `ROUTINE-PROMPT.md` for the MT equivalents) actually
    writing a `trigger=nightly` row, or an on-demand request being honored (step 5).

**Success looks like**: either a fresh `run_log` row with verdict `NIGHTLY_OK` /
`NIGHTLY_PARTIAL_OK`, or a clean `NIGHTLY_SKIP <reason>` in the routine's own log with
no unhandled error -- and, either way, no `ENV_MISSING`, `ENV_BLOCKED`, or
`CHECKOUT_MISSING` line, which would mean steps 1 through 3 didn't fully land.

**If it fails**:
- `ENV_MISSING <names>`: the environment (step 2) is missing one of those exact
  variable names; add it and re-run this step.
- `ENV_BLOCKED: oauth2.googleapis.com unreachable`: the environment's network access
  isn't actually `Full`; fix it and re-run this step.
- `CHECKOUT_MISSING <paths>`: the repository source (step 3) isn't attached correctly,
  or the pull request (step 1) didn't fully merge what `ROUTINE-PROMPT.md` step 2
  expects. Recheck both before retrying.
- `NIGHTLY_FAIL <reason>`: something in the pipeline itself failed on a real attempt.
  Report the reason as-is; this is not something to retry or patch here.

## 5. Verify the refresh round trip

Run the checklist in `VERIFY.md` end to end, including the on-demand refresh request
and the `superseded` status on any stale queued row. This has already succeeded once
against the vendor's own environment (2026-09-08); the point of repeating it here is to
confirm it still works now that the routine runs from the repository checkout instead
of a downloaded bundle.

## 6. Later, not blocking

Two things are deliberately deferred and do not block calling this routine "working":

- `RUNBOOK-google-identity.md` -- moving the Google identity behind
  `SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN` from the owner to Casandra. The owner decided
  (2026-09-09) to leave this as-is for now.
- `CUTOVER.md` -- disabling the older "Spikeball Finance nightly refresh" routine and
  moving this routine's nightly slot from 10:00 UTC to 9:00 UTC. Do this once you've
  watched this routine succeed on its own for a few days.
