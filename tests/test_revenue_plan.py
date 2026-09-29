"""Revenue Plan tab reader (spike/revenue_plan.py): the pure grid parser, build_outputs(),
the stale-snapshot resolver, and run_nightly.fetch_revenue_plan_step's never-fatal fallback.
Hermetic: no network, no Doppler, files under tmp_path.

Run: python -m pytest tests/test_revenue_plan.py -q
"""
import json
import os

# See test_refresh_gate.py: run_nightly's import chain touches doppler_env at import time.
os.environ.setdefault("NETSUITE_ACCOUNT_ID", "test-sentinel-account")
os.environ.setdefault("SPIKEBALL_OAUTH_CLIENT_ID", "000000000000-test-sentinel.apps.googleusercontent.com")

import argparse
from types import SimpleNamespace

import pytest

import revenue_plan
import run_nightly

CFG = {
    "groups": [
        {"key": "amazon", "label": "Amazon"},
        {"key": "wholesale", "label": "Wholesale"},
        {"key": "dtc", "label": "Spikeball.com"},
        {"key": "other_b2b", "label": "Other B2B"},
    ],
    "unassigned": {"key": "unassigned", "label": "Unassigned"},
}

HEADER = ["Channel", "Series", "2026-01", "2026-02", "2026-03"]


def grid(*rows, notes=()):
    return [list(n) for n in notes] + [HEADER] + [list(r) for r in rows]


# --------------------------------------------------------------------------- parser

def test_parse_basic_with_note_rows_and_header_row_number():
    g = grid(["Amazon", "Plan", "100", "200", "300"], notes=[["Note one"], ["Note two", "x"], []])
    r = revenue_plan.parse_revenue_plan_grid(g, CFG)
    assert r["valid"] and r["error"] is None
    assert r["header_row"] == 4
    assert r["note"] == "Note one | Note two x"
    assert r["month_columns"] == ["2026-01", "2026-02", "2026-03"]
    assert r["rows"] == [{"key": "amazon", "label": "Amazon", "series": "plan",
                          "months": {"2026-01": 100.0, "2026-02": 200.0, "2026-03": 300.0}}]
    assert r["dropped_rows"] == []


def test_header_detection_is_case_insensitive_and_trimmed():
    g = [["  CHANNEL "] + HEADER[1:], ["amazon", "PLAN", "5", "", ""]]
    r = revenue_plan.parse_revenue_plan_grid(g, CFG)
    assert r["valid"] and r["header_row"] == 1
    assert r["rows"][0]["series"] == "plan"


@pytest.mark.parametrize("cell,key,label", [
    ("Amazon", "amazon", "Amazon"), ("amazon", "amazon", "Amazon"),
    ("Spikeball.com", "dtc", "Spikeball.com"), ("DTC", "dtc", "Spikeball.com"),
    ("other b2b", "other_b2b", "Other B2B"), ("other_b2b", "other_b2b", "Other B2B"),
    ("WHOLESALE", "wholesale", "Wholesale"),
])
def test_channel_matches_label_or_key(cell, key, label):
    r = revenue_plan.parse_revenue_plan_grid(grid([cell, "Plan", "1", "", ""]), CFG)
    assert (r["rows"][0]["key"], r["rows"][0]["label"]) == (key, label)


def test_amount_formats_dollar_comma_space_and_parentheses():
    g = grid(["Amazon", "Plan", "$1,234.50", " 2 000 ", "(300)"])
    r = revenue_plan.parse_revenue_plan_grid(g, CFG)
    assert r["rows"][0]["months"] == {"2026-01": 1234.5, "2026-02": 2000.0, "2026-03": -300.0}


def test_numeric_cells_and_blank_cells():
    g = grid(["Amazon", "Plan", 10, None, ""], ["Wholesale", "Plan"])
    r = revenue_plan.parse_revenue_plan_grid(g, CFG)
    assert r["rows"][0]["months"] == {"2026-01": 10.0}
    assert r["rows"][1]["months"] == {}


