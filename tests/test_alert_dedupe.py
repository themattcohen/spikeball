"""Alert de-duplication for the nightly routine (owner ruling 2026-10-08): one alert
per distinct failing reason per window, later identical failures leave a `suppressed`
row on the Sheet's alert_log tab instead of emailing.

Covers spike/routine/alert.py's alert_signature(), dedupe_window_hours(),
should_send() and log_alert(), and run_nightly.fail()'s use of them. Every Google call
is stubbed (google_auth.authed_request and alert.send_alert); nothing here reaches the
network, Doppler, or the real Sheet.

Run: python -m pytest tests/test_alert_dedupe.py -q
"""
import os

# Set BEFORE importing run_nightly: spike/routine/alert.py (imported by run_nightly.py)
# calls doppler_env.ensure_loaded() at IMPORT TIME. Pre-seeding doppler_env's two
# "already loaded" sentinel env vars makes that a same-process no-op (no subprocess, no
# network). Inert placeholders, never used to make a real call.
os.environ.setdefault("NETSUITE_ACCOUNT_ID", "test-sentinel-account")
os.environ.setdefault("SPIKEBALL_OAUTH_CLIENT_ID", "000000000000-test-sentinel.apps.googleusercontent.com")

import argparse
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

import alert
import run_nightly


# ---------------------------------------------------------------------------
# Real failure texts (verbatim from the routine's run logs)
# ---------------------------------------------------------------------------

# 2026-10-07 03:28 MT run, check g.
G_DETAIL_0328 = (
    "2026-09: moved revenue 26346.51, ntxn 5; explained by transactions created in "
    "(2026-10-06 08:42:04, 2026-10-07 04:11:40] (NetSuite clock): revenue 1097.82, ntxn 5; "
    "residual revenue 25248.69 (3.1326%), ntxn 0 (0.0%) -- residual exceeds 0.5% "
    "(unexplained movement: edits, deletions or extract error)"
)
# 2026-10-07 11:27 MT run, same reason, different numbers.
G_DETAIL_1127 = (
    "2026-09: moved revenue 26392.94, ntxn 7; explained by transactions created in "
    "(2026-10-06 08:42:04, 2026-10-07 12:11:40] (NetSuite clock): revenue 1144.25, ntxn 7; "
    "residual revenue 25248.69 (3.1326%), ntxn 0 (0.0%) -- residual exceeds 0.5% "
    "(unexplained movement: edits, deletions or extract error)"
)
# 2026-10-06 07:42 MT run, check a.
A_DETAIL_0742 = (
    "channel foot exceeds $0.01: YTD diff=-92117.76 [MTD: re-ran after concurrent posting: "
    "first diff -92117.76, second diff 0.0; YTD: re-ran after concurrent posting: first diff "
    "-92117.76, second diff -12481.96 -- still differs, not a race; 2026-10: re-ran after "
    "concurrent posting: first diff -92117.76, second diff 0.0]"
)

PASSING = {"pass": True, "detail": "ok"}


def _checks(**failing):
    """A meta.checks dict in the extract's shape: every known check passing except the
    named ones, plus the all_pass flag the publishers read."""
    checks = {
        "a_channel_foot": dict(PASSING),
        "b_inventory_replica": dict(PASSING),
        "g_closed_months_stable": dict(PASSING),
        "t5_bom_rule": dict(PASSING),
    }
    for name, detail in failing.items():
        checks[name] = {"pass": False, "detail": detail}
    checks["all_pass"] = not failing
    return checks


def _reason(checks):
    """The reason string run_nightly builds for a checks failure (publish_sheet.get_all_pass)."""
    failing = {k: v.get("detail") for k, v in checks.items()
               if k != "all_pass" and isinstance(v, dict) and v.get("pass") is False}
    return f"checks failed: meta.checks.all_pass is false; failing checks: {failing}"


def _utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.text = text

    def json(self):
        return self._json


