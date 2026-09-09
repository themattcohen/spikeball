#!/usr/bin/env python3
"""demand_plan.py -- reads (never writes) the CFO-owned "Demand Plan" Google Sheet tab.

Design: research/09-cfo-input-mechanism-design.md. This is the narrow, units-only slice
of the CFO input mechanism (demand vs actuals), NOT the full deferred `cfo_inputs.py`
mechanism PRD-v2.md Section 3 describes for forecast dollars / 2027 scenario / cash
engine -- those stay deferred (V2R1/V2R3). There is no function anywhere in this module
that constructs a values:batchUpdate or values:batchClear request against the Demand
Plan tab. Ever.

Called by run_nightly.py as a subprocess (`python demand_plan.py fetch --sheet ID --out
PATH [--prev PATH]`), right after fetch_prev_state() and before run_extract(), matching
the --prev-state pattern already in use (run_nightly.py:166-176). extract.py then reads
the written JSON via a new --demand-plan PATH argument -- it never imports this module or
calls the Sheets API itself.

Tab layout (research/09 Section 2), header row keyed by TEXT, never by column position
(PRD-v2.md Section 11 item 8):
    SKU | Customer | Location | Unit Price | 2026-01 | 2026-02 | ... | 2026-12
Any row(s) above the header row (the row whose first cell is exactly "SKU", case
insensitive) are treated as a freeform meta note (e.g. "Last updated 8/27 by Matt") --
optional, never required, never parsed into structured fields.

Failure classes (research/09 Section 6), in increasing severity:
  - a malformed DATA ROW (blank SKU, blank Customer/Location, a non-numeric month cell,
    or a row with no month value entered at all) is dropped and reported; it does not
    invalidate the tab.
  - a SCHEMA problem (no header row found, a required column renamed/missing, no
    YYYY-MM column present, or the tab not yet seeded) invalidates the whole read for
    this run (`valid: false`) but is NOT a fatal error -- the caller (run_nightly.py)
    falls back to the last known-good snapshot (`--prev`) and labels it stale, never
    blanks the section.
  - a genuine Google API error (auth failure, timeout, persistent 5xx/429, or any
    unexpected non-200) raises DemandPlanFetchError, which the CLI reports as
    DEMAND_PLAN_FETCH_ERROR and exits 1 -- run_nightly.py treats that as NIGHTLY_FAIL,
    same as an extract.py crash, never as a silent stale-data path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import google_auth  # noqa: E402
from publish_sheet import now_mt_iso  # noqa: E402  (shared MT-timestamp helper)

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
TAB_NAME = "Demand Plan"
FETCH_RANGE = f"'{TAB_NAME}'!A1:P400"
REQUIRED_HEADERS = ("sku", "customer", "location")
MONTH_COL_RE = re.compile(r"^\d{4}-\d{2}$")


class DemandPlanFetchError(RuntimeError):
    """A genuine Google API failure reading the tab -- never a validation failure."""


def _to_float_or_none(raw):
    s = "" if raw is None else str(raw).strip()
    if s == "":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _norm(s):
    return "" if s is None else str(s).strip()


def _find_header_row(values):
    for i, row in enumerate(values):
        if row and _norm(row[0]).lower() == "sku":
            return i
    return None


def _empty_result(reason):
    return {
        "valid": False, "reason": reason, "tab_name": TAB_NAME, "meta_note": None,
        "month_columns": [], "rows": [], "dropped_rows": [], "row_count": 0,
        "dropped_row_count": 0, "sku_count": 0,
    }


def parse_demand_plan_grid(values):
    """Pure parser, no network -- takes the raw `values` grid exactly as the Sheets API
    returns it (list of rows, each a list of cell strings, ragged rows allowed) and
    returns the same shape fetch_demand_plan() writes, minus the read_at_mt/checked_at_mt
    stamps. Kept separate from the HTTP call so it is unit-testable on a hand-made grid."""
    if not values:
        return _empty_result("tab is empty (no header row) -- not yet seeded")

    header_idx = _find_header_row(values)
    if header_idx is None:
        return _empty_result("no header row found (expected a cell containing 'SKU' in column A)")

    meta_lines = []
    for row in values[:header_idx]:
        text = " ".join(_norm(c) for c in row if _norm(c))
        if text:
            meta_lines.append(text)
    meta_note = " | ".join(meta_lines) if meta_lines else None

    headers = [_norm(h) for h in values[header_idx]]
    col_by_header = {}
    for i, h in enumerate(headers):
        if h:
            col_by_header.setdefault(h.lower(), i)

    missing = [h for h in REQUIRED_HEADERS if h not in col_by_header]
    if missing:
        result = _empty_result(f"missing required column(s): {', '.join(missing)}")
        result["meta_note"] = meta_note
        return result

    month_cols = [(h, i) for i, h in enumerate(headers) if MONTH_COL_RE.match(h)]
    if not month_cols:
        result = _empty_result("no YYYY-MM month column found")
        result["meta_note"] = meta_note
        return result

    sku_i = col_by_header["sku"]
    cust_i = col_by_header["customer"]
    loc_i = col_by_header["location"]
    price_i = col_by_header.get("unit price")

    def cell(row, i):
        return row[i] if i is not None and i < len(row) else ""

    rows_out, dropped = [], []
    for offset, row in enumerate(values[header_idx + 1:]):
        abs_row = header_idx + 2 + offset  # 1-based sheet row number, for diagnostics
        sku = _norm(cell(row, sku_i))
        if not sku:
            if any(_norm(c) for c in row):
                dropped.append({"row": abs_row, "reason": "blank SKU"})
            continue
        customer = _norm(cell(row, cust_i))
        location = _norm(cell(row, loc_i))
        if not customer or not location:
            dropped.append({"row": abs_row, "sku": sku, "reason": "blank Customer or Location"})
            continue
        unit_price = _to_float_or_none(cell(row, price_i)) if price_i is not None else None

        months, any_entered, bad_cell = {}, False, None
        for name, i in month_cols:
            raw = _norm(cell(row, i))
            if raw == "":
                months[name] = 0.0
                continue
            val = _to_float_or_none(raw)
            if val is None:
                bad_cell = (name, raw)
                break
            months[name] = val
            any_entered = True
        if bad_cell:
            dropped.append({"row": abs_row, "sku": sku,
                             "reason": f"non-numeric value '{bad_cell[1]}' in {bad_cell[0]}"})
            continue
        if not any_entered:
            dropped.append({"row": abs_row, "sku": sku, "reason": "no month values entered"})
            continue

        rows_out.append({
            "sku": sku, "customer": customer, "location": location,
            "unit_price": unit_price, "months": months, "row": abs_row,
        })

    return {
        "valid": True, "reason": None, "tab_name": TAB_NAME, "meta_note": meta_note,
        "month_columns": [h for h, _ in month_cols],
        "rows": rows_out, "dropped_rows": dropped,
        "row_count": len(rows_out), "dropped_row_count": len(dropped),
        "sku_count": len({r["sku"] for r in rows_out}),
    }


def fetch_demand_plan(sheet_id):
    """One GET against the Demand Plan tab, parsed and validated. Never writes. Raises
    DemandPlanFetchError on a genuine API failure; a missing/malformed tab is returned
    as a normal (non-exception) `valid: false` result -- see module docstring."""
    url = f"{SHEETS_BASE}/{sheet_id}/values/{urllib.parse.quote(FETCH_RANGE, safe='')}"
    try:
        resp = google_auth.authed_request("GET", url)
    except google_auth.GoogleAuthError as e:
        raise DemandPlanFetchError(str(e)) from e

    now = now_mt_iso()
    if resp.status_code == 400 and "Unable to parse range" in resp.text:
        result = _empty_result("Demand Plan tab not found in the Sheet (not yet seeded)")
    elif resp.status_code != 200:
        raise DemandPlanFetchError(f"GET {FETCH_RANGE} HTTP {resp.status_code}: {resp.text[:500]}")
    else:
        try:
            values = resp.json().get("values") or []
        except ValueError as e:
            raise DemandPlanFetchError(f"unparseable JSON response: {e}") from e
        result = parse_demand_plan_grid(values)

    result["stale"] = False
    result["read_at_mt"] = now
    result["checked_at_mt"] = now
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_prev(prev_path):
    p = Path(prev_path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if data.get("rows") else None


def main():
    ap = argparse.ArgumentParser(description="Read (never write) the CFO's Demand Plan Sheet tab.")
    sub = ap.add_subparsers(dest="cmd")
    fp = sub.add_parser("fetch", help="Fetch, validate, and write the parsed plan JSON.")
    fp.add_argument("--sheet", required=True, help="Spreadsheet id.")
    fp.add_argument("--out", required=True, help="Where to write the parsed result JSON.")
    fp.add_argument("--prev", default=None,
                     help="Path to the last known-good parsed snapshot; used as a stale "
                          "fallback when this run's read is invalid.")
    args = ap.parse_args()

    if args.cmd != "fetch":
        ap.print_help()
        return 1

    try:
        result = fetch_demand_plan(args.sheet)
    except DemandPlanFetchError as e:
        print(f"DEMAND_PLAN_FETCH_ERROR {e}")
        return 1

    if not result["valid"] and args.prev:
        prev = _load_prev(args.prev)
        if prev is not None:
            this_run_reason = result["reason"]
            stale = dict(prev)
            # `valid` stays False -- THIS run's read genuinely failed; that is exactly the
            # signal check_p_demand_plan_valid (checks_v2.py) turns into demand_plan_ok=false
            # and the section's "inputs_stale" label (research/09 Section 3). `rows` (kept
            # from `prev`) is what build_demand_vs_actual actually consumes regardless of
            # this flag, so the section still renders -- never blank -- just labeled stale.
            stale["valid"] = False
            stale["stale"] = True
            stale["reason"] = this_run_reason
            stale["checked_at_mt"] = result["checked_at_mt"]
            # read_at_mt is left as the PRIOR snapshot's own read_at_mt -- it names when
            # the data being shown was actually captured, not when we last tried and failed.
            result = stale
            print(f"DEMAND_PLAN_STALE falling back to the snapshot read at {prev.get('read_at_mt')} "
                  f"(this run's read was invalid: {this_run_reason})")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    status = "valid" if result["valid"] else "invalid"
    stale_note = " stale=true" if result.get("stale") else ""
    print(f"DEMAND_PLAN_RESULT status={status}{stale_note} rows={result.get('row_count', 0)} "
          f"dropped={result.get('dropped_row_count', 0)} sku_count={result.get('sku_count', 0)} "
          f"reason={result.get('reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
