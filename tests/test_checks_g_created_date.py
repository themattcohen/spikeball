"""check (g) created-date rule (owner ruling 2026-10-03, PRD-v2 Section 12 V2R10): a prior
month's movement passes only when transactions CREATED in the window since the baseline explain
it; residual movement (edits, deletions, extract errors, over-explanation) fails. No NetSuite
access: the explainer is a stub that records, and asserts, the window it is asked for."""
import datetime
import json
from types import SimpleNamespace

import pytest

import checks
import extract


# Baseline run (the real 2026-09 incident): extract started 03:11:33 MT, income query ran 03:12:10.
BASELINE_PULL = "2026-10-02T03:11:33-06:00"
BASELINE_INCOME_AT = "2026-10-02T03:12:10-06:00"
# Failing run being checked.
CUR_PULL = "2026-10-03T03:05:00-06:00"
CUR_INCOME_AT = "2026-10-03T03:20:00-06:00"

# Window the stub must be asked for with a current-format baseline: baseline income query
# minus the 2 minute skew margin, through this run's income query, rendered on the clock SuiteQL
# uses for createddate (America/Chicago = Mountain + 1 hour).
WINDOW = ("2026-10-02 04:10:10", "2026-10-03 04:20:00")
# Legacy baseline (state without income_queried_at): lower edge is pulled_at_mt minus 2 minutes.
LEGACY_WINDOW = ("2026-10-02 04:09:33", "2026-10-03 04:20:00")


def _D(sep_revenue, sep_ntxn, known=None, cur_income_at=CUR_INCOME_AT, cur_pull=CUR_PULL):
    meta = {"trailing_months": ["2026-08", "2026-09", "2026-10"], "known_artifacts": known or [],
            "pulled_at_mt": cur_pull}
    if cur_income_at:
        meta["income_queried_at"] = cur_income_at
    return {
        "meta": meta,
        "pnl_by_channel_month": [
            {"ym": "2026-08", "revenue": 1000000.0, "cogs": 0.0, "ntxn": 5000},
            {"ym": "2026-09", "revenue": sep_revenue, "cogs": 0.0, "ntxn": sep_ntxn},
            {"ym": "2026-10", "revenue": 500.0, "cogs": 0.0, "ntxn": 3},
        ],
    }


def _prev(income_at=BASELINE_INCOME_AT, pulled_at=BASELINE_PULL, sep_revenue=803511.16, sep_ntxn=2234):
    st = {"closed_months": {"2026-08": {"revenue": 1000000.0, "cogs": 0.0, "ntxn": 5000},
                            "2026-09": {"revenue": sep_revenue, "cogs": 0.0, "ntxn": sep_ntxn}}}
    if income_at:
        st["income_queried_at"] = income_at
    if pulled_at:
        st["pulled_at_mt"] = pulled_at
    return st


class Explainer:
    """Records calls and asserts the (after, through] window it is asked for."""

    def __init__(self, rows, window=WINDOW):
        self.rows = rows
        self.window = window
        self.calls = []

    def __call__(self, ym, after_ns, through_ns):
        self.calls.append((ym, after_ns, through_ns))
        assert (after_ns, through_ns) == self.window, f"unexpected window {(after_ns, through_ns)}"
        return self.rows


# The incident shape: 105 credit memos (GL +7385.04) and 4 true-up invoices (GL -4982.73) moved
# revenue by -2402.31 and the summed per-channel ntxn by +214 (credit memos span two channel rows).
INCIDENT_ROWS = [
    {"ttype": "CustCred", "chan": None, "amt": "7385.04", "ntxn": "105"},
    {"ttype": "CustInvc", "chan": "1", "amt": "-4982.73", "ntxn": "109"},
]


def test_fully_explained_movement_passes_with_note():
    ex = Explainer(INCIDENT_ROWS)
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(), ex)
    assert res["pass"] is True
    assert ex.calls == [("2026-09",) + WINDOW]  # only the moved month is queried
    note = res["explained_by_created_date"]["2026-09"]
    assert note["baseline"] == {"revenue": 803511.16, "ntxn": 2234}
    assert note["current"] == {"revenue": 801108.85, "ntxn": 2448}
    assert note["explained"]["revenue"] == -2402.31
    assert note["explained"]["ntxn"] == 214
    assert note["explained"]["top_types"]["CustCred"] == {"ntxn": 105, "revenue": -7385.04}
    assert note["residual"]["revenue"] == 0.0 and note["residual"]["ntxn"] == 0
    assert note["created_window_ns_clock"] == {"after": WINDOW[0], "through": WINDOW[1]}
    assert note["legacy_baseline"] is False
    assert note["baseline_source"] == "prior state income_queried_at"
    assert "explained by created date" in res["detail"]


