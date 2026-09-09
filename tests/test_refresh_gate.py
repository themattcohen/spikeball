"""PRD-month-refresh.md Section 7 T9, T9b, T9c, T10 -- the on-demand refresh gate
(Section 5 M5): `spike/routine/refresh_gate.py`'s pure `decide()` table, its Sheets
read/write helpers against a stubbed backend, `publish_sheet.write_run_log`'s header
extension, and a `run_nightly.py --gate` integration run with every Sheets call and
Doppler/network touch stubbed out.

Also covers the handoff-design.md client-cutover changes: `doppler_env.ensure_loaded()`
env-first (Deliverable 1 -- no Doppler CLI/API call, no DOPPLER_TOKEN, when the
sentinels are already in os.environ) and `refresh_gate.nightly_slot_utc_hour()`'s
SPIKEBALL_NIGHTLY_SLOT_UTC override (Deliverable 2).

Run: python -m pytest tests/test_refresh_gate.py -q
"""
import os

# Set BEFORE importing run_nightly: spike/routine/alert.py (imported by run_nightly.py)
# calls doppler_env.ensure_loaded() at IMPORT TIME, as a convenience for standalone
# invocations of alert.py itself. Pre-seeding doppler_env's two "already loaded"
# sentinel env vars here makes that call a same-process no-op (no subprocess, no
# network) without touching alert.py or doppler_env.py, neither of which this build
# owns. The values are inert placeholders never used to make a real API call --every
# Sheets/Google call these tests exercise is monkeypatched.
os.environ.setdefault("NETSUITE_ACCOUNT_ID", "test-sentinel-account")
os.environ.setdefault("SPIKEBALL_OAUTH_CLIENT_ID", "000000000000-test-sentinel.apps.googleusercontent.com")

import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

import doppler_env
import publish_sheet
import refresh_gate
import run_nightly


# ---------------------------------------------------------------------------
# Shared fakes / helpers
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.text = text

    def json(self):
        return self._json


def _utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def _snapshot_dir(path):
    """{relative path: mtime} for every file under `path`, recursively. Empty dict if
    `path` doesn't exist."""
    snap = {}
    if not path.is_dir():
        return snap
    for p in sorted(path.rglob("*")):
        if p.is_file():
            snap[str(p.relative_to(path))] = p.stat().st_mtime
    return snap


# ---------------------------------------------------------------------------
# T9: decide() table-driven, every row of PRD-month-refresh.md Section 5's table
# ---------------------------------------------------------------------------

NOW = _utc(2026, 9, 8, 15, 0, 0)          # 15:00 UTC -- not the 09:00 nightly slot
RECENT_SUCCESS = NOW - timedelta(hours=2)  # well under the 20h stale threshold
STALE_SUCCESS = NOW - timedelta(hours=21)  # past the 20h stale threshold


def test_decide_lock_busy_blocks_everything():
    verdict, reason, honored = refresh_gate.decide(NOW, [], RECENT_SUCCESS, None, 45.0)
    assert (verdict, reason, honored) == ("skip", "GATE_BUSY", [])


def test_decide_lock_age_at_90min_boundary_no_longer_busy():
    # 90 minutes exactly is NOT "< 90", so the lock no longer blocks. Use the nightly
    # slot hour so the rest of the table can only produce one unambiguous verdict.
    now_9 = _utc(2026, 9, 8, 9, 0, 0)
    verdict, reason, honored = refresh_gate.decide(now_9, [], RECENT_SUCCESS, None, 90.0)
    assert (verdict, reason, honored) == ("run", "nightly_slot", [])


def test_decide_lock_none_not_busy():
    now_9 = _utc(2026, 9, 8, 9, 0, 0)
    verdict, reason, honored = refresh_gate.decide(now_9, [], RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "nightly_slot", [])


def test_decide_sheets_error_when_requests_is_none():
    # requests=None is the caller's signal that read_requests() raised.
    verdict, reason, honored = refresh_gate.decide(NOW, None, RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "sheets_error", [])


def test_decide_sheets_error_from_a_raising_read_requests():
    """Exercises the actual integration shape: a read_requests() that raises gets
    caught by the caller and translated to requests=None before calling decide()."""
    def raising_read_requests(_sheet_id):
        raise RuntimeError("simulated Sheets transport error")

    try:
        requests_ = raising_read_requests("SHEETID")
    except Exception:  # noqa: BLE001
        requests_ = None

    verdict, reason, honored = refresh_gate.decide(NOW, requests_, RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "sheets_error", [])


