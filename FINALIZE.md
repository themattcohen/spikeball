# Finalize: Spikeball Finance dashboard on this account

The ordered checklist from "the routine runs unattended" (true since 2026-09-10) to "the
dashboard is in service for its readers". Work it in order. Steps marked **HUMAN** need a
person in a browser; everything else is for the Claude Code session working this file.
Times are Mountain Time (MT) with the UTC cron hour in parentheses where it matters.

## 0. What is proven, what is not

Proven, on the Sheet "Spikeball Finance Data" and the page itself:

- The routine `Spikeball Finance refresh` runs from the repository checkout with no
  permission denials (2026-09-09).
- An on-demand request was honored end to end: `refresh_requests` row 4 `honored`,
  `run_log` row 31 `trigger=request`, `all_pass=TRUE`, page republished (2026-09-09).
- The nightly slot fired by cron with nobody watching: `run_log` row 32 at 04:08 MT
  (10:08 UTC), `trigger=nightly`, `all_pass=TRUE`, page pulled_at matches (2026-09-10).
- Hourly fires outside the slot with nothing queued write nothing (`NIGHTLY_SKIP`).

Not yet done or not yet shown:

1. The readers (the CFO and CEO) can open the page.
2. A person has exercised the month range picker and the refresh link on the live page.
3. The CFO's input surface (the `Demand Plan` tab) flows into the page after an edit.
4. The older routine on the other account still pulls every night an hour before this
   one (`CUTOVER.md`).
5. The test suite runs clean on a fresh checkout (browser tests now skip cleanly without
   their extra dependency).

## 1. Run the tests on this checkout

```bash
pip install -r requirements-dev.txt
python3 -m patchright install chromium
python3 -m pytest tests -q
```

**Success looks like**: every test passes. With the browser install skipped, the
`tests/test_dashboard_range.py` module reports itself skipped and the rest still pass.
The gate tests pass with `SPIKEBALL_NIGHTLY_SLOT_UTC` set to 10 (this environment), 9,
or unset; they pin the variable themselves now.

**If it fails**: a failure in `tests/test_refresh_gate.py` is a real defect in the gate
or its tests; stop and report the test name and assertion. A collection error naming
`patchright` means `requirements-dev.txt` was not installed.

## 2. HUMAN: give the readers access to the page

The page at `SPIKEBALL_ARTIFACT_URL` is private to this account until shared.

- Open it, use its share control, and grant access to the CFO and the CEO. Prefer
  sharing with their accounts over an anyone-with-the-link setting; the page carries
  company financials.
- Ask one of them to open it and confirm they see the green freshness pill
  ("As of <yesterday's date> (MT), through yesterday's close").
- Tell them this is the address to bookmark. The page the older routine publishes
  stops updating at cutover (step 5) and must not be the one they keep.

**Success looks like**: a reader outside this account has opened the page and reported
the pill text. Record who and when in the sign-off (step 7).

## 3. HUMAN: exercise the page; the session verifies the Sheet side

Do `VERIFY.md` sections 1 and 2 in a browser. The specifics to check:

Month range picker:
- The preset buttons (MTD, YTD, 13 months) still work and the tiles change with each.
- Choose a custom range of at least two full months (for example March 2026 to May
  2026). The Sales, Gross profit and Gross margin tiles change, the range label shows
  the chosen months, and the channel table rows change with them.
- The life-to-date tile shows its "Not range-aware" caption; cash flow and balance
  sheet notes name the months actually available.
- The address bar hash changes with the range; reloading that address restores the
  same range. An out-of-range choice shows "No data for that range", not a blank page.

Refresh link:
- Click "Request data refresh". A new tab opens on the request endpoint and shows
  "Refresh requested at HH:MM MT" with the next-check wording. If the tab shows a
  Google "unable to open the file" page instead, the browser is signed into a Google
  account outside spikeball.com; retry in a private window or as a spikeball.com user.
- A second click within ten minutes shows "A refresh was already requested at ...";
  that is the rate limit working, not a failure.

Session side, after the click: read `refresh_requests`; the newest row is `queued` with
the click time. The next fire (hourly 07:00 to 17:00 MT, plus 18:00 MT and 04:00 MT)
turns it into `honored <pull time>` and appends a `run_log` row with `trigger=request`
and the row number in `request_row`. Then the page's pill shows the new pull time.

**Success looks like**: every bullet above observed, plus the `honored` row and the
matching `run_log` row.