def test_partially_explained_fails_on_residual():
    rows = [{"ttype": "CustCred", "amt": "7385.04", "ntxn": "105"}]  # invoices not explained
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(), Explainer(rows))
    assert res["pass"] is False
    note = res["explained_by_created_date"]["2026-09"]
    assert note["residual"]["revenue"] == pytest.approx(4982.73)
    assert note["residual"]["ntxn"] == 109
    assert "residual revenue 4982.73" in res["detail"] and "ntxn 109" in res["detail"]


def test_revenue_residual_alone_fails_even_when_count_is_explained():
    res = checks.check_g_closed_months_stable(_D(801108.85 - 8035.0, 2448), _prev(), Explainer(INCIDENT_ROWS))
    assert res["pass"] is False
    assert res["explained_by_created_date"]["2026-09"]["residual"]["revenue"] == pytest.approx(-8035.0)


def test_ntxn_residual_alone_fails_when_revenue_is_inside_tolerance():
    # revenue explained to the cent; 15 of 2234 transactions (0.67%) unexplained
    rows = [{"ttype": "CustCred", "amt": "7385.04", "ntxn": "105"},
            {"ttype": "CustInvc", "amt": "-4982.73", "ntxn": "94"}]
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(), Explainer(rows))
    note = res["explained_by_created_date"]["2026-09"]
    assert note["residual"]["revenue"] == 0.0 and note["residual"]["ntxn"] == 15
    assert res["pass"] is False


def test_negative_residual_over_explained_fails():
    # the window counted 100 more transactions than the month actually gained (e.g. documents
    # already in the baseline): residual -100 is as unexplained as +100
    rows = [{"ttype": "CustCred", "amt": "7385.04", "ntxn": "105"},
            {"ttype": "CustInvc", "amt": "-4982.73", "ntxn": "209"}]
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(), Explainer(rows))
    assert res["pass"] is False
    assert res["explained_by_created_date"]["2026-09"]["residual"]["ntxn"] == -100


def test_deletion_with_nothing_created_fails():
    ex = Explainer([])
    res = checks.check_g_closed_months_stable(_D(803511.16 - 5000.0, 2234 - 40), _prev(), ex)
    assert res["pass"] is False
    assert len(ex.calls) == 1
    note = res["explained_by_created_date"]["2026-09"]
    assert note["explained"]["ntxn"] == 0 and note["residual"]["ntxn"] == -40


def test_movement_inside_tolerance_never_queries():
    ex = Explainer(INCIDENT_ROWS)
    res = checks.check_g_closed_months_stable(_D(803511.16 * 1.001, 2239), _prev(), ex)  # +0.1%, +0.22%
    assert res["pass"] is True
    assert ex.calls == []
    assert "explained_by_created_date" not in res


def test_residual_within_tolerance_passes():
    rows = [{"ttype": "CustCred", "amt": "7385.04", "ntxn": "105"},
            {"ttype": "CustInvc", "amt": "-4982.73", "ntxn": "104"}]
    res = checks.check_g_closed_months_stable(_D(801108.85 + 800.0, 2448), _prev(), Explainer(rows))
    assert res["pass"] is True
    assert res["explained_by_created_date"]["2026-09"]["residual"]["ntxn"] == 5


def test_known_artifact_month_still_exempt_and_not_queried():
    ex = Explainer([])
    res = checks.check_g_closed_months_stable(_D(700000.0, 2448, known=[{"ship_day": "2026-09-19"}]), _prev(), ex)
    assert res["pass"] is True and ex.calls == []
    assert "covered by meta.known_artifacts" in res["detail"]


def test_explainer_error_fails_never_passes():
    def boom(ym, after, through):
        raise RuntimeError("SuiteQL 500")
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(), boom)
    assert res["pass"] is False
    assert "created-date query failed" in res["detail"]


def test_no_explainer_behaves_like_the_old_rule():
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(), None)
    assert res["pass"] is False
    assert "no created-date explainer available" in res["detail"]