def test_decide_no_prior_success():
    verdict, reason, honored = refresh_gate.decide(NOW, [], None, None, None)
    assert (verdict, reason, honored) == ("run", "no_prior_success", [])


def test_decide_no_prior_success_beats_a_queued_request():
    # PRD order: step 3 (no_prior_success) is checked before step 4 (request).
    requests_ = [(2, NOW - timedelta(minutes=5), "queued")]
    verdict, reason, honored = refresh_gate.decide(NOW, requests_, None, None, None)
    assert (verdict, reason, honored) == ("run", "no_prior_success", [])


def test_decide_single_queued_request_honored():
    requests_ = [(2, RECENT_SUCCESS + timedelta(minutes=10), "queued")]
    verdict, reason, honored = refresh_gate.decide(NOW, requests_, RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "request:2", [2])


def test_decide_multiple_queued_requests_honored_sorted_ascending():
    requests_ = [
        (5, RECENT_SUCCESS + timedelta(minutes=10), "queued"),
        (2, RECENT_SUCCESS + timedelta(minutes=20), "queued"),
    ]
    verdict, reason, honored = refresh_gate.decide(NOW, requests_, RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "request:2,5", [2, 5])


def test_decide_request_at_or_before_last_success_not_honored():
    requests_ = [(2, RECENT_SUCCESS, "queued"), (3, RECENT_SUCCESS - timedelta(minutes=1), "queued")]
    verdict, reason, honored = refresh_gate.decide(NOW, requests_, RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("skip", "no_request", [])


def test_decide_already_honored_request_not_re_honored():
    requests_ = [(2, RECENT_SUCCESS + timedelta(minutes=10), "honored 2026-09-08T08:00:00-06:00")]
    verdict, reason, honored = refresh_gate.decide(NOW, requests_, RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("skip", "no_request", [])


def test_decide_nightly_slot():
    now_9 = _utc(2026, 9, 8, 9, 0, 0)
    verdict, reason, honored = refresh_gate.decide(now_9, [], RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "nightly_slot", [])


def test_decide_nightly_slot_beats_stale_20h():
    now_9 = _utc(2026, 9, 8, 9, 0, 0)
    verdict, reason, honored = refresh_gate.decide(now_9, [], STALE_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "nightly_slot", [])


def test_decide_stale_20h_no_prior_attempt():
    verdict, reason, honored = refresh_gate.decide(NOW, [], STALE_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "stale_20h", [])


def test_decide_stale_20h_attempt_30min_ago_capped_skip():
    last_attempt = NOW - timedelta(minutes=30)
    verdict, reason, honored = refresh_gate.decide(NOW, [], STALE_SUCCESS, last_attempt, None)
    assert (verdict, reason, honored) == ("skip", "no_request", [])


def test_decide_stale_20h_attempt_60min_ago_runs():
    last_attempt = NOW - timedelta(minutes=60)
    verdict, reason, honored = refresh_gate.decide(NOW, [], STALE_SUCCESS, last_attempt, None)
    assert (verdict, reason, honored) == ("run", "stale_20h", [])


def test_decide_stale_20h_attempt_exactly_55min_not_yet():
    # Strictly "> 55 minutes" required, not ">=".
    last_attempt = NOW - timedelta(minutes=55)
    verdict, reason, honored = refresh_gate.decide(NOW, [], STALE_SUCCESS, last_attempt, None)
    assert (verdict, reason, honored) == ("skip", "no_request", [])


def test_decide_not_stale_not_nightly_no_request():
    verdict, reason, honored = refresh_gate.decide(NOW, [], RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("skip", "no_request", [])


# ---------------------------------------------------------------------------
# stale_rows(): queued requests decide() will never honor (handoff-design.md /
# client-repo cutover build, Deliverable 4)
# ---------------------------------------------------------------------------

def test_stale_rows_older_than_last_success_is_stale():
    requests_ = [(2, RECENT_SUCCESS - timedelta(minutes=10), "queued")]
    assert refresh_gate.stale_rows(requests_, RECENT_SUCCESS) == [2]


def test_stale_rows_equal_to_last_success_is_stale():
    # decide() step 4 requires strictly-after, so exactly-equal will never be
    # honored either -- stale_rows() must catch this boundary too.
    requests_ = [(3, RECENT_SUCCESS, "queued")]
    assert refresh_gate.stale_rows(requests_, RECENT_SUCCESS) == [3]


def test_stale_rows_newer_than_last_success_not_stale():
    requests_ = [(4, RECENT_SUCCESS + timedelta(minutes=1), "queued")]
    assert refresh_gate.stale_rows(requests_, RECENT_SUCCESS) == []


def test_stale_rows_last_success_none_nothing_stale():
    requests_ = [(2, RECENT_SUCCESS - timedelta(minutes=10), "queued")]
    assert refresh_gate.stale_rows(requests_, None) == []


def test_stale_rows_requests_none_nothing_stale():
    # Same sentinel decide() uses for a Sheets transport error.
    assert refresh_gate.stale_rows(None, RECENT_SUCCESS) == []


def test_stale_rows_ignores_non_queued_status():
    requests_ = [(2, RECENT_SUCCESS - timedelta(minutes=10), "honored 2026-09-08T08:00:00-06:00")]
    assert refresh_gate.stale_rows(requests_, RECENT_SUCCESS) == []


def test_stale_rows_sorted_ascending():
    requests_ = [
        (5, RECENT_SUCCESS - timedelta(minutes=5), "queued"),
        (2, RECENT_SUCCESS - timedelta(minutes=10), "queued"),
    ]
    assert refresh_gate.stale_rows(requests_, RECENT_SUCCESS) == [2, 5]


def test_stale_rows_mixed_stale_and_honorable():
    requests_ = [
        (2, RECENT_SUCCESS - timedelta(minutes=10), "queued"),      # stale
        (3, RECENT_SUCCESS + timedelta(minutes=10), "queued"),      # honorable, not stale
    ]
    assert refresh_gate.stale_rows(requests_, RECENT_SUCCESS) == [2]


def test_lock_and_attempt_helpers(tmp_path, monkeypatch):
    lock_path = tmp_path / ".gate.lock"
    attempt_path = tmp_path / ".gate.last_attempt"
    monkeypatch.setattr(refresh_gate, "GATE_LOCK_PATH", lock_path)
    monkeypatch.setattr(refresh_gate, "GATE_ATTEMPT_PATH", attempt_path)

    assert refresh_gate.lock_age_minutes() is None
    assert refresh_gate.read_last_attempt_utc() is None

    refresh_gate.acquire_lock()
    assert lock_path.is_file()
    age = refresh_gate.lock_age_minutes()
    # Filesystem mtime resolution can round to a hair before time.time()'s own
    # sample, so tolerate a small negative age rather than requiring exactly >= 0.
    assert age is not None and -1 < age < 1

    refresh_gate.touch_attempt()
    assert attempt_path.is_file()
    last_attempt = refresh_gate.read_last_attempt_utc()
    assert last_attempt is not None
    assert (datetime.now(timezone.utc) - last_attempt) < timedelta(minutes=1)

    refresh_gate.release_lock()
    assert not lock_path.exists()
    refresh_gate.release_lock()  # idempotent -- no error when already absent


# ---------------------------------------------------------------------------
# read_requests() and mark_honored() against a stubbed Sheets backend
# ---------------------------------------------------------------------------

def test_read_requests_raises_on_transport_error(monkeypatch):
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         lambda method, url, **kw: FakeResponse(500, {}, text="server error"))
    with pytest.raises(RuntimeError):
        refresh_gate.read_requests("SHEETID")


def test_read_requests_parses_well_formed_and_skips_malformed(monkeypatch, capsys):
    rows = [
        ["requested_at_utc", "requested_at_mt", "source", "user_agent", "status"],
        ["2026-09-08T14:00:00Z", "2026-09-08T08:00:00-06:00", "web", "UA/1.0", "queued"],
        ["not-a-timestamp", "x", "web", "UA/1.0", "queued"],       # malformed: bad date
        ["2026-09-08T15:00:00Z", "2026-09-08T09:00:00-06:00"],    # malformed: too few columns
    ]
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         lambda method, url, **kw: FakeResponse(200, {"values": rows}))
    result = refresh_gate.read_requests("SHEETID")
    assert result == [(2, refresh_gate._parse_utc_z("2026-09-08T14:00:00Z"), "queued")]
    assert "skipped 2 malformed" in capsys.readouterr().out


def test_mark_honored_puts_status_per_row(monkeypatch):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, urllib.parse.unquote(url), kwargs))
        return FakeResponse(200, {})

    monkeypatch.setattr(refresh_gate.google_auth, "authed_request", fake)
    refresh_gate.mark_honored("SHEETID", [2, 5], "2026-09-08T09:15:00-06:00")

    assert len(calls) == 2
    assert calls[0][0] == "PUT" and "refresh_requests'!E2" in calls[0][1]
    assert calls[0][2]["json"]["values"] == [["honored 2026-09-08T09:15:00-06:00"]]
    assert calls[1][0] == "PUT" and "refresh_requests'!E5" in calls[1][1]