def test_dropped_rows_report_reason_and_sheet_row_number():
    g = grid(["Mystery", "Plan", "1", "", ""],
             ["Amazon", "Plan", "12abc", "", ""],
             ["", "Plan", "1", "", ""],
             ["Amazon", "", "1", "", ""],
             ["Wholesale", "Plan", "nan", "", ""],
             ["Amazon", "Plan", "7", "", ""],
             ["Amazon", "Plan", "8", "", ""])
    r = revenue_plan.parse_revenue_plan_grid(g, CFG)
    assert r["valid"]
    assert [d["row"] for d in r["dropped_rows"]] == [2, 3, 4, 5, 6, 8]
    reasons = [d["reason"] for d in r["dropped_rows"]]
    assert "unknown channel 'Mystery'" in reasons[0]
    assert "unparsable amount '12abc' in 2026-01" in reasons[1]
    assert reasons[2] == "blank Channel"
    assert "blank Series" in reasons[3]
    assert "unparsable" in reasons[4]
    assert "duplicate" in reasons[5]
    assert len(r["rows"]) == 1 and r["rows"][0]["months"]["2026-01"] == 7.0


def test_fully_blank_rows_are_skipped_silently():
    r = revenue_plan.parse_revenue_plan_grid(grid([], ["", ""], ["Amazon", "Plan", "1", "", ""]), CFG)
    assert r["dropped_rows"] == [] and len(r["rows"]) == 1


def test_extra_series_kept_lowercased():
    r = revenue_plan.parse_revenue_plan_grid(
        grid(["Amazon", "Plan", "1", "", ""], ["Amazon", "Forecast", "2", "", ""]), CFG)
    assert [x["series"] for x in r["rows"]] == ["plan", "forecast"]


@pytest.mark.parametrize("cell", ["Total", "TOTAL", " total ", "tOtAl"])
def test_total_channel_parses_case_insensitive_and_trimmed_for_any_series(cell):
    r = revenue_plan.parse_revenue_plan_grid(
        grid([cell, "Forecast", "1", "", ""], [cell, "Plan", "2", "", ""]), CFG)
    assert r["valid"] and r["dropped_rows"] == []
    assert r["rows"] == [
        {"key": "total", "label": "Total", "series": "forecast", "months": {"2026-01": 1.0}},
        {"key": "total", "label": "Total", "series": "plan", "months": {"2026-01": 2.0}}]


def test_total_channel_duplicate_within_a_series_is_rejected():
    r = revenue_plan.parse_revenue_plan_grid(
        grid(["Total", "Forecast", "1", "", ""], ["total", "forecast", "9", "", ""]), CFG)
    assert len(r["rows"]) == 1 and r["rows"][0]["months"] == {"2026-01": 1.0}
    assert r["dropped_rows"] == [{"row": 3, "reason": "duplicate row for channel 'total' series 'forecast'"}]


def test_total_channel_is_not_a_rollup_lookup_hit():
    assert "total" not in revenue_plan._channel_lookup(CFG)


def test_columns_keyed_by_label_not_position():
    g = [["Series", "Channel", "2026-06", "junk", "2026-05"], ["x"], ["Plan", "Amazon", "6", "", "5"]]
    g[0] = ["Channel", "junk", "2026-06", "Series", "2026-05"]
    g[2] = ["Amazon", "", "6", "Plan", "5"]
    r = revenue_plan.parse_revenue_plan_grid(g[:1] + g[2:], CFG)
    assert r["month_columns"] == ["2026-06", "2026-05"]
    assert r["rows"][0]["months"] == {"2026-06": 6.0, "2026-05": 5.0}


@pytest.mark.parametrize("values,frag", [
    ([], "empty"),
    ([["Amazon", "Plan", "1"]], "no header row"),
    ([["Channel", "2026-01"]], "Series"),
    ([["Channel", "Series", "Jan"]], "month column"),
    ([["Channel", "Series", "2026-13", "2026-00"]], "month column"),
])
def test_schema_problems_are_invalid_not_raised(values, frag):
    r = revenue_plan.parse_revenue_plan_grid(values, CFG)
    assert r["valid"] is False and frag in r["error"]
    assert r["rows"] == []


# --------------------------------------------------------------------------- build_outputs

def plan_json(rows, cols=("2026-01", "2026-02", "2026-03"), **extra):
    d = {"valid": True, "error": None, "note": "", "month_columns": list(cols), "rows": rows,
         "dropped_rows": [], "stale": False, "fetched_at_mt": "2026-09-29T03:00:00-06:00"}
    d.update(extra)
    return d