def _unquoted_range(url):
    """The A1 range inside a Sheets values URL, decoded, with any ':append' suffix kept."""
    tail = url.split("/values/", 1)[1]
    return urllib.parse.unquote(tail)


# ---------------------------------------------------------------------------
# alert_signature(): pure
# ---------------------------------------------------------------------------

G_SIG = "NIGHTLY_FAIL:checks:g_closed_months_stable[2026-09]"
A_SIG = "NIGHTLY_FAIL:checks:a_channel_foot"


def test_signature_check_g_same_reason_different_numbers():
    c1 = _checks(g_closed_months_stable=G_DETAIL_0328)
    c2 = _checks(g_closed_months_stable=G_DETAIL_1127)
    s1 = alert.alert_signature(2, _reason(c1), checks=c1)
    s2 = alert.alert_signature(2, _reason(c2), checks=c2)
    assert s1 == G_SIG
    assert s2 == G_SIG


def test_signature_check_a():
    c = _checks(a_channel_foot=A_DETAIL_0742)
    assert alert.alert_signature(2, _reason(c), checks=c) == A_SIG


def test_signature_check_g_different_month_is_a_different_signature():
    aug = _checks(g_closed_months_stable=G_DETAIL_0328.replace("2026-09:", "2026-08:"))
    sep = _checks(g_closed_months_stable=G_DETAIL_0328)
    s_aug = alert.alert_signature(2, _reason(aug), checks=aug)
    s_sep = alert.alert_signature(2, _reason(sep), checks=sep)
    assert s_aug == "NIGHTLY_FAIL:checks:g_closed_months_stable[2026-08]"
    assert s_sep == G_SIG
    assert s_aug != s_sep


def test_signature_check_g_two_months_sorted():
    detail = G_DETAIL_0328 + "; " + G_DETAIL_1127.replace("2026-09:", "2026-07:")
    c = _checks(g_closed_months_stable=detail)
    assert alert.alert_signature(2, _reason(c), checks=c) == \
        "NIGHTLY_FAIL:checks:g_closed_months_stable[2026-07,2026-09]"


def test_signature_a_and_g_together_differs_from_g_alone():
    both = _checks(a_channel_foot=A_DETAIL_0742, g_closed_months_stable=G_DETAIL_0328)
    s_both = alert.alert_signature(2, _reason(both), checks=both)
    assert s_both == "NIGHTLY_FAIL:checks:a_channel_foot,g_closed_months_stable[2026-09]"
    assert s_both != G_SIG
    assert s_both != A_SIG


def test_signature_month_regex_ignores_timestamps_in_check_g_detail():
    # "(2026-10-06 08:42:04, ...]" and "2026-10: re-ran ..." shapes must not add months
    # to the closed-months key: only "YYYY-MM:" at a month label counts, and only for
    # check g (check a's "2026-10:" segment is not read at all).
    c = _checks(a_channel_foot=A_DETAIL_0742)
    assert "[" not in alert.alert_signature(2, _reason(c), checks=c)
    g = _checks(g_closed_months_stable=G_DETAIL_0328)
    assert alert.alert_signature(2, _reason(g), checks=g).endswith("[2026-09]")


def test_signature_ignores_all_pass_key_and_non_dict_entries():
    c = _checks(g_closed_months_stable=G_DETAIL_0328)
    c["all_pass"] = False
    c["note"] = "free text, not a check"
    assert alert.alert_signature(2, _reason(c), checks=c) == G_SIG


def test_signature_checks_with_nothing_failing_falls_back_to_reason():
    c = _checks()
    sig = alert.alert_signature(2, "checks failed: something odd 123", checks=c)
    assert sig == "NIGHTLY_FAIL:2:checks failed: something odd"