def test_mark_honored_raises_on_transport_error(monkeypatch):
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         lambda method, url, **kw: FakeResponse(403, {}, text="forbidden"))
    with pytest.raises(RuntimeError):
        refresh_gate.mark_honored("SHEETID", [2], "2026-09-08T09:15:00-06:00")


# ---------------------------------------------------------------------------
# mark_status(): general form behind mark_honored(), also used for 'superseded ...'
# ---------------------------------------------------------------------------

def test_mark_status_puts_given_text_per_row(monkeypatch):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, urllib.parse.unquote(url), kwargs))
        return FakeResponse(200, {})

    monkeypatch.setattr(refresh_gate.google_auth, "authed_request", fake)
    refresh_gate.mark_status("SHEETID", [2, 5], "superseded 2026-09-08T09:15:00-06:00")

    assert len(calls) == 2
    assert calls[0][0] == "PUT" and "refresh_requests'!E2" in calls[0][1]
    assert calls[0][2]["json"]["values"] == [["superseded 2026-09-08T09:15:00-06:00"]]
    assert calls[1][0] == "PUT" and "refresh_requests'!E5" in calls[1][1]
    assert calls[1][2]["json"]["values"] == [["superseded 2026-09-08T09:15:00-06:00"]]


def test_mark_status_no_op_on_empty_row_indexes(monkeypatch):
    def _boom(*_a, **_kw):
        raise AssertionError("must not call authed_request with no rows to mark")

    monkeypatch.setattr(refresh_gate.google_auth, "authed_request", _boom)
    refresh_gate.mark_status("SHEETID", [], "superseded 2026-09-08T09:15:00-06:00")  # no raise