def prow(key, label, months, series="plan"):
    return {"key": key, "label": label, "series": series, "months": months}


def rb(ym, key, gross, revenue=None):
    row = {"ym": ym, "key": key, "label": key}
    if gross is not None:
        row["gross_revenue"] = gross
    if revenue is not None:
        row["revenue"] = revenue
    return row


def pva(out, ym, key):
    return next(r for r in out["plan_vs_actual_month"] if r["ym"] == ym and r["key"] == key)


def full_year_rollup(year=2026):
    rows = []
    for m in range(1, 13):
        ym = f"{year}-{m:02d}"
        rows += [rb(ym, "amazon", 100.0), rb(ym, "wholesale", 50.0), rb(ym, "dtc", 25.0),
                 rb(ym, "other_b2b", 5.0)]
    return rows


def test_basis_by_asof_month_and_variance():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 80.0, "2026-02": 100.0, "2026-03": 90.0})])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-02-10", CFG)
    jan, feb, mar = pva(out, "2026-01", "amazon"), pva(out, "2026-02", "amazon"), pva(out, "2026-03", "amazon")
    assert (jan["basis"], jan["actual_gross"], jan["variance"], jan["variance_pct"]) == ("actual", 100.0, 20.0, 25.0)
    assert (feb["basis"], feb["variance"], feb["variance_pct"]) == ("open", 0.0, 0.0)
    assert (mar["basis"], mar["actual_gross"], mar["variance"], mar["variance_pct"]) == ("future", None, None, None)
    assert mar["plan_gross"] == 90.0


def test_twelve_months_and_key_order_with_total_last():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 1.0})])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    rows = out["plan_vs_actual_month"]
    assert len(rows) == 12 * 5
    assert [r["key"] for r in rows[:5]] == ["amazon", "wholesale", "dtc", "other_b2b", "total"]
    assert rows[4]["label"] == "Total"
    assert {r["ym"] for r in rows} == {f"2026-{m:02d}" for m in range(1, 13)}


def test_no_plan_basis_when_key_has_actuals_but_no_plan():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 80.0})])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    r = pva(out, "2026-02", "wholesale")
    assert (r["basis"], r["plan_gross"], r["actual_gross"], r["variance"]) == ("no_plan", None, 50.0, None)


def test_total_plan_and_actual_include_all_keys_and_unassigned():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 60.0}), prow("dtc", "Spikeball.com", {"2026-01": 40.0})])
    rollup = full_year_rollup() + [rb("2026-01", "unassigned", 3.0), rb("2026-01", "total", 999.0)]
    out = revenue_plan.build_outputs(pj, rollup, "2026-06-01", CFG)
    t = pva(out, "2026-01", "total")
    assert t["plan_gross"] == 100.0
    assert t["actual_gross"] == 100.0 + 50.0 + 25.0 + 5.0 + 3.0
    assert t["variance"] == 83.0 and t["variance_pct"] == 83.0
    assert pva(out, "2026-01", "unassigned")["label"] == "Unassigned"
    assert pva(out, "2026-02", "unassigned")["actual_gross"] == 0.0
    assert pva(out, "2026-02", "total")["actual_gross"] == 180.0


PLAN_FIELDS = ("ym", "key", "label", "plan_gross", "actual_gross", "variance", "variance_pct", "basis")
FORECAST_FIELDS = ("forecast_gross", "variance_vs_forecast", "variance_vs_forecast_pct")


def test_forecast_feeds_forecast_gross_and_never_plan_gross():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 80.0}),
                    prow("amazon", "Amazon", {"2026-01": 125.0}, series="forecast")])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    r = pva(out, "2026-01", "amazon")
    assert (r["plan_gross"], r["variance"], r["variance_pct"]) == (80.0, 20.0, 25.0)
    assert (r["forecast_gross"], r["variance_vs_forecast"], r["variance_vs_forecast_pct"]) == (125.0, -25.0, -20.0)
    assert out["revenue_plan_meta"]["series"] == ["forecast", "plan"]
    assert [(m["series"], m["plan_gross"]) for m in out["revenue_plan_month"]] == [("forecast", 125.0), ("plan", 80.0)]