def test_signature_without_checks_strips_digits_first_line_lowercase_80_chars():
    r1 = ("extract.py crashed (rc=1), no output written: Traceback (most recent call last)\n"
          "  File \"x.py\", line 123\nKeyError: 'foo'")
    r2 = ("extract.py crashed (rc=2), no output written: Traceback (most recent call last)\n"
          "  File \"x.py\", line 456\nKeyError: 'bar'")
    s1 = alert.alert_signature(4, r1)
    s2 = alert.alert_signature(4, r2)
    assert s1 == s2
    assert s1.startswith("NIGHTLY_FAIL:4:extract.py crashed (rc=), no output written:")
    reason_part = s1.split(":", 2)[2]
    assert reason_part == reason_part.lower()   # the verdict prefix keeps its case
    assert "\n" not in s1
    assert len(s1) <= len("NIGHTLY_FAIL:4:") + 80
    long_reason = "A" * 500
    assert len(alert.alert_signature(4, long_reason)) == len("NIGHTLY_FAIL:4:") + 80


def test_signature_without_checks_collapses_whitespace():
    assert alert.alert_signature(3, "publish   failed \t  twice  ") == "NIGHTLY_FAIL:3:publish failed twice"


def test_signature_without_checks_real_reason_text_still_stable_across_numbers():
    # The fallback path on the real checks-failure reason (if a caller ever passes no
    # checks dict) still collapses the two 10/07 runs to one key.
    c1 = _checks(g_closed_months_stable=G_DETAIL_0328)
    c2 = _checks(g_closed_months_stable=G_DETAIL_1127)
    assert alert.alert_signature(2, _reason(c1)) == alert.alert_signature(2, _reason(c2))


def test_signature_partial_verdict_and_code_are_part_of_the_key():
    assert alert.alert_signature(3, "x", verdict="NIGHTLY_PARTIAL_OK") == "NIGHTLY_PARTIAL_OK:3:x"
    assert alert.alert_signature(3, "x") != alert.alert_signature(4, "x")


def test_signature_empty_reason():
    assert alert.alert_signature(3, "") == "NIGHTLY_FAIL:3:"
    assert alert.alert_signature(3, None) == "NIGHTLY_FAIL:3:"


# ---------------------------------------------------------------------------
# dedupe_window_hours()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (None, 24), ("", 24), ("   ", 24), ("24", 24), ("12", 12), ("1.5", 1.5),
    ("abc", 24), ("-5", 24), ("nan", 24), ("inf", 24), ("0", 0), ("0.0", 0),
])
def test_dedupe_window_hours(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(alert.DEDUPE_HOURS_ENV, raising=False)
    else:
        monkeypatch.setenv(alert.DEDUPE_HOURS_ENV, raw)
    assert alert.dedupe_window_hours() == expected


# ---------------------------------------------------------------------------
# should_send(): Sheet read stubbed
# ---------------------------------------------------------------------------

NOW = _utc(2026, 10, 7, 17, 27, 0)        # 11:27 MT on 10/07
SENT_0328 = _utc(2026, 10, 7, 9, 28, 0)   # 03:28 MT on 10/07, eight hours earlier
HEADER = ["sent_at_utc", "sent_at_mt", "verdict", "signature", "action"]


def _row(sent_utc, signature, action="sent", verdict="NIGHTLY_FAIL"):
    return [sent_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), alert.to_mt_iso(sent_utc), verdict, signature, action]


def _stub_get(monkeypatch, rows, status=200, text=""):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert method == "GET"
        assert _unquoted_range(url) == "'alert_log'!A:E"
        if status != 200:
            return FakeResponse(status, {}, text)
        return FakeResponse(200, {"values": [HEADER] + rows})

    monkeypatch.setattr(alert.google_auth, "authed_request", fake)
    return calls


def test_should_send_same_signature_inside_window_suppresses(monkeypatch):
    _stub_get(monkeypatch, [_row(SENT_0328, G_SIG)])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (False, SENT_0328)


def test_should_send_same_signature_outside_window_sends(monkeypatch):
    old = NOW - timedelta(hours=25)
    _stub_get(monkeypatch, [_row(old, G_SIG)])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, old)


def test_should_send_window_boundary_is_exclusive(monkeypatch):
    exactly = NOW - timedelta(hours=24)
    _stub_get(monkeypatch, [_row(exactly, G_SIG)])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, exactly)


