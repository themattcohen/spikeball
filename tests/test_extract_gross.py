"""Hermetic tests for the gross-revenue and revenue-plan additions to extract.py / checks_v2.py.
No network: NetSuite is never touched."""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spike"))

import extract  # noqa: E402
import checks_v2  # noqa: E402

ROLLUPS = {
    "groups": [
        {"key": "amazon", "label": "Amazon", "channel_ids": [1]},
        {"key": "wholesale", "label": "Wholesale", "channel_ids": [2, 3]},
    ],
    "unassigned": {"key": "unassigned", "label": "Unassigned"},
}


def _w(rev, gross, cogs=0.0):
    return {"revenue": rev, "cogs": cogs, "gp": rev - cogs, "margin_pct": None, "gross_revenue": gross}


def _period_row(cid, mtd, ytd, pmtd, pytd):
    return {"channel_id": cid, "channel": str(cid), "mtd": mtd, "ytd": ytd,
            "mtd_prior_year": pmtd, "ytd_prior_year": pytd}


def _plan_module(monkeypatch):
    mod = sys.modules.get("revenue_plan")
    if mod is None:
        mod = types.ModuleType("revenue_plan")
        monkeypatch.setitem(sys.modules, "revenue_plan", mod)
    return mod


def test_gross_matrices_flip_sign_and_group():
    rows = [{"ym": "2026-08", "chan": "1", "amt": "-100.5"}, {"ym": "2026-08", "chan": "1", "amt": "-0.5"},
            {"ym": "2026-08", "chan": "", "amt": "-7"}]
    m = extract._gross_month_matrix(rows)
    assert m[("2026-08", 1)] == 101.0 and m[("2026-08", None)] == 7.0
    assert extract._gross_channel_map([{"chan": "2", "amt": "-5"}]) == {2: 5.0}


def test_sum_gross_none_propagates():
    assert extract._sum_gross([1.5, 2.5]) == 4.0
    assert extract._sum_gross([1.5, None]) is None


def test_yoy_gross_pct():
    assert extract._yoy_gross_pct({"gross_revenue": 150.0}, {"gross_revenue": 100.0}) == 50.0
    assert extract._yoy_gross_pct({"gross_revenue": 150.0}, {"gross_revenue": 0.0}) is None
    assert extract._yoy_gross_pct({"gross_revenue": None}, {"gross_revenue": 100.0}) is None


def test_rollup_by_period_carries_gross():
    z = _w(0.0, 0.0)
    rows = [
        _period_row(1, _w(90, 100), _w(900, 1000), _w(45, 50), _w(450, 500)),
        _period_row(2, _w(10, 20), _w(100, 200), _w(0, 0), _w(0, 0)),
        _period_row(3, _w(5, 5), _w(50, 50), _w(0, 0), _w(0, 0)),
        _period_row(None, z, z, z, z),
        _period_row("TOTAL", _w(105, 125), _w(1050, 1250), _w(45, 50), _w(450, 500)),
    ]
    out, _ = extract.build_rollup_by_period(rows, ROLLUPS)
    by = {r["key"]: r for r in out}
    assert by["amazon"]["mtd"]["gross_revenue"] == 100.0
    assert by["wholesale"]["mtd"]["gross_revenue"] == 25.0
    assert by["amazon"]["yoy_mtd_gross_pct"] == 100.0 and by["amazon"]["yoy_ytd_gross_pct"] == 100.0
    assert by["wholesale"]["yoy_mtd_gross_pct"] is None
    assert by["total"]["mtd"]["gross_revenue"] == 125.0
    assert by["unassigned"]["mtd"]["gross_revenue"] == 0.0


def test_rollup_by_month_sums_gross_and_py():
    def mrow(cid, gross, gross_py):
        return {"ym": "2026-08", "channel_id": cid, "revenue": 1.0, "cogs": 0.0,
                "revenue_py": 1.0, "gross_revenue": gross, "gross_revenue_py": gross_py}
    rows = [mrow(1, 10.0, 4.0), mrow(2, 5.0, 1.0), mrow(3, 2.5, 0.5), mrow(None, 0.0, 0.0)]
    out, _ = extract.build_rollup_by_month(rows, ROLLUPS)
    by = {r["key"]: r for r in out}
    assert by["wholesale"]["gross_revenue"] == 7.5 and by["wholesale"]["gross_revenue_py"] == 1.5
    assert by["amazon"]["gross_revenue"] == 10.0