def test_total_forecast_row_alone_fills_total_rows_and_leaves_channels_null():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 80.0, "2026-02": 90.0}),
                    prow("total", "Total", {"2026-01": 150.0, "2026-02": 160.0}, series="forecast")])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    t = pva(out, "2026-01", "total")
    assert (t["plan_gross"], t["actual_gross"], t["forecast_gross"]) == (80.0, 180.0, 150.0)
    assert (t["variance_vs_forecast"], t["variance_vs_forecast_pct"]) == (30.0, 20.0)
    for key in ("amazon", "wholesale", "dtc", "other_b2b"):
        r = pva(out, "2026-01", key)
        assert (r["forecast_gross"], r["variance_vs_forecast"], r["variance_vs_forecast_pct"]) == (None, None, None)
    # the Total forecast row is an ordinary revenue_plan_month row
    assert {"ym": "2026-02", "key": "total", "label": "Total", "series": "forecast",
            "plan_gross": 160.0} in out["revenue_plan_month"]


def test_channel_forecast_rows_sum_into_total_when_no_total_row():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 80.0}),
                    prow("amazon", "Amazon", {"2026-01": 100.5}, series="forecast"),
                    prow("dtc", "Spikeball.com", {"2026-01": 20.25}, series="forecast")])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    assert pva(out, "2026-01", "total")["forecast_gross"] == 120.75
    assert pva(out, "2026-01", "amazon")["forecast_gross"] == 100.5
    assert pva(out, "2026-01", "wholesale")["forecast_gross"] is None
    assert pva(out, "2026-02", "total")["forecast_gross"] is None


def test_explicit_total_forecast_row_wins_over_channel_sum():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 100.0}, series="forecast"),
                    prow("dtc", "Spikeball.com", {"2026-01": 20.0}, series="forecast"),
                    prow("total", "Total", {"2026-01": 999.0}, series="forecast")])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    assert pva(out, "2026-01", "total")["forecast_gross"] == 999.0
    assert pva(out, "2026-01", "amazon")["forecast_gross"] == 100.0


def test_explicit_total_plan_row_wins_over_channel_sum_too():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 100.0}),
                    prow("total", "Total", {"2026-01": 400.0})])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    t = pva(out, "2026-01", "total")
    assert (t["plan_gross"], t["variance"], t["variance_pct"]) == (400.0, -220.0, -55.0)
    assert pva(out, "2026-01", "amazon")["plan_gross"] == 100.0
    assert pva(out, "2026-01", "wholesale")["plan_gross"] is None


def test_plan_output_unchanged_when_forecast_rows_present():
    plan_rows = [prow("amazon", "Amazon", {f"2026-{m:02d}": 80.0 + m for m in range(1, 13)}),
                 prow("dtc", "Spikeball.com", {f"2026-{m:02d}": 20.0 + m for m in range(1, 13)}),
                 prow("wholesale", "Wholesale", {"2026-01": 0.0, "2026-03": 55.5})]
    forecast_rows = [prow("total", "Total", {f"2026-{m:02d}": 500.0 + m for m in range(1, 13)}, series="forecast"),
                     prow("amazon", "Amazon", {"2026-01": 77.0}, series="forecast")]
    rollup = full_year_rollup() + [rb("2026-01", "unassigned", 3.0)]
    without = revenue_plan.build_outputs(plan_json(plan_rows), rollup, "2026-06-15", CFG)
    with_f = revenue_plan.build_outputs(plan_json(plan_rows + forecast_rows), rollup, "2026-06-15", CFG)
    assert len(with_f["plan_vs_actual_month"]) == len(without["plan_vs_actual_month"]) == 12 * 6
    for a, b in zip(without["plan_vs_actual_month"], with_f["plan_vs_actual_month"]):
        assert {k: a[k] for k in PLAN_FIELDS} == {k: b[k] for k in PLAN_FIELDS}
    assert all(r[k] is None for r in without["plan_vs_actual_month"] for k in FORECAST_FIELDS)
    assert any(r["forecast_gross"] is not None for r in with_f["plan_vs_actual_month"])
    plan_month_rows = [r for r in with_f["revenue_plan_month"] if r["series"] == "plan"]
    assert plan_month_rows == without["revenue_plan_month"]
    m_without, m_with = without["revenue_plan_meta"], with_f["revenue_plan_meta"]
    for k in m_without:
        if k not in ("series", "row_count", "forecast_available", "forecast_grain"):
            assert m_without[k] == m_with[k], k