def test_should_send_different_signature_sends(monkeypatch):
    _stub_get(monkeypatch, [_row(SENT_0328, G_SIG)])
    assert alert.should_send(A_SIG, "SHEETID", NOW, 24) == (True, None)


def test_should_send_suppressed_rows_do_not_count_as_sent(monkeypatch):
    _stub_get(monkeypatch, [_row(SENT_0328, G_SIG, action="suppressed"),
                            _row(NOW - timedelta(hours=1), G_SIG, action="suppressed")])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, None)


def test_should_send_uses_the_latest_sent_row_regardless_of_order(monkeypatch):
    old = NOW - timedelta(hours=30)
    rows = [_row(SENT_0328, G_SIG), _row(old, G_SIG), _row(NOW - timedelta(hours=2), G_SIG, "suppressed")]
    _stub_get(monkeypatch, rows)
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (False, SENT_0328)
    _stub_get(monkeypatch, list(reversed(rows)))
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (False, SENT_0328)


def test_should_send_empty_tab_header_only(monkeypatch):
    _stub_get(monkeypatch, [])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, None)


def test_should_send_get_non_200_fails_open(monkeypatch, capsys):
    _stub_get(monkeypatch, [], status=503, text="backend error")
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, None)
    assert "HTTP 503" in capsys.readouterr().out


def test_should_send_missing_tab_fails_open(monkeypatch, capsys):
    _stub_get(monkeypatch, [], status=400, text='{"error": {"message": "Unable to parse range: \'alert_log\'!A:E"}}')
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, None)
    assert "alert_log tab not present yet" in capsys.readouterr().out


def test_should_send_transport_error_fails_open(monkeypatch, capsys):
    def boom(method, url, **kwargs):
        raise alert.google_auth.GoogleAuthError("simulated transport failure")

    monkeypatch.setattr(alert.google_auth, "authed_request", boom)
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, None)
    assert "dedupe read failed" in capsys.readouterr().out


def test_should_send_unreadable_body_fails_open(monkeypatch, capsys):
    class Broken(FakeResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(alert.google_auth, "authed_request", lambda m, u, **kw: Broken(200))
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, None)
    assert "unreadable body" in capsys.readouterr().out


def test_should_send_unparsable_sent_at_row_is_ignored(monkeypatch, capsys):
    bad = ["not a time", "x", "NIGHTLY_FAIL", G_SIG, "sent"]
    short = ["2026-10-07T09:28:00Z", "x", "NIGHTLY_FAIL"]  # fewer than 5 columns
    _stub_get(monkeypatch, [bad, short])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 24) == (True, None)
    assert "unreadable sent_at_utc" in capsys.readouterr().out


def test_should_send_window_zero_always_sends_without_reading(monkeypatch):
    calls = _stub_get(monkeypatch, [_row(SENT_0328, G_SIG)])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 0) == (True, None)
    assert calls == []


def test_should_send_fractional_window(monkeypatch):
    _stub_get(monkeypatch, [_row(NOW - timedelta(minutes=20), G_SIG)])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 0.5)[0] is False
    _stub_get(monkeypatch, [_row(NOW - timedelta(minutes=40), G_SIG)])
    assert alert.should_send(G_SIG, "SHEETID", NOW, 0.5)[0] is True


# ---------------------------------------------------------------------------
# log_alert(): Sheet writes stubbed
# ---------------------------------------------------------------------------