# --- legacy baseline: state has pulled_at_mt but no income_queried_at (first run after deploy) ----

def test_legacy_baseline_passes_only_on_exactly_zero_residual():
    ex = Explainer(INCIDENT_ROWS, window=LEGACY_WINDOW)
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(income_at=None), ex)
    assert res["pass"] is True
    note = res["explained_by_created_date"]["2026-09"]
    assert note["legacy_baseline"] is True
    assert "exactly zero residual" in note["legacy_note"]
    assert note["baseline_source"].startswith("prior state pulled_at_mt (legacy")


def test_legacy_baseline_small_nonzero_residual_fails_closed():
    # 5 transactions unexplained: inside the 0.5% tolerance a current-format baseline would pass
    rows = [{"ttype": "CustCred", "amt": "7385.04", "ntxn": "105"},
            {"ttype": "CustInvc", "amt": "-4982.73", "ntxn": "104"}]
    ex = Explainer(rows, window=LEGACY_WINDOW)
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(income_at=None), ex)
    assert res["pass"] is False
    assert "not exactly zero (legacy baseline)" in res["detail"]
    ok = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(), Explainer(rows))
    assert ok["pass"] is True  # the same data passes against a current-format baseline


def test_run_log_fallback_used_when_state_has_no_time_at_all():
    ex = Explainer(INCIDENT_ROWS, window=LEGACY_WINDOW)
    res = checks.check_g_closed_months_stable(
        _D(801108.85, 2448), _prev(income_at=None, pulled_at=None), ex, baseline_fallback_pulled_at=BASELINE_PULL)
    assert res["pass"] is True
    assert res["explained_by_created_date"]["2026-09"]["baseline_source"].startswith("run_log fallback")


def test_state_value_wins_over_fallback():
    ex = Explainer(INCIDENT_ROWS)
    checks.check_g_closed_months_stable(
        _D(801108.85, 2448), _prev(), ex, baseline_fallback_pulled_at="2026-09-01T00:00:00-06:00")
    assert ex.calls[0][1] == WINDOW[0]


def test_no_baseline_time_anywhere_fails_closed():
    ex = Explainer(INCIDENT_ROWS)
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(income_at=None, pulled_at=None), ex)
    assert res["pass"] is False and ex.calls == []
    assert "no baseline pull time" in res["detail"]


def test_naive_or_garbage_timestamp_is_not_trusted():
    ex = Explainer(INCIDENT_ROWS)
    for bad in ("2026-10-02T03:11:33", "not a date", 12345):
        res = checks.check_g_closed_months_stable(_D(801108.85, 2448), _prev(income_at=bad, pulled_at=bad), ex)
        assert res["pass"] is False
    assert ex.calls == []


def test_current_run_without_income_queried_at_bounds_by_its_pulled_at():
    ex = Explainer(INCIDENT_ROWS, window=(WINDOW[0], "2026-10-03 04:05:00"))
    res = checks.check_g_closed_months_stable(_D(801108.85, 2448, cur_income_at=None), _prev(), ex)
    assert res["pass"] is True
    assert res["explained_by_created_date"]["2026-09"]["through_source"].startswith("this run pulled_at_mt")


def test_current_run_with_no_time_fails_closed():
    D = _D(801108.85, 2448, cur_income_at=None)
    D["meta"].pop("pulled_at_mt")
    ex = Explainer(INCIDENT_ROWS)
    res = checks.check_g_closed_months_stable(D, _prev(), ex)
    assert res["pass"] is False and ex.calls == []