@pytest.mark.parametrize("rows,available,grain", [
    ([prow("amazon", "Amazon", {"2026-01": 1.0})], False, None),
    ([prow("total", "Total", {"2026-01": 1.0}, series="forecast")], True, "total"),
    ([prow("amazon", "Amazon", {"2026-01": 1.0}, series="forecast")], True, "channel"),
    ([prow("amazon", "Amazon", {"2026-01": 1.0}, series="forecast"),
      prow("total", "Total", {"2026-01": 1.0}, series="forecast")], True, "mixed"),
    ([prow("total", "Total", {"2026-01": 1.0})], False, None),
    ([prow("amazon", "Amazon", {}, series="forecast")], True, "channel"),
])
def test_meta_forecast_available_and_grain(rows, available, grain):
    meta = revenue_plan.build_outputs(plan_json(rows), full_year_rollup(), "2026-06-01", CFG)["revenue_plan_meta"]
    assert (meta["forecast_available"], meta["forecast_grain"]) == (available, grain)


def test_meta_forecast_fields_when_plan_invalid_or_absent():
    for bad in (None, {}, {"valid": False, "error": "x"}):
        meta = revenue_plan.build_outputs(bad, full_year_rollup(), "2026-06-01", CFG)["revenue_plan_meta"]
        assert (meta["forecast_available"], meta["forecast_grain"]) == (False, None)


def test_variance_vs_forecast_rounding_and_null_rules():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 33.333, "2026-02": 0.0, "2026-04": 10.0}, series="forecast")],
                   cols=("2026-01", "2026-02", "2026-03", "2026-04"))
    rollup = full_year_rollup() + [rb("2026-01", "amazon", 0.004)]
    out = revenue_plan.build_outputs(pj, rollup, "2026-03-10", CFG)
    jan = pva(out, "2026-01", "amazon")
    assert jan["forecast_gross"] == 33.33                    # 2dp money rounding
    assert jan["variance_vs_forecast"] == 66.67              # 100.004 - 33.33 -> 2dp
    assert jan["variance_vs_forecast_pct"] == 200.0          # 1dp
    feb = pva(out, "2026-02", "amazon")
    assert (feb["forecast_gross"], feb["variance_vs_forecast"], feb["variance_vs_forecast_pct"]) == (0.0, 100.0, None)
    mar = pva(out, "2026-03", "amazon")                       # no forecast for March
    assert (mar["forecast_gross"], mar["variance_vs_forecast"], mar["variance_vs_forecast_pct"]) == (None, None, None)
    apr = pva(out, "2026-04", "amazon")                       # future month: forecast shown, no variance
    assert (apr["basis"], apr["forecast_gross"], apr["variance_vs_forecast"], apr["variance_vs_forecast_pct"]) == (
        "future", 10.0, None, None)
    assert jan["basis"] == "no_plan" and jan["plan_gross"] is None and jan["variance"] is None


def test_forecast_variance_null_when_month_outside_actuals_window():
    pj = plan_json([prow("total", "Total", {"2026-01": 50.0}, series="forecast")])
    out = revenue_plan.build_outputs(pj, [rb("2026-05", "amazon", 5.0)], "2026-06-15", CFG)
    t = pva(out, "2026-01", "total")
    assert (t["forecast_gross"], t["actual_gross"], t["variance_vs_forecast"]) == (50.0, None, None)


def test_every_plan_vs_actual_row_carries_the_forecast_fields():
    out = revenue_plan.build_outputs(plan_json([prow("amazon", "Amazon", {"2026-01": 1.0})]),
                                     full_year_rollup(), "2026-06-01", CFG)
    want = set(PLAN_FIELDS) | set(FORECAST_FIELDS)
    assert all(set(r) == want for r in out["plan_vs_actual_month"])


def test_revenue_plan_month_sorted_by_ym_then_rollup_order():
    pj = plan_json([prow("dtc", "Spikeball.com", {"2026-02": 1.0, "2026-01": 2.0}),
                    prow("amazon", "Amazon", {"2026-02": 3.0, "2026-01": 4.0})])
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    assert [(r["ym"], r["key"]) for r in out["revenue_plan_month"]] == [
        ("2026-01", "amazon"), ("2026-01", "dtc"), ("2026-02", "amazon"), ("2026-02", "dtc")]
    assert set(out["revenue_plan_month"][0]) == {"ym", "key", "label", "series", "plan_gross"}