class FakeSheets:
    """A minimal in-memory stand-in for the four Sheets calls alert.py makes: GET the
    alert_log range, POST values:append, POST :batchUpdate (addSheet), PUT the header.
    Starts without the tab unless `rows` is given. `fail_append_status` makes every
    append answer with that status (and `fail_append_text`)."""

    def __init__(self, rows=None, fail_append_status=None, fail_append_text=""):
        self.tab = None if rows is None else [list(HEADER)] + [list(r) for r in rows]
        self.calls = []
        self.fail_append_status = fail_append_status
        self.fail_append_text = fail_append_text

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith(":batchUpdate"):
            title = kwargs["json"]["requests"][0]["addSheet"]["properties"]["title"]
            assert title == "alert_log"
            self.tab = []
            return FakeResponse(200, {"replies": [{"addSheet": {"properties": {"title": title}}}]})
        rng = _unquoted_range(url)
        if method == "GET":
            if self.tab is None:
                return FakeResponse(400, {}, "Unable to parse range: 'alert_log'!A:E")
            return FakeResponse(200, {"values": self.tab})
        if method == "PUT":
            assert rng == "'alert_log'!A1:E1"
            assert kwargs["params"] == {"valueInputOption": "RAW"}
            assert self.tab is not None
            if self.tab:
                self.tab[0] = list(kwargs["json"]["values"][0])
            else:
                self.tab.append(list(kwargs["json"]["values"][0]))
            return FakeResponse(200, {"updatedRows": 1})
        if method == "POST" and rng.endswith(":append"):
            assert rng == "'alert_log'!A:E:append"
            assert kwargs["params"] == {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
            if self.fail_append_status:
                return FakeResponse(self.fail_append_status, {}, self.fail_append_text)
            if self.tab is None:
                return FakeResponse(400, {}, '{"error": {"message": "Unable to parse range: alert_log!A:E"}}')
            self.tab.extend(list(r) for r in kwargs["json"]["values"])
            return FakeResponse(200, {"updates": {"updatedRows": len(kwargs["json"]["values"])}})
        raise AssertionError(f"unexpected call {method} {url}")

    def methods(self):
        return [(m, _unquoted_range(u) if "/values/" in u else u.rsplit("/", 1)[-1].rsplit(":", 1)[-1])
                for (m, u, _kw) in self.calls]


def test_log_alert_append_happy_path(monkeypatch):
    sheets = FakeSheets(rows=[])
    monkeypatch.setattr(alert.google_auth, "authed_request", sheets)
    assert alert.log_alert("SHEETID", SENT_0328, "NIGHTLY_FAIL", G_SIG, "sent") is True
    assert sheets.methods() == [("POST", "'alert_log'!A:E:append")]
    assert sheets.tab == [HEADER, ["2026-10-07T09:28:00Z", "2026-10-07T03:28:00-06:00", "NIGHTLY_FAIL", G_SIG, "sent"]]


def test_log_alert_creates_tab_on_unable_to_parse_range(monkeypatch, capsys):
    sheets = FakeSheets()  # no tab yet
    monkeypatch.setattr(alert.google_auth, "authed_request", sheets)
    assert alert.log_alert("SHEETID", SENT_0328, "NIGHTLY_FAIL", G_SIG, "sent") is True
    assert sheets.methods() == [
        ("POST", "'alert_log'!A:E:append"),   # 400 Unable to parse range
        ("POST", "batchUpdate"),              # addSheet alert_log
        ("PUT", "'alert_log'!A1:E1"),         # header row
        ("POST", "'alert_log'!A:E:append"),   # the row, once more
    ]
    assert sheets.tab[0] == HEADER
    assert sheets.tab[1][3:] == [G_SIG, "sent"]
    assert "creating the alert_log tab" in capsys.readouterr().out


def test_log_alert_500_is_swallowed(monkeypatch, capsys):
    sheets = FakeSheets(rows=[], fail_append_status=500, fail_append_text="internal")
    monkeypatch.setattr(alert.google_auth, "authed_request", sheets)
    assert alert.log_alert("SHEETID", SENT_0328, "NIGHTLY_FAIL", G_SIG, "sent") is False
    assert sheets.methods() == [("POST", "'alert_log'!A:E:append")]
    assert "append HTTP 500" in capsys.readouterr().out


def test_log_alert_other_400_is_not_treated_as_missing_tab(monkeypatch):
    sheets = FakeSheets(rows=[], fail_append_status=400, fail_append_text="Invalid value at 'data.values'")
    monkeypatch.setattr(alert.google_auth, "authed_request", sheets)
    assert alert.log_alert("SHEETID", SENT_0328, "NIGHTLY_FAIL", G_SIG, "sent") is False
    assert sheets.methods() == [("POST", "'alert_log'!A:E:append")]


def test_log_alert_transport_error_is_swallowed(monkeypatch, capsys):
    def boom(method, url, **kwargs):
        raise alert.google_auth.GoogleAuthError("simulated transport failure")

    monkeypatch.setattr(alert.google_auth, "authed_request", boom)
    assert alert.log_alert("SHEETID", SENT_0328, "NIGHTLY_FAIL", G_SIG, "suppressed") is False
    assert "append failed" in capsys.readouterr().out


def test_log_alert_tab_creation_failure_is_swallowed(monkeypatch, capsys):
    sheets = FakeSheets()

    def flaky(method, url, **kwargs):
        if url.endswith(":batchUpdate"):
            return FakeResponse(403, {}, "forbidden")
        return sheets(method, url, **kwargs)

    monkeypatch.setattr(alert.google_auth, "authed_request", flaky)
    assert alert.log_alert("SHEETID", SENT_0328, "NIGHTLY_FAIL", G_SIG, "sent") is False
    assert "addSheet alert_log HTTP 403" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_nightly.fail(): integration with the alert module stubbed
# ---------------------------------------------------------------------------

def _args(**over):
    base = dict(no_alert=False, dry_run=False, sheet="SHEETID")
    base.update(over)
    return argparse.Namespace(**base)


class AlertStubs:
    """Records send_alert / should_send / log_alert calls against an in-memory log so
    two fail() calls in one test see each other's rows."""

    def __init__(self, monkeypatch, send_ok=True, window=24):
        self.sent = []
        self.log = []          # (now_utc, verdict, signature, action)
        self.should_send_calls = []
        self.send_ok = send_ok
        monkeypatch.setattr(alert, "send_alert", self._send_alert)
        monkeypatch.setattr(alert, "should_send", self._should_send)
        monkeypatch.setattr(alert, "log_alert", self._log_alert)
        monkeypatch.setenv(alert.DEDUPE_HOURS_ENV, str(window))

    def _send_alert(self, subject, body, to=None, dry_run=False):
        self.sent.append((subject, body, dry_run))
        return (True, "msg-id-1") if self.send_ok else (False, "Gmail send HTTP 500: boom")

    def _should_send(self, signature, sheet_id, now_utc, window_hours):
        self.should_send_calls.append((signature, sheet_id, now_utc, window_hours))
        if window_hours <= 0:
            return True, None
        last = [t for (t, _v, s, a) in self.log if s == signature and a == "sent"]
        if last and now_utc - max(last) < timedelta(hours=window_hours):
            return False, max(last)
        return True, (max(last) if last else None)

    def _log_alert(self, sheet_id, now_utc, verdict, signature, action):
        self.log.append((now_utc, verdict, signature, action))
        return True


def test_fail_first_call_sends_and_logs_sent_second_identical_call_suppresses(monkeypatch, capsys):
    stubs = AlertStubs(monkeypatch)
    c1 = _checks(g_closed_months_stable=G_DETAIL_0328)
    c2 = _checks(g_closed_months_stable=G_DETAIL_1127)

    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328)
    code = run_nightly.fail(2, f"checks failed: {_reason(c1)}", _args(), checks=c1)
    assert code == 2
    out1 = capsys.readouterr().out
    assert out1.startswith("NIGHTLY_FAIL checks failed: ")
    assert "[run_nightly] alert sent: msg-id-1" in out1
    assert len(stubs.sent) == 1
    assert stubs.sent[0][0] == "Spikeball Finance nightly: FAILED"
    assert stubs.sent[0][2] is False
    assert stubs.log == [(SENT_0328, "NIGHTLY_FAIL", G_SIG, "sent")]

    monkeypatch.setattr(run_nightly, "_now_utc", lambda: NOW)
    code = run_nightly.fail(2, f"checks failed: {_reason(c2)}", _args(), checks=c2)
    assert code == 2
    out2 = capsys.readouterr().out
    assert out2.startswith("NIGHTLY_FAIL checks failed: ")
    assert len(stubs.sent) == 1  # no second email
    assert ("[run_nightly] alert suppressed: same reason already sent at 2026-10-07T03:28:00-06:00 MT "
            f"(window 24h): {G_SIG}") in out2
    assert stubs.log[-1] == (NOW, "NIGHTLY_FAIL", G_SIG, "suppressed")
    assert stubs.should_send_calls[-1] == (G_SIG, "SHEETID", NOW, 24)