def test_mark_status_raises_on_transport_error(monkeypatch):
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         lambda method, url, **kw: FakeResponse(403, {}, text="forbidden"))
    with pytest.raises(RuntimeError):
        refresh_gate.mark_status("SHEETID", [2], "superseded 2026-09-08T09:15:00-06:00")


def test_mark_honored_delegates_to_mark_status(monkeypatch):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, urllib.parse.unquote(url), kwargs))
        return FakeResponse(200, {})

    monkeypatch.setattr(refresh_gate.google_auth, "authed_request", fake)
    refresh_gate.mark_honored("SHEETID", [2], "2026-09-08T09:15:00-06:00")

    assert len(calls) == 1
    assert calls[0][2]["json"]["values"] == [["honored 2026-09-08T09:15:00-06:00"]]


# ---------------------------------------------------------------------------
# T9b: write_run_log header extension against a stubbed Sheets backend
# ---------------------------------------------------------------------------

SIX_LABEL_HEADER = ["pulled_at_mt", "asof_date", "all_pass", "sections_json", "checks_json", "published_at_mt"]
EIGHT_LABEL_HEADER = SIX_LABEL_HEADER + ["trigger", "request_row"]


def _sample_run_log_data():
    return {
        "meta": {
            "pulled_at_mt": "2026-09-08T03:15:22-06:00",
            "asof_date": "2026-09-07",
            "checks": {"all_pass": True},
            "sections": {"sales": "ok"},
        }
    }


def _make_fake_run_log_authed_request(existing_header, calls):
    def fake(method, url, **kwargs):
        decoded = urllib.parse.unquote(url)
        calls.append({"method": method, "url": decoded, "kwargs": kwargs})
        if method == "GET" and "run_log'!1:1" in decoded:
            return FakeResponse(200, {"values": [existing_header]} if existing_header else {})
        if method == "GET" and "run_log'!A:A" in decoded:
            return FakeResponse(200, {"values": []})  # no prior row -> append proceeds
        if method == "PUT" and "run_log'!A1" in decoded:
            return FakeResponse(200, {})
        if method == "POST" and decoded.endswith("run_log':append"):
            return FakeResponse(200, {})
        raise AssertionError(f"unexpected authed_request call: {method} {decoded}")
    return fake


def test_write_run_log_extends_six_label_header(monkeypatch):
    calls = []
    monkeypatch.setattr(publish_sheet.google_auth, "authed_request",
                         _make_fake_run_log_authed_request(SIX_LABEL_HEADER, calls))
    publish_sheet.write_run_log("SHEETID", _sample_run_log_data(), dry_run=False,
                                 trigger="nightly", request_row="")
    put_calls = [c for c in calls if c["method"] == "PUT"]
    assert len(put_calls) == 1
    assert put_calls[0]["kwargs"]["json"]["values"][0] == EIGHT_LABEL_HEADER


