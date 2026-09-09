#!/usr/bin/env python3
"""On-demand refresh gate for the Spikeball Finance dashboard nightly routine
(PRD-month-refresh.md Section 5 M5, RC M5).

Decides whether a `run_nightly.py --gate` invocation should run the full pipeline or
skip, based on: a local lock file (a run already in progress or very recently
finished), the last successful `run_log` publish (Sheets tab `run_log`), and any queued
rows on the `refresh_requests` Sheet tab (Section 4) that a viewer's "Request data
refresh" click appended via the Apps Script endpoint (M3).

All Sheets I/O goes through `spike/google_auth.py`'s `authed_request()`, the same
pattern `spike/publish_sheet.py` uses -- plain `requests` REST calls, bearer token from
the Spikeball Google OAuth refresh token, no `google-api-python-client`. Never prints a
token.

A separate pure function, `stale_rows()`, identifies `queued` requests that `decide()`
will never honor (their `requested_at_utc` is at or before `last_success_utc`, so its
strictly-after comparison in step 4 below can never select them) -- `run_nightly.py`'s
`--gate` path marks these rows `superseded <now_mt_iso>` via `mark_status()` (the
general form behind `mark_honored()`) so they stop showing as perpetually `queued` on
the Sheet. This runs alongside `decide()`, not inside it: `decide()`'s three-tuple
return is unchanged.

Decision table (`decide()`, evaluated in the exact order below):
  1. a lock file (`spike/data/.gate.lock`) exists and is younger than 90 minutes
     -> skip GATE_BUSY (another gate invocation is presumed still running).
  2. `requests` is None (the caller's `read_requests()` call raised -- a Sheets
     transport error) -> run sheets_error (fail open: better to run an extra pipeline
     than to silently stop honoring requests because of a transient API error).
  3. `last_success_utc` is None (no prior successful `run_log` row, or it could not be
     read) -> run no_prior_success.
  4. any request with status `queued` and `requested_at_utc` strictly after
     `last_success_utc` -> run request:<comma-joined row numbers, ascending>.
  5. `now_utc.hour == nightly_slot_utc_hour()` (the nightly slot, default UTC 09:00 =
     03:00 MT, overridable via env SPIKEBALL_NIGHTLY_SLOT_UTC -- see PROMPT_gate.md)
     -> run nightly_slot.
  6. `now_utc - last_success_utc > 20h` AND (`last_attempt_utc` is None OR
     `now_utc - last_attempt_utc > 55min`) -> run stale_20h (a full day gone stale, but
     capped to at most one attempt per hour so a persistently-failing pipeline doesn't
     retry every 5-minute gate slot).
  7. else -> skip no_request.

`decide()` is pure (no I/O, no clock reads) so it is fully unit-testable (T9).
"""
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # .../spike, for google_auth
import google_auth  # noqa: E402

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

SPIKE = Path(__file__).resolve().parents[1]
GATE_LOCK_PATH = SPIKE / "data" / ".gate.lock"
GATE_ATTEMPT_PATH = SPIKE / "data" / ".gate.last_attempt"

LOCK_BUSY_MINUTES = 90
STALE_HOURS = 20
ATTEMPT_CAP_MINUTES = 55
NIGHTLY_SLOT_UTC_HOUR_DEFAULT = 9


def nightly_slot_utc_hour():
    """The nightly gate's UTC hour, from env SPIKEBALL_NIGHTLY_SLOT_UTC (int),
    defaulting to NIGHTLY_SLOT_UTC_HOUR_DEFAULT. Read at call time (not cached at
    import) so a routine's cron and this env var can be repointed together (see
    CUTOVER.md) without a code change, and so tests can monkeypatch the env. Falls back
    to the default on a missing, blank, or non-integer value; never raises."""
    raw = os.environ.get("SPIKEBALL_NIGHTLY_SLOT_UTC")
    if raw is None or not raw.strip():
        return NIGHTLY_SLOT_UTC_HOUR_DEFAULT
    try:
        return int(raw.strip())
    except ValueError:
        return NIGHTLY_SLOT_UTC_HOUR_DEFAULT


# ---------------------------------------------------------------------------
# Lock and attempt marker (mtime based; spike/data/.gate.lock, spike/data/.gate.last_attempt)
# ---------------------------------------------------------------------------

def lock_age_minutes():
    """Age of `spike/data/.gate.lock` in minutes (float), or None if the file is
    absent. Used by `decide()` step 1 (GATE_BUSY)."""
    try:
        mtime = GATE_LOCK_PATH.stat().st_mtime
    except OSError:
        return None
    return (time.time() - mtime) / 60.0