def test_fail_different_reason_inside_window_emails_immediately(monkeypatch):
    stubs = AlertStubs(monkeypatch)
    g = _checks(g_closed_months_stable=G_DETAIL_0328)
    a = _checks(a_channel_foot=A_DETAIL_0742)
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328)
    run_nightly.fail(2, f"checks failed: {_reason(g)}", _args(), checks=g)
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: NOW)
    run_nightly.fail(2, f"checks failed: {_reason(a)}", _args(), checks=a)
    assert len(stubs.sent) == 2
    assert [entry[2:] for entry in stubs.log] == [(G_SIG, "sent"), (A_SIG, "sent")]


def test_fail_same_reason_after_window_emails_again(monkeypatch):
    stubs = AlertStubs(monkeypatch)
    g = _checks(g_closed_months_stable=G_DETAIL_0328)
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328)
    run_nightly.fail(2, f"checks failed: {_reason(g)}", _args(), checks=g)
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328 + timedelta(hours=25))
    run_nightly.fail(2, f"checks failed: {_reason(g)}", _args(), checks=g)
    assert len(stubs.sent) == 2
    assert [entry[3] for entry in stubs.log] == ["sent", "sent"]


def test_fail_window_zero_sends_every_time_and_still_logs(monkeypatch):
    stubs = AlertStubs(monkeypatch, window=0)
    g = _checks(g_closed_months_stable=G_DETAIL_0328)
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328)
    run_nightly.fail(2, f"checks failed: {_reason(g)}", _args(), checks=g)
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: NOW)
    run_nightly.fail(2, f"checks failed: {_reason(g)}", _args(), checks=g)
    assert len(stubs.sent) == 2
    assert [entry[3] for entry in stubs.log] == ["sent", "sent"]
    assert stubs.should_send_calls[-1][3] == 0