def test_write_run_log_no_put_when_header_already_eight_labels(monkeypatch):
    calls = []
    monkeypatch.setattr(publish_sheet.google_auth, "authed_request",
                         _make_fake_run_log_authed_request(EIGHT_LABEL_HEADER, calls))
    publish_sheet.write_run_log("SHEETID", _sample_run_log_data(), dry_run=False,
                                 trigger="nightly", request_row="")
    assert not any(c["method"] == "PUT" for c in calls)
    assert any(c["method"] == "POST" for c in calls)  # the append still happens


# ---------------------------------------------------------------------------
# T9c: read_run_log_last_success
# ---------------------------------------------------------------------------

def _make_fake_run_log_a_c_authed_request(values_rows):
    def fake(method, url, **kwargs):
        decoded = urllib.parse.unquote(url)
        assert method == "GET" and "run_log'!A:C" in decoded
        return FakeResponse(200, {"values": values_rows} if values_rows is not None else {})
    return fake


def test_read_run_log_last_success_ignores_false_all_pass(monkeypatch):
    rows = [
        ["pulled_at_mt", "asof_date", "all_pass"],
        ["2026-09-06T03:00:00-06:00", "2026-09-05", True],
        ["2026-09-07T03:00:00-06:00", "2026-09-06", False],
    ]
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         _make_fake_run_log_a_c_authed_request(rows))
    result = refresh_gate.read_run_log_last_success("SHEETID")
    assert result == refresh_gate._parse_offset_iso_to_utc("2026-09-06T03:00:00-06:00")


def test_read_run_log_last_success_empty_tab_returns_none(monkeypatch):
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         _make_fake_run_log_a_c_authed_request([]))
    assert refresh_gate.read_run_log_last_success("SHEETID") is None


def test_read_run_log_last_success_header_only_returns_none(monkeypatch):
    rows = [["pulled_at_mt", "asof_date", "all_pass"]]
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         _make_fake_run_log_a_c_authed_request(rows))
    assert refresh_gate.read_run_log_last_success("SHEETID") is None


def test_read_run_log_last_success_dst_boundary_mdt_side(monkeypatch):
    # 2026-10-31 is before the 2026-11-01 fall-back -- MDT, offset -06:00.
    rows = [
        ["pulled_at_mt", "asof_date", "all_pass"],
        ["2026-10-31T20:00:00-06:00", "2026-10-31", True],
    ]
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         _make_fake_run_log_a_c_authed_request(rows))
    assert refresh_gate.read_run_log_last_success("SHEETID") == _utc(2026, 11, 1, 2, 0, 0)


def test_read_run_log_last_success_dst_boundary_mst_side(monkeypatch):
    # 2026-11-02 is after the 2026-11-01 fall-back -- MST, offset -07:00.
    rows = [
        ["pulled_at_mt", "asof_date", "all_pass"],
        ["2026-11-02T20:00:00-07:00", "2026-11-02", True],
    ]
    monkeypatch.setattr(refresh_gate.google_auth, "authed_request",
                         _make_fake_run_log_a_c_authed_request(rows))
    assert refresh_gate.read_run_log_last_success("SHEETID") == _utc(2026, 11, 3, 3, 0, 0)


# ---------------------------------------------------------------------------
# T10: run_nightly.py --gate integration, no network, no Doppler
# ---------------------------------------------------------------------------

def test_gate_skip_no_request_touches_nothing(monkeypatch, capsys):
    monkeypatch.setattr(run_nightly.doppler_env, "ensure_loaded", lambda: True)
    monkeypatch.setenv("SPIKEBALL_FINANCE_SHEET_ID", "test-sheet-id")

    monkeypatch.setattr(refresh_gate, "read_requests", lambda sheet_id: [])
    recent_success = _utc(2026, 9, 8, 13, 0, 0)
    monkeypatch.setattr(refresh_gate, "read_run_log_last_success", lambda sheet_id: recent_success)
    monkeypatch.setattr(refresh_gate, "lock_age_minutes", lambda: None)
    monkeypatch.setattr(refresh_gate, "read_last_attempt_utc", lambda: None)

    def _boom(*_a, **_kw):
        raise AssertionError("must not be called on a NIGHTLY_SKIP path")

    monkeypatch.setattr(refresh_gate, "touch_attempt", _boom)
    monkeypatch.setattr(refresh_gate, "acquire_lock", _boom)
    monkeypatch.setattr(run_nightly, "run_pipeline", _boom)

    monkeypatch.setattr(run_nightly, "_now_utc", lambda: _utc(2026, 9, 8, 15, 0, 0))  # hour 15, not 9
    monkeypatch.setattr("sys.argv", ["run_nightly.py", "--gate", "--no-alert"])

    data_dir = run_nightly.SPIKE / "data"
    before = _snapshot_dir(data_dir)

    exit_code = run_nightly.main()

    after = _snapshot_dir(data_dir)
    captured = capsys.readouterr()

    assert "NIGHTLY_SKIP no_request" in captured.out
    assert exit_code == 0
    assert before == after