def test_variance_pct_null_when_plan_zero():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 0.0})])
    r = pva(revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG), "2026-01", "amazon")
    assert r["variance"] == 100.0 and r["variance_pct"] is None and r["basis"] == "actual"


def test_falls_back_to_revenue_when_gross_absent_and_says_so():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 80.0})])
    rollup = [rb("2026-01", "amazon", None, revenue=90.0)]
    out = revenue_plan.build_outputs(pj, rollup, "2026-06-01", CFG)
    assert pva(out, "2026-01", "amazon")["actual_gross"] == 90.0
    assert "net revenue" in out["revenue_plan_meta"]["note"]


def test_gross_revenue_preferred_over_revenue():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 80.0})])
    rollup = [rb("2026-01", "amazon", 120.0, revenue=90.0)]
    out = revenue_plan.build_outputs(pj, rollup, "2026-06-01", CFG)
    assert pva(out, "2026-01", "amazon")["actual_gross"] == 120.0
    assert "net revenue" not in out["revenue_plan_meta"]["note"]


@pytest.mark.parametrize("bad", [None, {}, {"valid": False, "error": "HTTP 403: no"}])
def test_none_or_invalid_plan_lists_actuals_only(bad):
    rollup = [rb("2026-01", "amazon", 10.0), rb("2026-02", "amazon", 20.0)]
    out = revenue_plan.build_outputs(bad, rollup, "2026-02-15", CFG)
    meta = out["revenue_plan_meta"]
    assert meta["valid"] is False and meta["year"] is None and meta["error"]
    assert out["revenue_plan_month"] == []
    rows = out["plan_vs_actual_month"]
    assert {r["ym"] for r in rows} == {"2026-01", "2026-02"}
    assert all(r["plan_gross"] is None and r["variance"] is None for r in rows)
    assert pva(out, "2026-01", "amazon")["basis"] == "no_plan"
    assert pva(out, "2026-01", "amazon")["actual_gross"] == 10.0


def test_meta_shape_and_year_selection():
    pj = plan_json([prow("amazon", "Amazon", {"2027-01": 1.0})], cols=("2027-01",),
                   dropped_rows=[{"row": 9, "reason": "x"}], note="hello", stale=True)
    out = revenue_plan.build_outputs(pj, [rb("2026-09", "amazon", 1.0)], "2026-09-07", CFG)
    meta = out["revenue_plan_meta"]
    assert set(meta) == {"valid", "stale", "fetched_at_mt", "source", "year", "series", "month_columns",
                         "row_count", "dropped_rows", "note", "error", "forecast_available", "forecast_grain"}
    assert meta["year"] == 2027 and meta["stale"] is True and meta["source"] == "Revenue Plan"
    assert meta["dropped_rows"] == [{"row": 9, "reason": "x"}] and meta["row_count"] == 1
    assert meta["note"].startswith("hello")
    # 2027 plan, as-of 2026: every 2027 month is future
    assert all(r["basis"] == "future" for r in out["plan_vs_actual_month"])


def test_asof_year_preferred_when_present():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 1.0, "2027-01": 2.0})], cols=("2026-01", "2027-01"))
    out = revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG)
    assert out["revenue_plan_meta"]["year"] == 2026


def test_months_outside_actuals_window_are_flagged():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 1.0})])
    rollup = [rb("2026-05", "amazon", 5.0), rb("2026-06", "amazon", 6.0)]
    out = revenue_plan.build_outputs(pj, rollup, "2026-06-15", CFG)
    r = pva(out, "2026-01", "amazon")
    assert r["actual_gross"] is None and r["basis"] == "actual" and r["variance"] is None
    assert "2026-01" in out["revenue_plan_meta"]["note"]
    assert pva(out, "2026-05", "amazon")["actual_gross"] == 5.0
    assert pva(out, "2026-06", "amazon")["basis"] == "no_plan"


def test_outputs_are_json_serializable():
    pj = plan_json([prow("amazon", "Amazon", {"2026-01": 80.0})])
    json.dumps(revenue_plan.build_outputs(pj, full_year_rollup(), "2026-06-01", CFG))


