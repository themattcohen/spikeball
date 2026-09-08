# Spikeball Finance handoff — open gaps

Written by the setup session on 2026-09-08, for whoever picks this up next.
Everything here is something the setup could **not** verify, could **not** configure,
or that the packet documents inconsistently. It is not a list of failures — the
routine itself is set up. It is the list of things still resting on assumptions.

Routine: `Spikeball Finance refresh` — `trig_012sYmV5ZRzkcaRvHgTcxgTc`
Environment: `Spikeball Finance` — `env_01XTN5CezsGWv8FTYZLEVn61`

---

## 1. Things the setup session could not verify itself

### 1a. Network access is `Full` — taken on trust
`list_environments` returns only id, name, description, state and kind. It does **not**
return the network-access mode. The setup asked the human and they confirmed `Full`,
but no tool available to this session can check it independently.

**Consequence if wrong:** the routine's own step 1 catches it and prints
`ENV_BLOCKED: oauth2.googleapis.com unreachable`, so it fails loudly rather than
silently. Low risk, but it is an unverified assumption, not a checked fact.

**To close:** confirm visually in the environment's settings at claude.ai/code.

### 1b. The credentials block is complete — taken on trust
Environment variable *values* are not readable through any tool here (correctly so).
The human confirmed they pasted the full block. The routine's step 1 only sentinel-checks
**two** of the ~20 names: `NETSUITE_ACCOUNT_ID` and `SPIKEBALL_OAUTH_CLIENT_ID`.

**Consequence:** a missing `SP_API_REFRESH_TOKEN_EU`, `SPIKEBALL_DASH_STATE_FILE_ID`,
`SPIKEBALL_ALERT_TO`, etc. sails past step 1 and surfaces later as a `NIGHTLY_FAIL` or
`NIGHTLY_PARTIAL_OK` deep in the pipeline, with a less obvious error.

**To close:** eyeball the environment's variable list against `ROUTINE-PROMPT.md`'s
Environment section — all of: the 5 `NETSUITE_*`, the 3 `SPIKEBALL_OAUTH_*`/`GCP_*`,
`SPIKEBALL_ALERT_TO`, `SPIKEBALL_FINANCE_SHEET_ID`, `SPIKEBALL_DASH_BUNDLE_FILE_ID`,
`SPIKEBALL_DASH_STATE_FILE_ID`, `SPIKEBALL_REFRESH_REQUEST_URL`,
`SPIKEBALL_LOOKER_REPORT_URL`, the 4 `SP_API_*`, `SPIKEBALL_DASH_FEATURES`,
`SPIKEBALL_NIGHTLY_SLOT_UTC`, and (after first run) `SPIKEBALL_ARTIFACT_URL`.

### 1c. `SPIKEBALL_NIGHTLY_SLOT_UTC` is set to `10` — unverified
The whole no-collision-with-the-old-routine design depends on this being `10` while the
old routine runs at UTC 9. Nothing in the setup could read it back. If it is unset or
wrong, the nightly-slot rule misfires: either no run is ever treated as the guaranteed
nightly, or it collides with the older routine's hour.

**To close:** confirm it reads exactly `10` in the environment's variables.

---

## 2. Things the tooling could not configure

### 2a. `allowed_tools` could not be set to `["Bash", "Artifact"]`
The routine-creation tool available here (`create_trigger`) exposes name, cron,
environment, prompt, and (via a follow-up update) model. It has **no** parameter for
`allowed_tools`, `disallowed_tools`, or `sources`. The created routine has
`allowed_tools: []`, which means *default tool set*, not *Bash and Artifact only*.

The human was asked and chose to proceed. Recorded here because it is a real deviation
from the spec:

- **What is lost:** a structural guardrail. `ROUTINE-PROMPT.md` step 5 explicitly forbids
  `ScheduleWakeup`, `Monitor`, and background mechanisms; with the tool restriction in
  place that would have been impossible rather than merely forbidden. Now it rests on
  the prompt alone.
- **Also lost:** the narrower blast radius of a credential-bearing unattended session
  that can only shell out and publish.

**To close:** check whether the routines UI at claude.ai/code/routines exposes an
allowed-tools field, and if so set it to Bash + Artifact. If it does not, this is a
platform gap worth reporting.

### 2b. `model` needed a second call, and is fragile
`create_trigger` has no `model` parameter; the routine was created with an empty model
and then set to `claude-sonnet-5` via `update_trigger`. It is correct now (verified in
the response). But anyone who recreates this routine from the spec via the same tool
will get the default model unless they remember the second call.

### 2c. No MCP connectors are attached
The create call warned: *"this trigger stores no MCP connectors, so the sessions it
fires will run without connector (`mcp__<server>__*`) tools."* This is fine — the
pipeline uses raw HTTPS via Python, not connectors — but worth knowing that the fired
sessions genuinely cannot reach Google/NetSuite through any connector path, only
through the credentials in the environment.

### 2d. The run log is not readable from this session
There is no tool here to read a fired routine session's transcript or run log.
`get_session` returns the session *record* (status, model, timestamps) and nothing else.
`ROUTINE-PROMPT.md`'s verdict lines are printed *inside* that session, so verifying a
run means opening claude.ai/code/routines in a browser as the account owner.

**Consequence for future automation:** an agent cannot self-verify a routine run here.
Any "did the nightly work?" check has to be done by a human, or by reading the Sheet's
`run_log` tab (which is the better programmatic path anyway).

---

## 3. Inconsistencies inside the packet itself