def test_run_nightly_fallback_only_when_state_lacks_pulled_at(tmp_path, monkeypatch):
    import run_nightly
    calls = []
    last = datetime.datetime(2026, 10, 2, 9, 11, 33, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(run_nightly.refresh_gate, "read_run_log_last_success",
                        lambda sid: calls.append(sid) or last)
    args = SimpleNamespace(sheet="SHEET1")
    with_ts = tmp_path / "with.json"
    with_ts.write_text(json.dumps({"pulled_at_mt": BASELINE_PULL}), encoding="utf-8")
    without_ts = tmp_path / "without.json"
    without_ts.write_text(json.dumps({"closed_months": {}}), encoding="utf-8")

    assert run_nightly.baseline_pulled_at_fallback(args, str(with_ts)) is None
    assert calls == []
    assert run_nightly.baseline_pulled_at_fallback(args, str(without_ts)) == last.isoformat()
    assert run_nightly.baseline_pulled_at_fallback(args, None) == last.isoformat()
    monkeypatch.setattr(run_nightly.refresh_gate, "read_run_log_last_success", lambda sid: None)
    assert run_nightly.baseline_pulled_at_fallback(args, str(without_ts)) is None


# --- timezone: bounds are rendered on the clock SuiteQL uses for createddate (America/Chicago) --

@pytest.mark.parametrize("iso,expected", [
    ("2026-10-02T03:12:10-06:00", "2026-10-02 04:12:10"),       # MDT -> CDT: Mountain + 1 hour
    ("2026-10-02T09:12:10+00:00", "2026-10-02 04:12:10"),       # UTC -> CDT (-5)
    ("2026-12-15T10:05:00+00:00", "2026-12-15 04:05:00"),       # UTC -> CST (-6)
    ("2026-12-15T03:05:00-07:00", "2026-12-15 04:05:00"),       # MST -> CST: still Mountain + 1 hour
    ("2026-10-02T02:05:00-07:00", "2026-10-02 04:05:00"),       # a Pacific-labelled instant
    ("2026-03-08T07:59:00+00:00", "2026-03-08 01:59:00"),       # last minute of CST before spring forward
    ("2026-03-08T08:00:00+00:00", "2026-03-08 03:00:00"),       # first minute of CDT (02:00 never exists)
    ("2026-11-01T06:30:00+00:00", "2026-11-01 01:30:00"),       # 01:30 CDT, before fall back
    ("2026-11-01T08:30:00+00:00", "2026-11-01 02:30:00"),       # 02:30 CST, after fall back
])
def test_ns_clock_is_chicago_and_follows_dst(iso, expected):
    assert checks.ns_clock(datetime.datetime.fromisoformat(iso)) == expected


def test_ns_clock_is_one_hour_ahead_of_mountain_all_year():
    for iso in ("2026-01-15T10:00:00-07:00", "2026-07-15T10:00:00-06:00", "2026-03-09T12:00:00-06:00",
                "2026-11-02T12:00:00-07:00"):
        mt = datetime.datetime.fromisoformat(iso)
        ns = datetime.datetime.strptime(checks.ns_clock(mt), "%Y-%m-%d %H:%M:%S")
        assert ns - mt.replace(tzinfo=None) == datetime.timedelta(hours=1)


def test_window_edges_cross_midnight_on_the_netsuite_clock():
    prev = _prev(income_at="2026-10-02T23:01:00-06:00")
    after, through, info = checks.created_window(prev, {"income_queried_at": "2026-10-03T23:30:30-06:00"}, None)
    assert after == "2026-10-02 23:59:00"      # 00:59 CDT on 10/03 minus 2 minutes, previous NetSuite day
    assert through == "2026-10-04 00:30:30"    # 23:30 MT is already 00:30 the next day on the NetSuite clock
    assert info["legacy_baseline"] is False


# --- the shared query builder -------------------------------------------------------------

def _capture_sql(monkeypatch):
    seen = []
    monkeypatch.setattr(extract, "suiteql", lambda env, sql, **kw: seen.append(" ".join(sql.split())) or [])
    return seen


def test_income_query_default_is_unchanged_and_window_variant_adds_both_bounds(monkeypatch):
    seen = _capture_sql(monkeypatch)
    extract.income_by_channel_month({}, "2026-09-01", "2026-09-30")
    extract.income_by_channel_month({}, "2026-09-01", "2026-09-30", created_after_ns="2026-10-02 04:10:10",
                                    created_through_ns="2026-10-03 04:20:00", by_type=True)
    base, explained = seen
    assert "createddate" not in base and "t.type" not in base
    assert "AND t.createddate > TO_DATE('2026-10-02 04:10:10','YYYY-MM-DD HH24:MI:SS')" in explained
    assert "AND t.createddate <= TO_DATE('2026-10-03 04:20:00','YYYY-MM-DD HH24:MI:SS')" in explained
    assert "t.type AS ttype" in explained
    assert "GROUP BY TO_CHAR(t.trandate,'YYYY-MM'), tl.cseg_appf_channel, t.type" in explained
    for sql in seen:  # same revenue / ntxn definition
        assert "ai.posting = 'T' AND a.accttype = 'Income'" in sql
        assert "SUM(ai.amount) AS amt" in sql and "COUNT(DISTINCT t.id) AS ntxn" in sql


def test_explainer_queries_the_whole_calendar_month_with_both_bounds(monkeypatch):
    seen = []
    monkeypatch.setattr(extract, "income_by_channel_month",
                        lambda env, start, end, **kw: seen.append((start, end, kw)) or [])
    explainer = extract.make_created_since_explainer({}, [])
    explainer("2026-02", "2026-03-01 00:00:00", "2026-03-02 00:00:00")
    explainer("2026-09", WINDOW[0], WINDOW[1])
    assert seen[0][:2] == ("2026-02-01", "2026-02-28")
    assert seen[1] == ("2026-09-01", "2026-09-30",
                       {"created_after_ns": WINDOW[0], "created_through_ns": WINDOW[1], "by_type": True})


def test_explainer_counts_only_the_channels_the_month_totals_count(monkeypatch):
    rows = [{"chan": "1", "ttype": "CustInvc", "amt": "-100", "ntxn": "1"},
            {"chan": None, "ttype": "CustCred", "amt": "40", "ntxn": "2"},
            {"chan": "999", "ttype": "CustInvc", "amt": "-5000", "ntxn": "9"}]  # not in the picklist
    monkeypatch.setattr(extract, "income_by_channel_month", lambda env, start, end, **kw: rows)
    channels = [{"id": 1, "name": "Amazon"}, {"id": 5, "name": "Spikeball.com"}]
    got = extract.make_created_since_explainer({}, channels)("2026-09", "a", "b")
    assert [r["chan"] for r in got] == ["1", None]
    assert extract.counted_channel_ids(channels) == {1, 5, None}


def test_month_totals_and_explainer_share_the_same_channel_set(monkeypatch):
    monkeypatch.setattr(extract, "income_by_channel_month", lambda env, s, e, **kw: [
        {"ym": "2026-09", "chan": "1", "amt": "-100", "ntxn": "1"},
        {"ym": "2026-09", "chan": "999", "amt": "-5000", "ntxn": "9"}])
    monkeypatch.setattr(extract, "cogs_by_channel_month", lambda env, s, e: [])
    monkeypatch.setattr(extract, "_try_gross", lambda *a, **k: None)
    D = {"trailing_start": "2026-09-01", "asof": "2026-09-30", "py_trailing_start": "2025-09-01",
         "py_asof": "2025-09-30", "trailing_months": ["2026-09"]}
    out, _n = extract.build_pnl_by_channel_month({}, D, [{"id": 1, "name": "Amazon"}])
    assert sum(r["ntxn"] for r in out) == 1            # channel 999 is not in any month total
    assert "income_queried_at" in D                    # stamped before the income query
    datetime.datetime.fromisoformat(D["income_queried_at"])


def test_income_queried_at_is_stamped_before_the_query(monkeypatch):
    order = []
    monkeypatch.setattr(extract, "income_by_channel_month",
                        lambda env, s, e, **kw: order.append(("query", dict(D).get("income_queried_at"))) or [])
    monkeypatch.setattr(extract, "cogs_by_channel_month", lambda env, s, e: [])
    monkeypatch.setattr(extract, "_try_gross", lambda *a, **k: None)
    D = {"trailing_start": "2026-09-01", "asof": "2026-09-30", "py_trailing_start": "2025-09-01",
         "py_asof": "2025-09-30", "trailing_months": ["2026-09"]}
    extract.build_pnl_by_channel_month({}, D, [])
    assert order[0][1] is not None  # already stamped when the first income query ran


def test_write_state_persists_income_queried_at():
    out = {"meta": {"trailing_months": ["2026-08", "2026-09"], "pulled_at_mt": BASELINE_PULL,
                    "income_queried_at": BASELINE_INCOME_AT, "sections": {}},
           "pnl_by_channel_month": []}
    st = extract.build_write_state(out)
    assert st["income_queried_at"] == BASELINE_INCOME_AT and st["pulled_at_mt"] == BASELINE_PULL


def test_run_checks_passes_explainer_through():
    ex = Explainer(INCIDENT_ROWS)
    out = checks.run_checks(_D(801108.85, 2448), _prev(), explainer=ex)
    assert out["g_closed_months_stable"]["pass"] is True
    assert ex.calls


def test_extract_main_passes_the_explainer_and_stamps_meta(tmp_path, monkeypatch):
    """extract.main with every NetSuite section erroring (recorded per section, as in a real
    outage): run_checks must still receive an explainer and the baseline fallback, and the
    output meta must carry income_queried_at."""
    captured = {}

    def fake_run_checks(output, prev_state, explainer=None, baseline_fallback_pulled_at=None):
        captured.update(explainer=explainer, fallback=baseline_fallback_pulled_at, prev=prev_state)
        return {"all_pass": True}

    def no_netsuite(*a, **k):
        raise extract.SuiteQLError("offline in test")

    monkeypatch.setattr(extract, "run_checks", fake_run_checks)
    monkeypatch.setattr(extract, "load_env", lambda: {})
    monkeypatch.setattr(extract, "suiteql", no_netsuite)
    monkeypatch.setattr(extract, "try_suiteql", lambda *a, **k: (None, "offline in test"))
    monkeypatch.setattr(extract, "run_checks_v2", lambda output: {"v2_pass": True})
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(_prev()), encoding="utf-8")
    out = tmp_path / "latest.json"
    monkeypatch.setattr("sys.argv", ["extract.py", "--out", str(out), "--skip-amazon", "--prev-state", str(prev),
                                     "--baseline-pulled-at", BASELINE_PULL,
                                     "--write-state", str(tmp_path / "state.json")])
    extract.main()
    assert callable(captured["explainer"])
    assert captured["fallback"] == BASELINE_PULL
    assert captured["prev"]["income_queried_at"] == BASELINE_INCOME_AT
    meta = json.loads(out.read_text(encoding="utf-8"))["meta"]
    datetime.datetime.fromisoformat(meta["income_queried_at"])
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["income_queried_at"] == meta["income_queried_at"]