def test_fail_no_alert_path_unchanged(monkeypatch, capsys):
    stubs = AlertStubs(monkeypatch)
    code = run_nightly.fail(2, "checks failed: x", _args(no_alert=True), checks=_checks(a_channel_foot="d"))
    assert code == 2
    out = capsys.readouterr().out
    assert out.startswith("NIGHTLY_FAIL checks failed: x")
    assert "--no-alert set, skipping alert send" in out
    assert stubs.sent == []
    assert stubs.should_send_calls == []
    assert stubs.log == []


def test_fail_dry_run_path_unchanged(monkeypatch, capsys):
    stubs = AlertStubs(monkeypatch)
    code = run_nightly.fail(3, "publish failed", _args(dry_run=True))
    assert code == 3
    out = capsys.readouterr().out
    assert out.startswith("NIGHTLY_FAIL publish failed")
    assert "[run_nightly] alert sent: msg-id-1" in out
    assert stubs.sent == [("Spikeball Finance nightly: FAILED", stubs.sent[0][1], True)]
    assert stubs.should_send_calls == []   # the Sheet is never touched on a dry run
    assert stubs.log == []


def test_fail_send_failure_does_not_log_sent(monkeypatch, capsys):
    stubs = AlertStubs(monkeypatch, send_ok=False)
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328)
    code = run_nightly.fail(4, "extract.py crashed (rc=1), no output written: tail", _args())
    assert code == 4
    out = capsys.readouterr().out
    assert "ALERT SEND FAILED (not fatal to this run's exit code): Gmail send HTTP 500: boom" in out
    assert len(stubs.sent) == 1
    assert stubs.log == []
    # And because nothing was logged as sent, the next identical failure sends again.
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: NOW)
    stubs.send_ok = True
    run_nightly.fail(4, "extract.py crashed (rc=1), no output written: tail", _args())
    assert len(stubs.sent) == 2
    assert [entry[3] for entry in stubs.log] == ["sent"]