These are places where two packet documents disagree, or where a document describes a
world the new owner does not actually have access to.

### 3a. "Secrets store" vs. environment variables
`ROUTINE-PROMPT.md` is emphatic: *"Nothing is read from a secrets service; the variable
is simply present or it is not."* But `OPERATIONS.md` says:

- *"Alert address: `SPIKEBALL_ALERT_TO` in the secrets store (currently the owner)."*
- *"Manual refresh: from the code repository root, **with the secrets store token
  available**."*
- *"run `spike/routine/publish_bundle.py` (with the secrets store available)."*

`OPERATIONS.md` appears to be written from the *original owner's* workstation setup,
where a secrets store existed. On this account there is no secrets store — only the
cloud environment's variables. The routine path is fine; the **manual/maintenance paths
in `OPERATIONS.md` do not apply as written**.

### 3b. The new owner cannot deploy a code change
`OPERATIONS.md`'s "Deploying a code change" and several fixes assume access to the code
repository:

- *"add it to `spike/config/rollups.json` in the code repository"* (new NetSuite channel id)
- *"Set `features.amazon_aov` to true in `spike/config/rollups.json`"*
- *"run `python spike/routine/publish_bundle.py`"* to push a new bundle to Drive

This session has GitHub scope limited to `mcohen-spikeball/spikeball` and the dashboard
code is **not** in this working directory — the routine downloads it as a zip from
mcohen@spikeball.com's Drive. So today, **any code change still routes through the
original owner.** That is arguably the largest remaining dependency on "the vendor's
infrastructure" that `README-HANDOFF.md` says the packet removes.

**To close:** decide who owns the code repo and the bundle-publishing step going
forward, and get that person the repo, `publish_bundle.py`, and whatever
`publish_bundle.py` needs for credentials.

### 3c. Google identity is still the previous owner
`OPERATIONS.md`: *"the refresh runs as mcohen@spikeball.com through a one-time consent.
If that account's password or security settings change and the token is revoked, re-run
`spike/routine/google_consent.py` and approve once."*

So `SPIKEBALL_GCP_OAUTH_REFRESH_TOKEN` is bound to mcohen@spikeball.com, not to the
account now owning the routine. **If that person leaves or their token is revoked, this
dashboard stops**, and recovery requires `google_consent.py` — which lives in the code
repo (see 3b) — plus a browser consent as that Google user.

**To close:** either re-mint the refresh token as an account that will outlive the
handoff (ideally a service account or a shared finance mailbox), or make sure whoever
owns this knows the recovery runbook and has repo access.

### 3d. Alert email probably still goes to the previous owner
`OPERATIONS.md`: *"Alert address: `SPIKEBALL_ALERT_TO` ... (currently the owner).
Cutting over to Casandra is a one-value change."* If the pasted credentials block came
from the original owner unchanged, failure alerts are still going to them, not to the
new owner. This session cannot read the value to check.

**To close:** set `SPIKEBALL_ALERT_TO` to the address that should actually get woken up
by a `NIGHTLY_FAIL`.

### 3e. Cutover MT times are stated two different ways
`CUTOVER.md` step 3 says the post-cutover nightly is *"03:00 MT during MDT, 02:00 MT
during MST."* That is consistent with moving the slot from UTC 10 → 9 (MDT: UTC-6 → 03:00;
MST: UTC-7 → 02:00). Just note it against `README-HANDOFF.md`, which frames the older
routine's slot as "03:00 MT" without the MST qualifier. Not a bug — but if someone
compares the two docs while half-awake at 2am, the numbers look like they disagree.

---

## 4. Operational things nobody has proven yet

These are not setup defects; they are simply untested as of handoff.

- **The on-demand refresh round trip has not been exercised.** `VERIFY.md` §2 (click
  "Request data refresh" → a `queued` row appears in the Sheet's `refresh_requests` tab
  → a later run flips it to `honored`) requires a human with the Sheet open, across two
  scheduled slots. Until someone does it, "refresh on request" is a design, not a
  demonstrated behavior.
- **Balance-sheet anchor is bootstrapped from the June workbook.** `OPERATIONS.md` says
  the balance sheet is exact through June and **provisional after**, and that refreshing
  the anchor needs a NetSuite UI session **with the owner's 2FA** (`spike/config/bs_anchor.json`,
  NetSuite Balance Sheet report cr=-202). That is another standing dependency on the
  previous owner, and it silently degrades accuracy the further we get from June.
- **Two routines are writing to the same Sheet, BigQuery dataset and Drive state file**
  until cutover. The hour-apart scheduling makes this safe, but it also means
  `SPIKEBALL_NIGHTLY_SLOT_UTC` (see 1c) and the cron's `10` are load-bearing safety
  settings, not preferences. Change one without the other and you can get two runs in
  the same hour, which `CUTOVER.md` warns can revert the Amazon order watermark.
- **Nobody has watched a scheduled (non-forced) run yet.** The setup fired the routine
  manually. The first genuinely scheduled firing is the real test of the cron.

---

## 5. Suggested next actions, in order

1. Eyeball the environment's variable list against §1b, and fix `SPIKEBALL_ALERT_TO`
   (§3d) and confirm `SPIKEBALL_NIGHTLY_SLOT_UTC=10` (§1c) while you are in there.
2. Walk `VERIFY.md` end to end, including the refresh round trip (§4).
3. Decide the ownership question in §3b/§3c — code repo, bundle publishing, and the
   Google identity behind the refresh token. This is the one that turns into an outage
   later if left alone.
4. Only then consider `CUTOVER.md`.