def acquire_lock():
    """Creates (or refreshes the mtime of) `spike/data/.gate.lock`, marking a gate run
    in progress. Called only on the "run" path, never on skip."""
    GATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    GATE_LOCK_PATH.touch(exist_ok=True)
    os.utime(GATE_LOCK_PATH, None)


def release_lock():
    """Removes `spike/data/.gate.lock` if present. Safe to call even if it was never
    acquired (e.g. an early crash before `acquire_lock()`)."""
    try:
        GATE_LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def touch_attempt():
    """Creates (or refreshes the mtime of) `spike/data/.gate.last_attempt`, marking the
    moment a gate run last actually ran the pipeline. Called only on the "run" path.
    Feeds `decide()` step 6's 55-minute attempt cap via `read_last_attempt_utc()`."""
    GATE_ATTEMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    GATE_ATTEMPT_PATH.touch(exist_ok=True)
    os.utime(GATE_ATTEMPT_PATH, None)


def read_last_attempt_utc():
    """Returns `spike/data/.gate.last_attempt`'s mtime as an aware UTC datetime, or
    None if the file is absent. Read-only counterpart to `touch_attempt()`, used to
    build the `last_attempt_utc` argument `decide()` needs for its 55-minute cap."""
    try:
        mtime = GATE_ATTEMPT_PATH.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _truthy(cell):
    """Loose truthiness for a Sheets cell that should hold a boolean: the Sheets API
    returns a native JSON boolean for a cell written as one (publish_sheet.cell_value
    passes Python bool straight through), but tolerates a string form too (defensive,
    in case a value ever gets typed in the Sheet UI by hand)."""
    if isinstance(cell, bool):
        return cell
    if isinstance(cell, (int, float)):
        return bool(cell)
    if isinstance(cell, str):
        return cell.strip().upper() in ("TRUE", "1", "YES")
    return False