def test_gate_skip_marks_stale_queued_requests_superseded(monkeypatch, capsys):
    """On a NIGHTLY_SKIP path, a queued refresh_requests row older than (or exactly at)
    the last success is marked 'superseded <now_mt_iso>' via refresh_gate.mark_status()
    -- a Sheets cell PUT, still with no local filesystem write and no change to the
    printed verdict."""
    monkeypatch.setattr(run_nightly.doppler_env, "ensure_loaded", lambda: True)
    monkeypatch.setenv("SPIKEBALL_FINANCE_SHEET_ID", "test-sheet-id")

    recent_success = _utc(2026, 9, 8, 13, 0, 0)
    stale_request = [(2, recent_success - timedelta(hours=1), "queued")]
    monkeypatch.setattr(refresh_gate, "read_requests", lambda sheet_id: stale_request)
    monkeypatch.setattr(refresh_gate, "read_run_log_last_success", lambda sheet_id: recent_success)
    monkeypatch.setattr(refresh_gate, "lock_age_minutes", lambda: None)
    monkeypatch.setattr(refresh_gate, "read_last_attempt_utc", lambda: None)

    mark_status_calls = []
    monkeypatch.setattr(
        refresh_gate, "mark_status",
        lambda sheet_id, rows, status_text: mark_status_calls.append((sheet_id, rows, status_text)))

    def _boom(*_a, **_kw):
        raise AssertionError("must not be called on a NIGHTLY_SKIP path")

    monkeypatch.setattr(refresh_gate, "touch_attempt", _boom)
    monkeypatch.setattr(refresh_gate, "acquire_lock", _boom)
    monkeypatch.setattr(refresh_gate, "mark_honored", _boom)
    monkeypatch.setattr(run_nightly, "run_pipeline", _boom)

    monkeypatch.setattr(run_nightly, "_now_utc", lambda: _utc(2026, 9, 8, 15, 0, 0))  # hour 15, not 9
    monkeypatch.setattr("sys.argv", ["run_nightly.py", "--gate", "--no-alert"])

    data_dir = run_nightly.SPIKE / "data"
    before = _snapshot_dir(data_dir)

    exit_code = run_nightly.main()

    after = _snapshot_dir(data_dir)
    captured = capsys.readouterr()

    assert "NIGHTLY_SKIP no_request" in captured.out
    assert exit_code == 0
    assert before == after  # no local write -- only the stubbed Sheets PUT happened

    assert len(mark_status_calls) == 1
    sheet_id_called, rows_called, status_text_called = mark_status_calls[0]
    assert sheet_id_called == "test-sheet-id"
    assert rows_called == [2]
    assert status_text_called.startswith("superseded ")


def test_gate_skip_mark_status_failure_does_not_change_verdict(monkeypatch, capsys):
    """A Sheets error while marking stale rows superseded must be logged and swallowed,
    never raised and never allowed to flip the already-computed NIGHTLY_SKIP verdict."""
    monkeypatch.setattr(run_nightly.doppler_env, "ensure_loaded", lambda: True)
    monkeypatch.setenv("SPIKEBALL_FINANCE_SHEET_ID", "test-sheet-id")

    recent_success = _utc(2026, 9, 8, 13, 0, 0)
    stale_request = [(2, recent_success - timedelta(hours=1), "queued")]
    monkeypatch.setattr(refresh_gate, "read_requests", lambda sheet_id: stale_request)
    monkeypatch.setattr(refresh_gate, "read_run_log_last_success", lambda sheet_id: recent_success)
    monkeypatch.setattr(refresh_gate, "lock_age_minutes", lambda: None)
    monkeypatch.setattr(refresh_gate, "read_last_attempt_utc", lambda: None)

    def _raise_mark_status(*_a, **_kw):
        raise RuntimeError("simulated Sheets transport error")

    monkeypatch.setattr(refresh_gate, "mark_status", _raise_mark_status)

    def _boom(*_a, **_kw):
        raise AssertionError("must not be called on a NIGHTLY_SKIP path")

    monkeypatch.setattr(refresh_gate, "touch_attempt", _boom)
    monkeypatch.setattr(refresh_gate, "acquire_lock", _boom)
    monkeypatch.setattr(run_nightly, "run_pipeline", _boom)

    monkeypatch.setattr(run_nightly, "_now_utc", lambda: _utc(2026, 9, 8, 15, 0, 0))
    monkeypatch.setattr("sys.argv", ["run_nightly.py", "--gate", "--no-alert"])

    data_dir = run_nightly.SPIKE / "data"
    before = _snapshot_dir(data_dir)

    exit_code = run_nightly.main()

    after = _snapshot_dir(data_dir)
    captured = capsys.readouterr()

    assert "NIGHTLY_SKIP no_request" in captured.out
    assert exit_code == 0
    assert before == after
    assert "WARNING mark_status(superseded) failed" in captured.out