def test_rollup_by_month_gross_null_when_unavailable():
    rows = [{"ym": "2026-08", "channel_id": 1, "revenue": 1.0, "cogs": 0.0, "revenue_py": 0.0,
             "gross_revenue": None, "gross_revenue_py": None}]
    out, _ = extract.build_rollup_by_month(rows, ROLLUPS)
    assert out[0]["gross_revenue"] is None and out[0]["gross_revenue_py"] is None


def test_derive_chart_meta_valid_plan():
    trailing = [f"2025-{m:02d}" for m in range(9, 13)] + [f"2026-{m:02d}" for m in range(1, 10)]
    year, chart, rng = extract.derive_chart_meta(trailing, {"valid": True, "year": 2026})
    assert year == 2026
    assert chart == sorted(set(trailing) | {f"2026-{m:02d}" for m in range(1, 13)})
    assert rng == {"start": "2026-01", "end": "2026-12"}


def test_derive_chart_meta_invalid_plan_uses_trailing():
    trailing = ["2025-09", "2025-10", "2026-09"]
    year, chart, rng = extract.derive_chart_meta(trailing, {"valid": False, "year": None})
    assert year is None and chart == trailing
    assert rng == {"start": "2025-09", "end": "2026-09"}
    assert extract.derive_chart_meta(trailing, None)[0] is None


def test_revenue_plan_call_is_guarded(monkeypatch, capsys):
    mod = _plan_module(monkeypatch)

    def boom(*a, **k):
        raise ValueError("bad tab")
    monkeypatch.setattr(mod, "build_outputs", boom, raising=False)
    out = extract.build_revenue_plan_outputs({"rows": []}, [], "2026-09-28", ROLLUPS)
    assert out["revenue_plan_meta"]["valid"] is False
    assert out["revenue_plan_meta"]["error"] == "bad tab"
    assert out["revenue_plan_month"] == [] and out["plan_vs_actual_month"] == []
    assert "REVENUE_PLAN_BUILD_ERROR bad tab" in capsys.readouterr().out


def test_revenue_plan_passthrough(monkeypatch):
    mod = _plan_module(monkeypatch)
    want = {"revenue_plan_meta": {"valid": True, "year": 2026}, "revenue_plan_month": [{"ym": "2026-01"}],
            "plan_vs_actual_month": [{"ym": "2026-01"}]}
    seen = {}

    def fake(plan, rbm, asof, cfg):
        seen.update(plan=plan, asof=asof)
        return want
    monkeypatch.setattr(mod, "build_outputs", fake, raising=False)
    assert extract.build_revenue_plan_outputs(None, None, "2026-09-28", ROLLUPS) == want
    assert seen == {"plan": None, "asof": "2026-09-28"}


def _pnl(cid, ym, g):
    return {"channel_id": cid, "ym": ym, "gross_revenue": g}


def test_check_q_ties_and_flags_mismatch():
    pnl = [_pnl(1, "2026-08", 10.0), _pnl(2, "2026-08", 5.0), _pnl(None, "2026-08", 0.0)]
    gn = [_pnl("1", "2026-08", 10.0), _pnl("2", "2026-08", 5.0)]
    assert checks_v2.check_q_gross_tie({"pnl_by_channel_month": pnl, "pnl_channel_gross_net": gn})["pass"]
    gn[1]["gross_revenue"] = 5.5
    res = checks_v2.check_q_gross_tie({"pnl_by_channel_month": pnl, "pnl_channel_gross_net": gn})
    assert not res["pass"] and "2/2026-08" in res["detail"]


def test_check_q_null_gross_fails():
    res = checks_v2.check_q_gross_tie({"pnl_by_channel_month": [_pnl(1, "2026-08", None)],
                                       "pnl_channel_gross_net": [_pnl("1", "2026-08", 1.0)]})
    assert not res["pass"]


def test_check_r_and_informational_only():
    out = {"revenue_plan_meta": {"valid": True, "year": 2026, "row_count": 3},
           "plan_vs_actual_month": [{"ym": "2026-01"}]}
    assert checks_v2.check_r_revenue_plan_ok(out)["pass"]
    assert not checks_v2.check_r_revenue_plan_ok({"revenue_plan_meta": {"valid": False}})["pass"]
    res = checks_v2.run_checks_v2({})
    assert res["revenue_plan_ok"] is False
    assert res["v2_pass"] is True  # q and r never fold into v2_pass
    assert "gross_tie_ok" in res