def _parse_offset_iso_to_utc(s):
    """Parses an ISO-8601 timestamp that carries its own UTC offset (the shape
    `now_mt_iso()` in publish_sheet.py produces, e.g. '2026-09-08T03:15:22-06:00') into
    an aware UTC datetime. Returns None when unparseable or when the string has no
    offset/timezone at all (never guesses one)."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _parse_utc_z(s):
    """Parses the `requested_at_utc` shape from Section 4 ('YYYY-MM-DDTHH:MM:SSZ') into
    an aware UTC datetime. Returns None when unparseable."""
    if not isinstance(s, str) or not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") or s.endswith("z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Sheets reads / writes
# ---------------------------------------------------------------------------

def read_run_log_last_success(sheet_id):
    """GET 'run_log'!A:C (pulled_at_mt, asof_date, all_pass), same request pattern as
    publish_sheet.write_run_log. Returns the `pulled_at_mt` of the last (bottom-most)
    row whose `all_pass` is truthy, converted to an aware UTC datetime -- or None when
    the tab is empty, unreadable (any transport error), or that row's `pulled_at_mt`
    cannot be parsed. Feeds `decide()` step 3 (no_prior_success) and step 6
    (stale_20h)."""
    rng = urllib.parse.quote("'run_log'!A:C", safe="")
    try:
        resp = google_auth.authed_request("GET", f"{SHEETS_BASE}/{sheet_id}/values/{rng}")
    except google_auth.GoogleAuthError:
        return None
    if resp.status_code != 200:
        return None
    values = resp.json().get("values") or []
    if len(values) < 2:  # header only, or empty
        return None
    last_pulled_at_mt = None
    for row in values[1:]:
        if len(row) < 3:
            continue
        if _truthy(row[2]):
            last_pulled_at_mt = row[0]
    if not last_pulled_at_mt:
        return None
    return _parse_offset_iso_to_utc(last_pulled_at_mt)


def read_requests(sheet_id):
    """GET 'refresh_requests'!A:E (requested_at_utc, requested_at_mt, source,
    user_agent, status; Section 4 header). Returns
    [(row_index: int, requested_at_utc: aware UTC datetime, status: str)] for every
    well-formed data row (sheet row 1 is the header, so the first data row is index 2).
    A row with fewer than 5 populated columns, or whose requested_at_utc does not
    parse, or whose status is blank, is malformed: skipped, and the count is printed as
    a diagnostic (never raised for a malformed row). Raises RuntimeError on a genuine
    transport error (non-200 response) -- callers pass `requests=None` to `decide()` in
    that case (`decide()` step 2, sheets_error)."""
    rng = urllib.parse.quote("'refresh_requests'!A:E", safe="")
    resp = google_auth.authed_request("GET", f"{SHEETS_BASE}/{sheet_id}/values/{rng}")
    if resp.status_code != 200:
        raise RuntimeError(f"read refresh_requests HTTP {resp.status_code}: {resp.text[:500]}")
    values = resp.json().get("values") or []
    out = []
    malformed = 0
    for i, row in enumerate(values[1:], start=2):  # sheet row 1 is the header
        if len(row) < 5:
            malformed += 1
            continue
        requested_at_utc_raw, _requested_at_mt, _source, _user_agent, status = row[:5]
        dt = _parse_utc_z(requested_at_utc_raw)
        status = str(status).strip()
        if dt is None or not status:
            malformed += 1
            continue
        out.append((i, dt, status))
    if malformed:
        print(f"[refresh_gate] skipped {malformed} malformed refresh_requests row(s)")
    return out


def mark_status(sheet_id, row_indexes, status_text):
    """PUTs 'refresh_requests'!E<row> = status_text for each row index (valueInputOption
    RAW), one request per row (append-only tab, so a row index captured at read time
    stays valid -- Section 4). No-op when row_indexes is empty. General form behind
    `mark_honored()` (status 'honored <pulled_at_mt>') and the --gate path's
    'superseded <now_mt_iso>' write for `stale_rows()`."""
    for row in row_indexes:
        rng = urllib.parse.quote(f"'refresh_requests'!E{row}", safe="")
        resp = google_auth.authed_request(
            "PUT", f"{SHEETS_BASE}/{sheet_id}/values/{rng}",
            params={"valueInputOption": "RAW"},
            json={"values": [[status_text]]},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"mark_status PUT row {row} HTTP {resp.status_code}: {resp.text[:500]}")


def mark_honored(sheet_id, row_indexes, pulled_at_mt):
    """PUTs 'refresh_requests'!E<row> = 'honored <pulled_at_mt>' for each row index.
    Thin wrapper around `mark_status()`."""
    mark_status(sheet_id, row_indexes, f"honored {pulled_at_mt}")


# ---------------------------------------------------------------------------
# Pure decision
# ---------------------------------------------------------------------------

def decide(now_utc, requests, last_success_utc, last_attempt_utc, lock_age_min):
    """Pure. Returns (verdict, reason, honored_rows):
      verdict       "run" or "skip"
      reason        one of GATE_BUSY, sheets_error, no_prior_success,
                     "request:<rows>", nightly_slot, stale_20h, no_request
      honored_rows  list[int] of refresh_requests row indexes this run should mark
                     honored (non-empty only for the "request:<rows>" reason)

    `requests` is None to signal that the caller's `read_requests()` call raised (a
    Sheets transport error) -- NOT the same as an empty list, which means the read
    succeeded and found zero rows. See the module docstring for the full table."""
    if lock_age_min is not None and lock_age_min < LOCK_BUSY_MINUTES:
        return "skip", "GATE_BUSY", []

    if requests is None:
        return "run", "sheets_error", []

    if last_success_utc is None:
        return "run", "no_prior_success", []

    honored = sorted(
        row for (row, requested_at_utc, status) in requests
        if status == "queued" and requested_at_utc > last_success_utc
    )
    if honored:
        return "run", "request:" + ",".join(str(r) for r in honored), honored

    if now_utc.hour == nightly_slot_utc_hour():
        return "run", "nightly_slot", []

    if now_utc - last_success_utc > timedelta(hours=STALE_HOURS):
        if last_attempt_utc is None or (now_utc - last_attempt_utc) > timedelta(minutes=ATTEMPT_CAP_MINUTES):
            return "run", "stale_20h", []

    return "skip", "no_request", []


def stale_rows(requests, last_success_utc):
    """Pure. Returns a sorted list[int] of refresh_requests row indexes whose status is
    'queued' and whose requested_at_utc is at or before last_success_utc -- these
    requests were queued before (or at the exact moment of) the last successful run,
    so they are already covered by it, and `decide()`'s step 4 (strictly `>
    last_success_utc`) will never honor them: left alone they would stay 'queued'
    forever. `run_nightly.py`'s --gate path marks them 'superseded <now_mt_iso>' via
    `mark_status()` instead.

    Kept separate from `decide()` rather than folded into its return (a fourth tuple
    element) so `decide()`'s existing three-tuple return stays backward compatible.

    `requests` is the same [(row, requested_at_utc, status)] shape `decide()` takes;
    None (the caller's read_requests() raised) yields []. Returns [] when
    last_success_utc is None (nothing to compare against -- there is no prior success
    for a request to be stale relative to)."""
    if requests is None or last_success_utc is None:
        return []
    return sorted(
        row for (row, requested_at_utc, status) in requests
        if status == "queued" and requested_at_utc <= last_success_utc
    )