def test_fail_without_sheet_id_sends_without_dedupe(monkeypatch, capsys):
    stubs = AlertStubs(monkeypatch)
    monkeypatch.delenv("SPIKEBALL_FINANCE_SHEET_ID", raising=False)
    code = run_nightly.fail(3, "publish failed", _args(sheet=None))
    assert code == 3
    out = capsys.readouterr().out
    assert "alert de-duplication off for this run, sending" in out
    assert len(stubs.sent) == 1
    assert stubs.should_send_calls == []
    assert stubs.log == []


def test_fail_sheet_id_from_env_when_no_sheet_flag(monkeypatch):
    stubs = AlertStubs(monkeypatch)
    monkeypatch.setenv("SPIKEBALL_FINANCE_SHEET_ID", "env-sheet-id")
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328)
    run_nightly.fail(3, "publish failed", _args(sheet=None))
    assert stubs.should_send_calls[-1][1] == "env-sheet-id"


def test_fail_partial_verdict_subject_and_signature(monkeypatch, capsys):
    stubs = AlertStubs(monkeypatch)
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328)
    code = run_nightly.fail(3, "asof_date=2026-10-06. Failed: {'publish_bq': 'x'}", _args(), verdict="NIGHTLY_PARTIAL_OK")
    assert code == 3
    assert capsys.readouterr().out.startswith("NIGHTLY_PARTIAL_OK asof_date=2026-10-06.")
    assert stubs.sent[0][0] == "Spikeball Finance nightly: PARTIAL failure"
    assert stubs.log[0][1:] == ("NIGHTLY_PARTIAL_OK", "NIGHTLY_PARTIAL_OK:3:asof_date=--. failed: {'publish_bq': 'x'}", "sent")


def test_fail_end_to_end_through_the_real_sheet_helpers(monkeypatch, capsys):
    """Only send_alert and google_auth.authed_request are stubbed: the real
    should_send()/log_alert() run against the in-memory FakeSheets, starting with no
    alert_log tab at all (the first live run after this ships)."""
    sheets = FakeSheets()
    monkeypatch.setattr(alert.google_auth, "authed_request", sheets)
    sent = []
    monkeypatch.setattr(alert, "send_alert", lambda s, b, to=None, dry_run=False: (sent.append(s) or (True, "id")))
    monkeypatch.setenv(alert.DEDUPE_HOURS_ENV, "24")
    g1 = _checks(g_closed_months_stable=G_DETAIL_0328)
    g2 = _checks(g_closed_months_stable=G_DETAIL_1127)

    monkeypatch.setattr(run_nightly, "_now_utc", lambda: SENT_0328)
    assert run_nightly.fail(2, f"checks failed: {_reason(g1)}", _args(), checks=g1) == 2
    monkeypatch.setattr(run_nightly, "_now_utc", lambda: NOW)
    assert run_nightly.fail(2, f"checks failed: {_reason(g2)}", _args(), checks=g2) == 2

    out = capsys.readouterr().out
    assert "alert_log tab not present yet; sending the alert" in out
    assert "creating the alert_log tab" in out
    assert "alert suppressed: same reason already sent at 2026-10-07T03:28:00-06:00 MT (window 24h)" in out
    assert sent == ["Spikeball Finance nightly: FAILED"]
    assert sheets.tab == [
        HEADER,
        ["2026-10-07T09:28:00Z", "2026-10-07T03:28:00-06:00", "NIGHTLY_FAIL", G_SIG, "sent"],
        ["2026-10-07T17:27:00Z", "2026-10-07T11:27:00-06:00", "NIGHTLY_FAIL", G_SIG, "suppressed"],
    ]