# --------------------------------------------------------------------------- resolve + step

def test_resolve_snapshot_fresh_valid_wins():
    cur = plan_json([prow("amazon", "Amazon", {"2026-01": 1.0})])
    assert revenue_plan.resolve_snapshot(cur, None)["stale"] is False


def test_resolve_snapshot_falls_back_to_prev_stale_and_keeps_rows():
    prev = plan_json([prow("amazon", "Amazon", {"2026-01": 1.0})])
    out = revenue_plan.resolve_snapshot({"valid": False, "error": "HTTP 500"}, prev)
    assert out["valid"] is True and out["stale"] is True and out["error"] == "HTTP 500"
    assert out["rows"] == prev["rows"] and out["fetched_at_mt"] == prev["fetched_at_mt"]


def test_resolve_snapshot_carries_forecast_and_total_rows_like_plan_rows():
    prev = plan_json([prow("amazon", "Amazon", {"2026-01": 1.0}),
                      prow("total", "Total", {"2026-01": 9.0}, series="forecast")])
    out = revenue_plan.resolve_snapshot({"valid": False, "error": "HTTP 500"}, prev)
    assert out["stale"] is True and out["rows"] == prev["rows"]
    built = revenue_plan.build_outputs(out, full_year_rollup(), "2026-06-01", CFG)
    assert built["revenue_plan_meta"]["stale"] is True
    assert built["revenue_plan_meta"]["forecast_grain"] == "total"
    assert pva(built, "2026-01", "total")["forecast_gross"] == 9.0


def test_resolve_snapshot_invalid_without_prev():
    out = revenue_plan.resolve_snapshot({"valid": False, "error": "boom"}, None)
    assert out["valid"] is False and out["error"] == "boom"
    out2 = revenue_plan.resolve_snapshot({"valid": False, "error": "boom"}, {"valid": False, "rows": []})
    assert out2["valid"] is False