# --- run_nightly: a failed run must not become the local fallback baseline ------------------

def test_failed_checks_do_not_overwrite_the_local_state_fallback(tmp_path, monkeypatch):
    import run_nightly
    spike = tmp_path / "spike"
    (spike / "data").mkdir(parents=True)
    good = spike / "data" / "state_prev.json"
    good.write_text('{"pulled_at_mt": "good baseline"}', encoding="utf-8")
    state_new = spike / "data" / "state_new.json"
    state_new.write_text('{"pulled_at_mt": "failed run"}', encoding="utf-8")
    out_path = spike / "data" / "latest.json"
    out_path.write_text(json.dumps({"meta": {"pulled_at_mt": CUR_PULL, "checks": {
        "all_pass": False, "g_closed_months_stable": {"pass": False, "detail": "residual"}}}}), encoding="utf-8")

    monkeypatch.setattr(run_nightly, "SPIKE", spike)
    monkeypatch.setattr(run_nightly.state_sync, "download", lambda: None)
    monkeypatch.setattr(run_nightly, "fetch_prev_state", lambda args: None)
    monkeypatch.setattr(run_nightly, "fetch_demand_plan_step", lambda args: (None, None))
    monkeypatch.setattr(run_nightly, "fetch_revenue_plan_step", lambda args: None)
    monkeypatch.setattr(run_nightly, "run_extract", lambda *a, **k: (
        SimpleNamespace(returncode=1, stdout="", stderr=""), out_path, state_new))
    args = SimpleNamespace(no_alert=True, dry_run=True, sheet=None)
    code, ok_for_republish, pulled = run_nightly.run_pipeline(args)
    assert (code, ok_for_republish, pulled) == (2, False, None)
    assert json.loads(good.read_text(encoding="utf-8")) == {"pulled_at_mt": "good baseline"}


def test_passing_run_refreshes_the_local_state_fallback(tmp_path, monkeypatch):
    import run_nightly
    spike = tmp_path / "spike"
    (spike / "data").mkdir(parents=True)
    state_new = spike / "data" / "state_new.json"
    state_new.write_text('{"pulled_at_mt": "new"}', encoding="utf-8")
    monkeypatch.setattr(run_nightly, "SPIKE", spike)
    assert run_nightly.refresh_local_state_fallback(state_new) is True
    assert json.loads((spike / "data" / "state_prev.json").read_text(encoding="utf-8")) == {"pulled_at_mt": "new"}
    assert run_nightly.refresh_local_state_fallback(spike / "data" / "missing.json") is False