# ---------------------------------------------------------------------------
# doppler_env.ensure_loaded(): env-first (handoff-design.md Deliverable 1)
# ---------------------------------------------------------------------------

def test_ensure_loaded_env_first_no_doppler_call_no_token(monkeypatch, capsys):
    """When both sentinels are already in os.environ, ensure_loaded() must return True
    immediately -- no Doppler CLI subprocess, no Doppler API/network call -- and must
    not require DOPPLER_TOKEN to be set."""
    monkeypatch.setenv("NETSUITE_ACCOUNT_ID", "test-account-id")
    monkeypatch.setenv("SPIKEBALL_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.delenv("DOPPLER_TOKEN", raising=False)

    def _boom(*_a, **_kw):
        raise AssertionError("must not contact Doppler when the sentinels are already set")

    monkeypatch.setattr(doppler_env, "_load_via_cli", _boom)
    monkeypatch.setattr(doppler_env, "_load_via_api", _boom)

    assert doppler_env.ensure_loaded() is True
    assert "secrets already present in the environment" in capsys.readouterr().out


def test_ensure_loaded_env_first_ignores_a_present_doppler_token():
    """The env-first path short-circuits even when DOPPLER_TOKEN happens to also be
    set -- presence of the sentinels alone is what matters, per doppler_env.py's
    documented precedence (already-wrapped environment first)."""
    import os as _os

    prev_token = _os.environ.get("DOPPLER_TOKEN")
    _os.environ["NETSUITE_ACCOUNT_ID"] = "test-account-id"
    _os.environ["SPIKEBALL_OAUTH_CLIENT_ID"] = "test-client-id"
    _os.environ["DOPPLER_TOKEN"] = "unused-placeholder-token"
    try:
        assert doppler_env.ensure_loaded() is True
    finally:
        if prev_token is None:
            _os.environ.pop("DOPPLER_TOKEN", None)
        else:
            _os.environ["DOPPLER_TOKEN"] = prev_token


def test_ensure_loaded_returns_false_without_sentinels_token_or_cli(monkeypatch):
    """Inverse sanity check: with a sentinel missing, no DOPPLER_TOKEN, and the CLI
    fallback stubbed to fail (simulating "doppler" absent from PATH), ensure_loaded()
    must return False rather than silently proceeding with partial credentials."""
    monkeypatch.delenv("NETSUITE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("SPIKEBALL_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("DOPPLER_TOKEN", raising=False)
    monkeypatch.setattr(doppler_env, "_load_via_cli", lambda: False)

    assert doppler_env.ensure_loaded() is False


# ---------------------------------------------------------------------------
# refresh_gate.nightly_slot_utc_hour(): SPIKEBALL_NIGHTLY_SLOT_UTC override
# (handoff-design.md Deliverable 2)
# ---------------------------------------------------------------------------

def test_nightly_slot_utc_hour_defaults_to_9(monkeypatch):
    monkeypatch.delenv("SPIKEBALL_NIGHTLY_SLOT_UTC", raising=False)
    assert refresh_gate.nightly_slot_utc_hour() == 9 == refresh_gate.NIGHTLY_SLOT_UTC_HOUR_DEFAULT


def test_nightly_slot_utc_hour_reads_the_env_override(monkeypatch):
    monkeypatch.setenv("SPIKEBALL_NIGHTLY_SLOT_UTC", "10")
    assert refresh_gate.nightly_slot_utc_hour() == 10


def test_nightly_slot_utc_hour_blank_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SPIKEBALL_NIGHTLY_SLOT_UTC", "  ")
    assert refresh_gate.nightly_slot_utc_hour() == 9


def test_nightly_slot_utc_hour_non_integer_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SPIKEBALL_NIGHTLY_SLOT_UTC", "not-an-int")
    assert refresh_gate.nightly_slot_utc_hour() == 9


def test_decide_nightly_slot_override_hour_10_runs(monkeypatch):
    # With SPIKEBALL_NIGHTLY_SLOT_UTC=10, 10:00 UTC is the nightly slot.
    monkeypatch.setenv("SPIKEBALL_NIGHTLY_SLOT_UTC", "10")
    now_10 = _utc(2026, 9, 8, 10, 0, 0)
    verdict, reason, honored = refresh_gate.decide(now_10, [], RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("run", "nightly_slot", [])


def test_decide_nightly_slot_override_hour_9_no_longer_the_slot(monkeypatch):
    # With SPIKEBALL_NIGHTLY_SLOT_UTC=10, the OLD default hour (9) is no longer
    # special -- decide() falls through to no_request exactly like any other hour.
    monkeypatch.setenv("SPIKEBALL_NIGHTLY_SLOT_UTC", "10")
    now_9 = _utc(2026, 9, 8, 9, 0, 0)
    verdict, reason, honored = refresh_gate.decide(now_9, [], RECENT_SUCCESS, None, None)
    assert (verdict, reason, honored) == ("skip", "no_request", [])


# ---------------------------------------------------------------------------
# doppler_env: DOPPLER_PROJECT/DOPPLER_CONFIG have no literal fallback -- a Doppler
# service token is scoped to exactly one project/config, so _load_via_cli()/
# _load_via_api() omit --project/--config (resp. the project/config query params)
# entirely when unset, and the three write-back call sites skip the `doppler secrets
# set` write-back with a DOPPLER_WRITEBACK_SKIPPED line instead of failing.
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_load_via_api_omits_project_config_when_unset(monkeypatch):
    monkeypatch.delenv("DOPPLER_PROJECT", raising=False)
    monkeypatch.delenv("DOPPLER_CONFIG", raising=False)
    monkeypatch.setenv("DOPPLER_TOKEN", "test-token")

    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        return _FakeHTTPResponse(b'{"_TEST_DOPPLER_ENV_SECRET_A": "value"}')

    monkeypatch.setattr(doppler_env.urllib.request, "urlopen", fake_urlopen)
    os.environ.pop("_TEST_DOPPLER_ENV_SECRET_A", None)

    try:
        assert doppler_env._load_via_api() is True
    finally:
        os.environ.pop("_TEST_DOPPLER_ENV_SECRET_A", None)

    assert "project=" not in captured["url"]
    assert "config=" not in captured["url"]
    assert "format=json" in captured["url"]


def test_load_via_api_includes_project_config_when_set(monkeypatch):
    monkeypatch.setenv("DOPPLER_PROJECT", "test-project")
    monkeypatch.setenv("DOPPLER_CONFIG", "test-config")
    monkeypatch.setenv("DOPPLER_TOKEN", "test-token")

    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        return _FakeHTTPResponse(b'{"_TEST_DOPPLER_ENV_SECRET_B": "value"}')

    monkeypatch.setattr(doppler_env.urllib.request, "urlopen", fake_urlopen)
    os.environ.pop("_TEST_DOPPLER_ENV_SECRET_B", None)

    try:
        assert doppler_env._load_via_api() is True
    finally:
        os.environ.pop("_TEST_DOPPLER_ENV_SECRET_B", None)

    assert "project=test-project" in captured["url"]
    assert "config=test-config" in captured["url"]
    assert "format=json" in captured["url"]


def test_store_sheet_id_writeback_skipped_without_project_config(monkeypatch, capsys):
    monkeypatch.delenv("DOPPLER_PROJECT", raising=False)
    monkeypatch.delenv("DOPPLER_CONFIG", raising=False)

    def _boom(*_a, **_kw):
        raise AssertionError("must not shell out to doppler when project/config are unset")

    monkeypatch.setattr(publish_sheet.subprocess, "run", _boom)

    result = publish_sheet.store_sheet_id("SOME-SHEET-ID")

    assert result is False
    assert ("DOPPLER_WRITEBACK_SKIPPED set DOPPLER_PROJECT and DOPPLER_CONFIG to persist "
            "SPIKEBALL_FINANCE_SHEET_ID") in capsys.readouterr().out
