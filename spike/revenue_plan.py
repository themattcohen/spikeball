#!/usr/bin/env python3
"""revenue_plan.py -- reads (never writes) the CFO-owned "Revenue Plan" Google Sheet tab
and turns it into the plan-vs-actual gross revenue series the dashboard draws.

Same shape as demand_plan.py, with one deliberate difference: a plan problem NEVER fails
the nightly. Every error path here yields a `valid: false` result (exit code 0), and
run_nightly.py falls back to the last known-good snapshot flagged stale. There is no
function in this module that builds a values:update, values:batchUpdate, or
values:batchClear request against the tab.

Tab layout (header row keyed by TEXT, never by column position):
    Channel | Series | 2026-01 | 2026-02 | ... | 2026-12
  - Optional free-text note rows sit above the header. The header row is the first row
    whose first cell is "Channel" (case-insensitive, trimmed).
  - Channel = a rollup label ("Amazon", "Spikeball.com", "Wholesale", "Other B2B") or a
    rollup key ("amazon", "dtc", "wholesale", "other_b2b"), matched case-insensitively
    against spike/config/rollups.json.
  - Series = "Plan" for the plan of record; other series (e.g. "Forecast") are parsed and
    stored lowercased but only "plan" feeds plan_vs_actual_month.
  - Amounts are gross revenue dollars. Blank = no plan for that month. "$", ",", spaces
    and parentheses (negative) are accepted.
  - Any number of YYYY-MM month columns; months outside 01-12 are not month columns.

Called by run_nightly.py as `python revenue_plan.py fetch --sheet ID --out PATH`;
extract.py then reads that JSON through --revenue-plan and calls build_outputs().
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import google_auth  # noqa: E402
from publish_sheet import now_mt_iso  # noqa: E402  (shared MT-timestamp helper)

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
TAB_NAME = "Revenue Plan"
FETCH_RANGE = f"'{TAB_NAME}'!A1:Z200"
MONTH_COL_RE = re.compile(r"^\d{4}-\d{2}$")
PLAN_SERIES = "plan"
TOTAL_KEY = "total"
UNASSIGNED_KEY = "unassigned"


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _norm(s):
    return "" if s is None else str(s).replace(" ", " ").strip()


def _is_month_header(h):
    if not MONTH_COL_RE.match(h):
        return False
    return 1 <= int(h[5:7]) <= 12


def _parse_amount(raw):
    """Returns (value|None, blank: bool). value None with blank False = unparsable."""
    if isinstance(raw, bool):
        return None, False
    if isinstance(raw, (int, float)):
        return (float(raw), False) if math.isfinite(raw) else (None, False)
    s = _norm(raw)
    if s == "":
        return None, True
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("-"):
        neg, s = (not neg), s[1:]
    if not re.fullmatch(r"\d+(\.\d*)?|\.\d+", s):
        return None, False
    val = float(s)
    return (-val if neg else val), False


def _channel_lookup(rollups_cfg):
    """{lowercased label or key: (key, label)} over the rollup groups."""
    out = {}
    for g in (rollups_cfg or {}).get("groups", []):
        out[str(g["key"]).lower()] = (g["key"], g["label"])
        out[str(g["label"]).lower()] = (g["key"], g["label"])
    return out


def _invalid(error, note="", header_row=0):
    return {"valid": False, "error": error, "note": note, "header_row": header_row,
            "month_columns": [], "rows": [], "dropped_rows": []}


def parse_revenue_plan_grid(values, rollups_cfg):
    """Pure parser, no network. `values` is the raw Sheets grid (ragged rows allowed)."""
    if not values:
        return _invalid("tab is empty (no header row)")

    header_idx = None
    for i, row in enumerate(values):
        if row and _norm(row[0]).lower() == "channel":
            header_idx = i
            break
    if header_idx is None:
        return _invalid("no header row found (expected 'Channel' in column A)")

    note_lines = []
    for row in values[:header_idx]:
        text = " ".join(_norm(c) for c in row if _norm(c))
        if text:
            note_lines.append(text)
    note = " | ".join(note_lines)
    header_row = header_idx + 1

    headers = [_norm(h) for h in values[header_idx]]
    series_i = next((i for i, h in enumerate(headers) if h.lower() == "series"), None)
    if series_i is None:
        return _invalid("missing required column: Series", note, header_row)
    month_cols, seen = [], set()
    for i, h in enumerate(headers):
        if _is_month_header(h) and h not in seen:
            seen.add(h)
            month_cols.append((h, i))
    if not month_cols:
        return _invalid("no YYYY-MM month column found", note, header_row)

    lookup = _channel_lookup(rollups_cfg)

    def cell(row, i):
        return row[i] if i < len(row) else ""

    rows_out, dropped, taken = [], [], set()
    for offset, row in enumerate(values[header_idx + 1:]):
        sheet_row = header_idx + 2 + offset
        if not any(_norm(c) for c in row):
            continue
        channel = _norm(cell(row, 0))
        if not channel:
            dropped.append({"row": sheet_row, "reason": "blank Channel"})
            continue
        hit = lookup.get(channel.lower())
        if hit is None:
            dropped.append({"row": sheet_row, "reason": f"unknown channel '{channel}'"})
            continue
        series = _norm(cell(row, series_i)).lower()
        if not series:
            dropped.append({"row": sheet_row, "reason": f"blank Series for channel '{channel}'"})
            continue
        if (hit[0], series) in taken:
            dropped.append({"row": sheet_row,
                            "reason": f"duplicate row for channel '{channel}' series '{series}'"})
            continue
        months, bad = {}, None
        for name, i in month_cols:
            val, blank = _parse_amount(cell(row, i))
            if blank:
                continue
            if val is None:
                bad = (name, _norm(cell(row, i)))
                break
            months[name] = val
        if bad:
            dropped.append({"row": sheet_row,
                            "reason": f"unparsable amount '{bad[1]}' in {bad[0]}"})
            continue
        taken.add((hit[0], series))
        rows_out.append({"key": hit[0], "label": hit[1], "series": series, "months": months})

    return {"valid": True, "error": None, "note": note, "header_row": header_row,
            "month_columns": [h for h, _ in month_cols], "rows": rows_out,
            "dropped_rows": dropped}


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def load_rollups_cfg():
    return json.loads((Path(__file__).parent / "config" / "rollups.json").read_text(encoding="utf-8"))


def fetch_revenue_plan(sheet_id, rollups_cfg=None):
    """One read-only GET of the Revenue Plan tab, parsed. Never raises and never exits
    nonzero: any failure returns {"valid": False, "error": ...}."""
    url = f"{SHEETS_BASE}/{sheet_id}/values/{urllib.parse.quote(FETCH_RANGE, safe='')}"
    try:
        resp = google_auth.authed_request("GET", url)
    except Exception as e:  # noqa: BLE001  a plan problem must never fail the nightly
        print(f"REVENUE_PLAN_FETCH_ERROR request-failed {str(e)[:200]}")
        return {"valid": False, "error": f"request failed: {str(e)[:200]}"}
    if resp.status_code != 200:
        print(f"REVENUE_PLAN_FETCH_ERROR {resp.status_code} {resp.text[:200]}")
        return {"valid": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        values = resp.json().get("values") or []
        cfg = rollups_cfg if rollups_cfg is not None else load_rollups_cfg()
        result = parse_revenue_plan_grid(values, cfg)
    except Exception as e:  # noqa: BLE001
        print(f"REVENUE_PLAN_FETCH_ERROR parse-failed {str(e)[:200]}")
        return {"valid": False, "error": f"parse failed: {str(e)[:200]}"}
    result["stale"] = False
    result["fetched_at_mt"] = now_mt_iso()
    return result


def resolve_snapshot(current, prev):
    """Picks what the nightly hands to extract.py. A valid fresh read wins. Otherwise the
    last known-good snapshot is returned flagged stale (still valid, so the plan line keeps
    drawing, labeled stale) with this run's error attached. With neither, the invalid
    current read is returned."""
    if current.get("valid"):
        out = dict(current)
        out["stale"] = False
        return out
    if prev and prev.get("valid") and prev.get("rows"):
        out = dict(prev)
        out["stale"] = True
        out["error"] = current.get("error") or "Revenue Plan read failed"
        return out
    out = dict(current)
    out.setdefault("valid", False)
    out.setdefault("error", "Revenue Plan unavailable")
    out.setdefault("stale", False)
    return out


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------

def _money(x):
    return None if x is None else round(float(x), 2)


def _group_order(rollups_cfg):
    return [(g["key"], g["label"]) for g in (rollups_cfg or {}).get("groups", [])]


def build_outputs(plan_json, rollup_by_month, asof_date, rollups_cfg):
    """Returns {"revenue_plan_meta", "revenue_plan_month", "plan_vs_actual_month"}."""
    plan_json = plan_json or {}
    valid = bool(plan_json.get("valid"))
    groups = _group_order(rollups_cfg)
    order = {k: i for i, (k, _) in enumerate(groups)}
    labels = dict(groups)
    rollup_by_month = rollup_by_month or []
    asof_ym = str(asof_date)[:7]
    asof_year = int(asof_ym[:4])

    plan_rows = plan_json.get("rows") or [] if valid else []
    month_cols = list(plan_json.get("month_columns") or []) if valid else []
    year = None
    if valid and month_cols:
        years = sorted({int(m[:4]) for m in month_cols})
        year = asof_year if asof_year in years else years[-1]

    # revenue_plan_month: one row per (series, key, ym) with an amount
    month_rows = []
    for r in plan_rows:
        for ym, amt in r["months"].items():
            month_rows.append({"ym": ym, "key": r["key"], "label": r["label"],
                               "series": r["series"], "plan_gross": _money(amt)})
    month_rows.sort(key=lambda x: (x["ym"], order.get(x["key"], len(order)), x["series"]))

    # actuals
    notes = []
    actual = {}  # (ym, key) -> gross
    fell_back = False
    for r in rollup_by_month:
        key = r.get("key")
        if key == TOTAL_KEY or not r.get("ym"):
            continue
        if r.get("gross_revenue") is not None:
            val = r["gross_revenue"]
        elif "gross_revenue" not in r and r.get("revenue") is not None:
            val, fell_back = r["revenue"], True
        else:
            continue
        actual[(r["ym"], key)] = actual.get((r["ym"], key), 0.0) + float(val)
    if fell_back:
        notes.append("gross_revenue absent from rollup_by_month; actuals use net revenue")
    window = {r["ym"] for r in rollup_by_month if r.get("ym")}
    actual_keys = {k for (_, k) in actual}

    plan = {}  # (ym, key) -> plan_gross, series "plan" only
    for r in plan_rows:
        if r["series"] != PLAN_SERIES:
            continue
        for ym, amt in r["months"].items():
            plan[(ym, r["key"])] = amt

    yms = ([f"{year}-{m:02d}" for m in range(1, 13)] if year is not None
           else sorted(window))
    keys = [(k, l) for k, l in groups]
    if UNASSIGNED_KEY in actual_keys:
        keys.append((UNASSIGNED_KEY, (rollups_cfg or {}).get("unassigned", {}).get("label", "Unassigned")))
    keys.append((TOTAL_KEY, "Total"))

    def basis_for(ym):
        return "actual" if ym < asof_ym else ("open" if ym == asof_ym else "future")

    outside = []
    pva = []
    for ym in yms:
        basis0 = basis_for(ym)
        in_window = ym in window
        if basis0 != "future" and not in_window:
            outside.append(ym)
        plan_ym = [v for (m, _), v in plan.items() if m == ym]
        for key, label in keys:
            if key == TOTAL_KEY:
                p = sum(plan_ym) if plan_ym else None
                a = (sum(v for (m, _), v in actual.items() if m == ym)
                     if in_window and basis0 != "future" else None)
            else:
                p = plan.get((ym, key))
                a = (actual.get((ym, key), 0.0)
                     if in_window and basis0 != "future" else None)
            p, a = _money(p), _money(a)
            basis = basis0
            if basis0 != "future" and p is None:
                basis = "no_plan"
            variance = pct = None
            if p is not None and a is not None and basis0 != "future":
                variance = _money(a - p)
                pct = round(variance / p * 100, 1) if p else None
            pva.append({"ym": ym, "key": key, "label": label, "plan_gross": p,
                        "actual_gross": a, "variance": variance, "variance_pct": pct,
                        "basis": basis})
    if outside:
        notes.append("no actuals in the rollup window for: " + ", ".join(sorted(set(outside))))

    plan_note = _norm(plan_json.get("note"))
    note = " | ".join([n for n in [plan_note] + notes if n])
    series = sorted({r["series"] for r in plan_rows})
    meta = {
        "valid": valid,
        "stale": bool(plan_json.get("stale")),
        "fetched_at_mt": plan_json.get("fetched_at_mt"),
        "source": TAB_NAME,
        "year": year,
        "series": series,
        "month_columns": month_cols,
        "row_count": len(plan_rows),
        "dropped_rows": list(plan_json.get("dropped_rows") or []) if valid else [],
        "note": note,
        "error": plan_json.get("error") if plan_json else "Revenue Plan not read this run",
    }
    if not valid and meta["error"] is None:
        meta["error"] = "Revenue Plan not valid"
    return {"revenue_plan_meta": meta, "revenue_plan_month": month_rows,
            "plan_vs_actual_month": pva}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Read (never write) the CFO's Revenue Plan Sheet tab.")
    sub = ap.add_subparsers(dest="cmd")
    fp = sub.add_parser("fetch", help="Fetch, parse, and write the plan JSON. Always exits 0.")
    fp.add_argument("--sheet", required=True, help="Spreadsheet id.")
    fp.add_argument("--out", required=True, help="Where to write the parsed result JSON.")
    args = ap.parse_args()
    if args.cmd != "fetch":
        ap.print_help()
        return 1
    result = fetch_revenue_plan(args.sheet)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"REVENUE_PLAN_RESULT valid={result.get('valid')} rows={len(result.get('rows') or [])} "
          f"dropped={len(result.get('dropped_rows') or [])} error={result.get('error')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