@pytest.fixture
def step_env(tmp_path, monkeypatch):
    monkeypatch.setattr(run_nightly, "SPIKE", tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path


def _args():
    return argparse.Namespace(sheet="SHEET")


def _fake_run_step(writes=None, rc=0):
    def fake(cmd, label, cwd=None):
        if writes is not None:
            out = cmd[cmd.index("--out") + 1]
            with open(out, "w", encoding="utf-8") as f:
                json.dump(writes, f)
        return SimpleNamespace(returncode=rc, stdout="", stderr="")
    return fake


def test_step_fresh_valid_read_refreshes_prev(step_env, monkeypatch):
    good = plan_json([prow("amazon", "Amazon", {"2026-01": 1.0})])
    monkeypatch.setattr(run_nightly, "run_step", _fake_run_step(good))
    path = run_nightly.fetch_revenue_plan_step(_args())
    assert json.loads(open(path, encoding="utf-8").read())["stale"] is False
    assert json.loads((step_env / "data" / "revenue_plan_prev.json").read_text())["rows"] == good["rows"]


def test_step_failure_uses_prev_flagged_stale(step_env, monkeypatch):
    good = plan_json([prow("amazon", "Amazon", {"2026-01": 1.0})])
    (step_env / "data" / "revenue_plan_prev.json").write_text(json.dumps(good))
    monkeypatch.setattr(run_nightly, "run_step", _fake_run_step({"valid": False, "error": "HTTP 403"}))
    final = json.loads(open(run_nightly.fetch_revenue_plan_step(_args()), encoding="utf-8").read())
    assert final["stale"] is True and final["valid"] is True and final["error"] == "HTTP 403"


def test_step_crashed_subprocess_without_prev_writes_invalid_marker(step_env, monkeypatch):
    monkeypatch.setattr(run_nightly, "run_step", _fake_run_step(None, rc=1))
    path = run_nightly.fetch_revenue_plan_step(_args())
    final = json.loads(open(path, encoding="utf-8").read())
    assert final["valid"] is False and final["error"]
    assert not (step_env / "data" / "revenue_plan_prev.json").exists()


def test_step_exception_in_runner_never_propagates(step_env, monkeypatch):
    def boom(*a, **k):
        raise OSError("spawn failed")
    monkeypatch.setattr(run_nightly, "run_step", boom)
    path = run_nightly.fetch_revenue_plan_step(_args())
    assert json.loads(open(path, encoding="utf-8").read())["valid"] is False


def test_step_without_sheet_id_returns_none(step_env, monkeypatch):
    monkeypatch.delenv("SPIKEBALL_FINANCE_SHEET_ID", raising=False)
    assert run_nightly.fetch_revenue_plan_step(argparse.Namespace(sheet=None)) is None


# --------------------------------------------------------------------------- fetch + protection

class _Resp:
    def __init__(self, status, data=None, text=""):
        self.status_code, self._d, self.text = status, data or {}, text

    def json(self):
        return self._d


def test_fetch_http_error_returns_invalid_and_prints_marker(monkeypatch, capsys):
    monkeypatch.setattr(revenue_plan.google_auth, "authed_request",
                        lambda *a, **k: _Resp(403, text="forbidden"))
    r = revenue_plan.fetch_revenue_plan("SID", CFG)
    assert r["valid"] is False and "403" in r["error"]
    assert "REVENUE_PLAN_FETCH_ERROR 403 forbidden" in capsys.readouterr().out


def test_fetch_success_parses_and_only_issues_a_get(monkeypatch):
    calls = []

    def fake(method, url, **k):
        calls.append((method, url))
        return _Resp(200, {"values": grid(["Amazon", "Plan", "1", "", ""])})
    monkeypatch.setattr(revenue_plan.google_auth, "authed_request", fake)
    r = revenue_plan.fetch_revenue_plan("SID", CFG)
    assert r["valid"] and r["stale"] is False and r["fetched_at_mt"]
    assert [c[0] for c in calls] == ["GET"]
    assert "Revenue%20Plan" in calls[0][1]


def test_fetch_auth_exception_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no token")
    monkeypatch.setattr(revenue_plan.google_auth, "authed_request", boom)
    assert revenue_plan.fetch_revenue_plan("SID", CFG)["valid"] is False


def test_revenue_plan_tab_is_protected_in_config_and_default():
    import publish_sheet
    assert "Revenue Plan" in publish_sheet.DEFAULT_PROTECTED_TABS
    assert "Revenue Plan" in publish_sheet.load_protected_tabs()
    assert "Demand Plan" in publish_sheet.load_protected_tabs()


def test_new_output_keys_publish_as_ordinary_tabs():
    import publish_sheet
    out = revenue_plan.build_outputs(
        plan_json([prow("amazon", "Amazon", {"2026-01": 1.0})]), full_year_rollup(), "2026-06-01", CFG)
    tables = publish_sheet.build_tables(out)
    assert "revenue_plan_month" in tables and "plan_vs_actual_month" in tables
    assert "Revenue Plan" not in tables
    assert any(t.startswith("revenue_plan_meta") for t in tables)


def test_forecast_fields_flow_through_both_publishers_without_code_change():
    import publish_bq
    import publish_sheet
    out = revenue_plan.build_outputs(
        plan_json([prow("amazon", "Amazon", {"2026-01": 1.0}),
                   prow("total", "Total", {"2026-01": 5.0}, series="forecast")]),
        full_year_rollup(), "2026-06-01", CFG)
    tables = publish_sheet.build_tables(out)
    pva_headers = publish_sheet.build_grid(tables["plan_vs_actual_month"])[0]
    assert {"forecast_gross", "variance_vs_forecast", "variance_vs_forecast_pct"} <= set(pva_headers)
    summary_headers = publish_sheet.build_grid(tables["revenue_plan_meta_summary"])[0]
    assert {"forecast_available", "forecast_grain"} <= set(summary_headers)
    assert [r["value"] for r in tables["revenue_plan_meta_series"]] == ["forecast", "plan"]
    fields, _, _ = publish_bq.build_schema_and_order(tables["plan_vs_actual_month"])
    by_name = {f["name"]: f["type"] for f in fields}
    assert by_name["forecast_gross"] == "FLOAT64" and by_name["variance_vs_forecast_pct"] == "FLOAT64"
    fields, _, _ = publish_bq.build_schema_and_order(tables["revenue_plan_meta_summary"])
    by_name = {f["name"]: f["type"] for f in fields}
    assert by_name["forecast_available"] == "BOOL" and by_name["forecast_grain"] == "STRING"