**If it fails**: a request that stays `queued` past two fires means the gate did not see
it; check that the row's `requested_at_utc` parses (ISO, `Z` suffix) and that the run's
own log shows `NIGHTLY_SKIP` reasons, then report. A page that never changes its pill
after an `honored` row means the run published data but not the page; read the run log
for the Artifact step.

## 4. CFO input round trip: the `Demand Plan` tab

What it is: a tab named exactly `Demand Plan` in the Sheet "Spikeball Finance Data",
owned by the CFO. The pipeline reads it every run and never writes to it. Its header row
is the row whose first cell is `SKU`, with columns

```
SKU | Customer | Location | Unit Price | 2026-01 | 2026-02 | ... | 2026-12
```

Any rows above the header are a freeform note (for example "Last updated 9/10 by ...").
Month cells hold planned units. The page's "Demand vs actuals" section draws plan units
against shipped units by SKU and month (chart, per-SKU table, cost coverage table), and
the run's checks carry `demand_plan_ok`.

Before testing, **HUMAN**: confirm the CFO has edit access to the Sheet and can see the
`Demand Plan` tab. If the tab has no header row yet, seed it with the header above and
at least one SKU row before continuing.

The round trip:

1. Session: read the tab and pick one existing SKU row and one month column at or after
   the current month. Record the current value.
2. **HUMAN** (or the session, since reading and editing the Sheet uses the same
   credentials; a human edit is the truer test): change that cell by a distinctive
   amount, for example plus 7 units.
3. **HUMAN**: click "Request data refresh" on the page, or wait for the next scheduled
   fire (a run only happens at the nightly slot, after a queued request, or after a
   20-hour gap).
4. Session: after the `run_log` row appears, read the page's embedded data and confirm
   the plan value for that SKU and month moved by exactly the amount entered and the
   section's chart sub-caption still names the tab's note. Confirm the tab itself was
   not rewritten: the note row and every other cell are unchanged.
5. Restore the cell to its recorded value and let the next run pick it up, unless the
   CFO wants the new value kept.

Malformed input is handled, not fatal: a row with a blank SKU or a non-numeric month
cell is dropped and reported in the run's demand-plan output; a missing header or
month column makes the whole tab invalid for that run and the page shows the last
known-good plan labelled stale. Neither blanks the section.

**Success looks like**: the page's plan number for the edited SKU/month equals the
edited value after the run, and the tab is byte-for-byte what the human left.

**If it fails**: `demand_plan_ok` false in the run's checks means the tab failed schema
validation; report the reason the run's output gives (it names the missing header or
column). A page that shows the old value with `demand_plan_ok` true means the run read a
stale snapshot; check that the edit was saved before the run's pull time.

## 5. Retire the older routine (`CUTOVER.md`)

The older "Spikeball Finance nightly refresh" routine on the other account pulls at
03:05 MT (09:05 UTC); this one at 04:08 MT (10:08 UTC). Every night that both run is a
duplicated full extract writing the same Sheet.

Rule: cut over after two consecutive clean unattended nights from this routine. The
first was 2026-09-10; the second is the 2026-09-11 slot. The owner picks the day; do not
start the cutover without that ruling.

When ruled: follow `CUTOVER.md` steps 1 to 3 exactly. Step 1 (disable the older routine)
is **HUMAN** on the other account. Step 2 changes two things together on this account:
the cron `0 0,10,13-23 * * *` becomes `0 0,9,13-23 * * *` (the session can set cron) and
`SPIKEBALL_NIGHTLY_SLOT_UTC` becomes `9` (**HUMAN**, environment settings). Changing
one without the other leaves the guaranteed run and the cron out of alignment.

**Success looks like**: the next morning `run_log` has exactly one new nightly row, at
about 03:05 MT (09:05 UTC), `trigger=nightly`, and the page's pull time matches it.

## 6. Repository hygiene

- Merge the branch carrying `STATUS.md` once it names no person or address outside this
  project (it does not as of 2026-09-10) and its dates are current.
- Leave the routine's `outcomes` branch setting alone; it is inert.
- Do not add `patchright` or `pytest` to `requirements.txt`; they live in
  `requirements-dev.txt` so the routine's Setup script never installs them.

## 7. Sign-off report

Finish with one table, one row per step above: step, pass or fail, evidence (the
`run_log` row, the `refresh_requests` row, the page's pull time, the reader's name and
the time they confirmed, the Demand Plan cell and values before and after), and the
cutover day the owner ruled. Then the next scheduled fire time in MT.
