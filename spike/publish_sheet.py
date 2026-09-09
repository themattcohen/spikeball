#!/usr/bin/env python3
"""Publish spike/data/latest.json (or any file matching the CONTRACT.md shape) to the
Spikeball-owned Google Sheet, one tab per top-level key, full overwrite, per PRD FD2 /
CONTRACT.md "Publishers". Handles both the v1 and v2 CONTRACT shapes -- publishes
whatever top-level keys are present in --data, no version branching.

Usage:
    doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- \\
        python spike/publish_sheet.py --data spike/data/latest.json --create "Spikeball Finance Data"
    doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- \\
        python spike/publish_sheet.py --data spike/data/latest.json --sheet <SHEET_ID>
    python spike/publish_sheet.py --data spike/data/latest.json --create --dry-run   # no token needed

Refuses to write when meta.checks.all_pass is false unless --force (CONTRACT.md
"Publishers"). When meta.checks is absent (v1 payloads), treats the run as passing and
logs that it did.

Google APIs via plain requests REST (Sheets v4) through spike/google_auth.py. Never
prints a token. Row cap 50,000 per tab (truncates and logs).
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import google_auth  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "routine"))
import doppler_env  # noqa: E402  (spike/routine/doppler_env.py)

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
ROW_CAP = 50000

DEFAULT_PROTECTED_TABS = ["Demand Plan"]

# Top-level output keys that carry data for a publisher-internal mechanism, not a
# Sheet tab / BQ table of their own. bs_snapshot_append is the durable monthly V-D
# balance-sheet-snapshot append rows (extract.py) -- publish_bq.py reads it directly
# off the parsed JSON and appends it to the `bs_snapshot` BigQuery table; it would
# otherwise become a redundant WRITE_TRUNCATE table / Sheet tab that just duplicates
# the current month already present in bs_by_account_month.
SKIP_TOP_LEVEL_KEYS = {"bs_snapshot_append"}

# (top_level_key, sub_key) -> shorter tab-name segment, called out explicitly in the
# FD2 spec (sku_sales_top5, inventory's four tabs handled separately below).
ALIAS = {
    ("sku_sales", "top5_concentration"): "top5",
}


def now_mt_iso():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Denver")).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# JSON -> tables
# ---------------------------------------------------------------------------

def flatten_row(d, prefix=""):
    """Flattens one dict into a single flat dict of scalars: nested dicts expand with
    an underscore-joined prefix (mtd -> mtd_revenue, mtd_cogs, ...); lists of scalars
    join with '; '; lists of dicts fall back to a compact JSON string (rare -- only
    hit for deeply nested v2 fields like meta.rollups.groups)."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten_row(v, key + "_"))
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                out[key] = json.dumps(v, separators=(",", ":"))
            else:
                out[key] = "; ".join("" if x is None else str(x) for x in v)
        else:
            out[key] = v
    return out


def build_generic_dict(topkey, d):
    """Splits a top-level dict value into tables: every list-of-dicts subkey becomes
    its own tab `{topkey}_{name}`; every dict-of-dicts subkey (e.g. dtc_by_region's
    rollup: {US:{...}, Other:{...}}) becomes a table with an added 'key' column;
    every dict-of-scalars subkey (e.g. life_to_date's income_by_year) becomes a
    two-column key/value table; remaining scalars collect into one summary row,
    named `{topkey}_summary` if any other tab was produced from this key, else
    `{topkey}` itself."""
    tables = {}
    summary = {}
    for subkey, subval in d.items():
        name = ALIAS.get((topkey, subkey), subkey)
        tab = f"{topkey}_{name}"
        if isinstance(subval, list):
            if subval and isinstance(subval[0], dict):
                tables[tab] = [flatten_row(r) for r in subval]
            elif subval:
                tables[tab] = [{"value": x} for x in subval]
            else:
                summary[subkey] = ""
        elif isinstance(subval, dict):
            if not subval:
                summary[subkey] = ""
            elif all(isinstance(v, dict) for v in subval.values()):
                tables[tab] = [dict(key=k, **flatten_row(v)) for k, v in subval.items()]
            elif all(not isinstance(v, (dict, list)) for v in subval.values()):
                tables[tab] = [{"key": k, "value": v} for k, v in subval.items()]
            else:
                summary.update(flatten_row(subval, prefix=f"{subkey}_"))
        else:
            summary[subkey] = subval
    if summary:
        tables[f"{topkey}_summary" if tables else topkey] = [summary]
    if not tables:
        tables[topkey] = [{}]
    return tables


def build_inventory_tables(inv):
    """Exact four tabs per the FD2 spec: inventory_onhand, inventory_oos_sample,
    inventory_days_on_hand (v2 only), inventory_summary."""
    tables = {}
    onhand = inv.get("onhand_by_item_location") or []
    tables["inventory_onhand"] = [flatten_row(r) for r in onhand] if onhand else [{}]
    oos = inv.get("oos") or {}
    sample = oos.get("sample") or []
    tables["inventory_oos_sample"] = [{"sku": s} for s in sample] if sample else [{}]
    doh = inv.get("days_on_hand")  # v2 only
    if doh is not None:
        tables["inventory_days_on_hand"] = [flatten_row(r) for r in doh] if doh else [{}]
    kit = inv.get("kit_skus_without_onhand") or []
    tables["inventory_summary"] = [{
        "total_value_locations": inv.get("total_value_locations"),
        "total_value_item_header": inv.get("total_value_item_header"),
        "tie_out_diff": inv.get("tie_out_diff"),
        "oos_active_invtpart": oos.get("active_invtpart"),
        "oos_everywhere": oos.get("oos_everywhere"),
        "kit_skus_without_onhand_count": len(kit),
        "kit_skus_without_onhand": "; ".join(kit),
    }]
    return tables


def build_meta_rows(meta):
    """The meta tab: fully recursive key/value flatten, including every
    meta.checks.* result and every meta.sections.* status (CONTRACT.md)."""
    flat = flatten_row(meta or {})
    rows = [{"key": k, "value": v} for k, v in flat.items()]
    # The Looker Studio report link lives in the secrets store (SPIKEBALL_LOOKER_REPORT_URL) so the meta tab
    # keeps pointing at it after every nightly overwrite (OPERATIONS.md: "link kept in the Sheet's meta tab").
    looker = os.environ.get("SPIKEBALL_LOOKER_REPORT_URL")
    if looker:
        rows.append({"key": "looker_report_url", "value": looker})
    return rows


def get_all_pass(data):
    """Returns (all_pass: bool, detail: str). v1 payloads (no meta.checks) pass by
    convention, logged as such."""
    meta = data.get("meta") or {}
    checks = meta.get("checks")
    if checks is None:
        return True, "no meta.checks present (v1 payload) -- treated as pass"
    if checks.get("all_pass"):
        return True, "meta.checks.all_pass is true"
    failing = {k: v.get("detail") for k, v in checks.items() if k != "all_pass" and isinstance(v, dict) and v.get("pass") is False}
    return False, f"meta.checks.all_pass is false; failing checks: {failing or '(unspecified)'}"


def build_run_log_row(data, trigger="nightly", request_row=""):
    """PRD-month-refresh.md Section 4/5 M5: `trigger` and `request_row` are the two
    trailing columns the gate (`run_nightly.py --gate`) adds -- `trigger` is `nightly`
    (default, and always the value when `--gate` is absent) or `request` when a queued
    `refresh_requests` row fired the run; `request_row` is a comma-joined list of the
    honored row numbers, blank otherwise."""
    meta = data.get("meta") or {}
    all_pass, _detail = get_all_pass(data)
    checks = meta.get("checks")
    return {
        "pulled_at_mt": meta.get("pulled_at_mt"),
        "asof_date": meta.get("asof_date"),
        "all_pass": all_pass,
        "sections_json": json.dumps(meta.get("sections") or {}, separators=(",", ":")),
        "checks_json": json.dumps(checks, separators=(",", ":")) if checks is not None else "",
        "published_at_mt": now_mt_iso(),
        "trigger": trigger,
        "request_row": request_row,
    }


def build_tables(data):
    """Top-level dispatcher: meta and inventory get bespoke handling per the FD2 spec;
    everything else (present in v1, v2, or both) goes through the generic splitter, so
    new v2 keys (rollup_by_period, rollup_by_month, orders_by_channel,
    picklist_snapshot, amazon_orders, ...) publish with no code change."""
    tables = {}
    for topkey, val in data.items():
        if topkey in SKIP_TOP_LEVEL_KEYS:
            continue
        if topkey == "meta":
            tables["meta"] = build_meta_rows(val)
        elif topkey == "inventory":
            tables.update(build_inventory_tables(val or {}))
        elif isinstance(val, list):
            tables[topkey] = [flatten_row(r) for r in val] if val else [{}]
        elif isinstance(val, dict):
            tables.update(build_generic_dict(topkey, val))
        else:
            tables[topkey] = [{"value": val}]
    return tables


# ---------------------------------------------------------------------------
# Sheets I/O
# ---------------------------------------------------------------------------

def cell_value(v):
    if v is None:
        return ""
    if isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def build_grid(rows):
    if not rows:
        return [[]]
    headers = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                headers.append(k)
    grid = [headers]
    for r in rows:
        grid.append([cell_value(r.get(k)) for k in headers])
    return grid


def get_existing_tabs(sheet_id):
    resp = google_auth.authed_request("GET", f"{SHEETS_BASE}/{sheet_id}", params={"fields": "sheets.properties"})
    if resp.status_code != 200:
        raise RuntimeError(f"get spreadsheet HTTP {resp.status_code}: {resp.text[:500]}")
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in resp.json().get("sheets", [])}


def sync_tab_structure(sheet_id, needed_titles, existing):
    """ONE combined spreadsheets.batchUpdate that replaces what used to be three
    separate calls (add missing tabs, delete the default 'Sheet1', reorder/format):
    creates any missing tabs (pre-assigning each a sheetId so later requests in the
    SAME batch can format them without a second round trip), deletes the default
    'Sheet1' tab if it's still present and empty (only ever that exact name, only
    after confirming A1 is empty), puts 'meta' at index 0, and applies header-bold +
    frozen-row-1 formatting to every tab. Returns the updated title->sheetId map.
    This is the only structural write request per run."""
    missing = [t for t in needed_titles if t not in existing]
    next_id = (max(existing.values()) + 1) if existing else 1
    new_ids = {}
    for t in missing:
        new_ids[t] = next_id
        next_id += 1

    delete_sheet1 = False
    sheet1_id = existing.get("Sheet1")
    if sheet1_id is not None:
        rng = urllib.parse.quote("'Sheet1'!A1:A1", safe="")
        r = google_auth.authed_request("GET", f"{SHEETS_BASE}/{sheet_id}/values/{rng}")
        if r.status_code == 200 and r.json().get("values"):
            print("[publish_sheet] 'Sheet1' has data in A1 -- leaving it alone (unexpected)")
        else:
            delete_sheet1 = True

    all_ids = dict(existing)
    all_ids.update(new_ids)
    if delete_sheet1:
        all_ids.pop("Sheet1", None)

    reqs = [{"addSheet": {"properties": {"sheetId": new_ids[t], "title": t}}} for t in missing]
    if delete_sheet1:
        reqs.append({"deleteSheet": {"sheetId": sheet1_id}})
    meta_id = all_ids.get("meta")
    if meta_id is not None:
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": meta_id, "index": 0}, "fields": "index"}})
    for sid in all_ids.values():
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }})
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }})

    if reqs:
        resp = google_auth.authed_request("POST", f"{SHEETS_BASE}/{sheet_id}:batchUpdate", json={"requests": reqs})
        if resp.status_code != 200:
            raise RuntimeError(f"sync tab structure HTTP {resp.status_code}: {resp.text[:500]}")

    if missing:
        print(f"[publish_sheet] created {len(missing)} new tab(s): {missing}")
    if delete_sheet1:
        print("[publish_sheet] deleted default 'Sheet1' tab")
    if meta_id is not None:
        print("[publish_sheet] 'meta' tab set to index 0")
    return all_ids


def chunk_data_by_size(entries, max_bytes=1_800_000, max_chunks=3):
    """Splits {range,values} entries for values:batchUpdate into as few chunks as
    possible, each safely under the Sheets API's 2 MB per-request limit, capped at
    max_chunks. At current data volume this always returns one chunk -- chunking is a
    safety net for future growth, not the normal path."""
    total_size = len(json.dumps(entries, separators=(",", ":")))
    if total_size <= max_bytes or len(entries) <= 1:
        return [entries]
    n = min(max_chunks, len(entries))
    size = -(-len(entries) // n)  # ceil division
    return [entries[i:i + size] for i in range(0, len(entries), size) if entries[i:i + size]]


def write_all_tabs(sheet_id, tables, dry_run):
    """Writes every data tab (everything in `tables` -- NOT run_log, which is
    append-only and handled separately by write_run_log) via ONE values:batchClear +
    ONE values:batchUpdate (chunked into at most 3 requests each only if the combined
    payload would exceed a safe per-request size). Total Sheets API write calls for
    all data tabs combined: 2 in the normal case (was 2 PER TAB before -- 54 calls for
    27 tabs -- which is what triggered the 60-writes/minute quota)."""
    grids = {}
    row_counts = {}
    for tab, rows in tables.items():
        truncated = len(rows) > ROW_CAP
        rows = rows[:ROW_CAP]
        grid = build_grid(rows)
        grids[tab] = grid
        row_counts[tab] = (len(rows), truncated)
        if truncated:
            print(f"PUBLISH_SHEET_TRUNCATED tab={tab} kept={len(rows)} of original")

    if dry_run:
        for tab, grid in grids.items():
            n, trunc = row_counts[tab]
            print(f"[dry-run] would write tab '{tab}': {n} rows x {len(grid[0])} cols"
                  + (" (TRUNCATED)" if trunc else ""))
        return row_counts

    tabs_list = list(grids.keys())
    clear_ranges = [f"'{t}'" for t in tabs_list]
    resp = google_auth.authed_request("POST", f"{SHEETS_BASE}/{sheet_id}/values:batchClear",
                                       json={"ranges": clear_ranges})
    if resp.status_code != 200:
        raise RuntimeError(f"batchClear HTTP {resp.status_code}: {resp.text[:500]}")

    data_entries = [{"range": f"'{t}'!A1", "values": grids[t]} for t in tabs_list]
    for chunk in chunk_data_by_size(data_entries):
        resp = google_auth.authed_request("POST", f"{SHEETS_BASE}/{sheet_id}/values:batchUpdate",
                                           json={"valueInputOption": "RAW", "data": chunk})
        if resp.status_code != 200:
            raise RuntimeError(f"batchUpdate values HTTP {resp.status_code}: {resp.text[:500]}")

    return row_counts


def write_run_log(sheet_id, data, dry_run, trigger="nightly", request_row=""):
    row = build_run_log_row(data, trigger=trigger, request_row=request_row)
    header = list(row.keys())
    if dry_run:
        print(f"[dry-run] would append 1 row to run_log: {row}")
        return
    # Header handling (PRD-month-refresh.md Section 5 M5, replaces the old fixed
    # 'run_log'!A1:F1 six-column check): read the WHOLE existing header row; if its
    # column count differs from this run's row (e.g. an old six-label header meeting an
    # eight-key row once trigger/request_row are added), PUT a new header that keeps
    # every existing label in its existing order and appends any of this row's labels
    # not already present. When the lengths already match, no header PUT happens at
    # all -- this fires on every run whose row shape changed, not just the first ever
    # write, and it is idempotent (a stable header produces no PUT on subsequent runs).
    hdr_rng = urllib.parse.quote("'run_log'!1:1", safe="")
    r = google_auth.authed_request("GET", f"{SHEETS_BASE}/{sheet_id}/values/{hdr_rng}")
    existing_header = []
    if r.status_code == 200:
        rows = r.json().get("values") or []
        if rows:
            existing_header = rows[0]
    if len(existing_header) != len(header):
        new_header = list(existing_header)
        for label in header:
            if label not in new_header:
                new_header.append(label)
        rng_a1 = urllib.parse.quote("'run_log'!A1", safe="")
        r = google_auth.authed_request("PUT", f"{SHEETS_BASE}/{sheet_id}/values/{rng_a1}",
                                        params={"valueInputOption": "RAW"}, json={"values": [new_header]})
        if r.status_code != 200:
            raise RuntimeError(f"write run_log header HTTP {r.status_code}: {r.text[:500]}")
    # Idempotent append: a 429 retry can land the same row twice (seen 2026-08-27 00:11 MT). Skip when
    # the last logged pulled_at_mt already equals this run's.
    col_a = urllib.parse.quote("'run_log'!A:A", safe="")
    ra = google_auth.authed_request("GET", f"{SHEETS_BASE}/{sheet_id}/values/{col_a}")
    if ra.status_code == 200:
        col = ra.json().get("values") or []
        if col and col[-1] and str(col[-1][0]) == str(row.get("pulled_at_mt")):
            print("[publish_sheet] run_log already has this run's row; not appending again")
            return
    rng = urllib.parse.quote("'run_log'", safe="")
    values_row = [cell_value(row[k]) for k in header]
    r = google_auth.authed_request("POST", f"{SHEETS_BASE}/{sheet_id}/values/{rng}:append",
                                    params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                                    json={"values": [values_row]})
    if r.status_code != 200:
        raise RuntimeError(f"append run_log HTTP {r.status_code}: {r.text[:500]}")


def load_protected_tabs():
    """CFO-owned Sheet tabs this script must never write to (research/09 Section 3,
    PRD-v2.md:264). Read from spike/config/rollups.json's `protected_tabs`; falls back
    to the hardcoded default if the config is missing/unparseable/absent the key, so a
    config-loading bug can never accidentally DROP the protection."""
    try:
        rollups = json.loads((Path(__file__).parent / "config" / "rollups.json").read_text(encoding="utf-8"))
        tabs = rollups.get("protected_tabs")
        if tabs:
            return list(tabs)
    except (OSError, json.JSONDecodeError):
        pass
    return list(DEFAULT_PROTECTED_TABS)


def store_sheet_id(sheet_id):
    project = doppler_env.doppler_project()
    config = doppler_env.doppler_config()
    if not (project and config):
        print("DOPPLER_WRITEBACK_SKIPPED set DOPPLER_PROJECT and DOPPLER_CONFIG to persist SPIKEBALL_FINANCE_SHEET_ID")
        return False
    r = subprocess.run(
        ["doppler", "secrets", "set", f"SPIKEBALL_FINANCE_SHEET_ID={sheet_id}",
         "--project", project, "--config", config, "--silent"],
        capture_output=True, text=True,
    )
    print(f"PUBLISH_SHEET_STORED_ID doppler_rc={r.returncode} {r.stderr.strip()[:200]}")
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="Publish latest.json to the Spikeball Google Sheet.")
    ap.add_argument("--data", required=True, help="Path to the data JSON.")
    ap.add_argument("--sheet", default=None, help="Existing spreadsheet id to write to.")
    ap.add_argument("--create", nargs="?", const="Spikeball Finance Data", default=None,
                     help="Create a new spreadsheet (default title 'Spikeball Finance Data') "
                          "in mcohen@spikeball.com's My Drive and write to it.")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be written; no API calls.")
    ap.add_argument("--force", action="store_true", help="Write even if meta.checks.all_pass is false.")
    ap.add_argument("--trigger", default="nightly", help="run_log 'trigger' column value (default 'nightly'; run_nightly.py --gate passes 'request' when a queued refresh_requests row fired the run).")
    ap.add_argument("--request-row", default="", help="run_log 'request_row' column value (default ''; run_nightly.py --gate passes the comma-joined honored refresh_requests row numbers).")
    args = ap.parse_args()

    if not args.sheet and not args.create:
        print("PUBLISH_SHEET_ERROR need --sheet ID or --create [TITLE]")
        return 1

    if not os.path.isfile(args.data):
        print(f"PUBLISH_SHEET_ERROR data file not found: {args.data}")
        return 1
    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)

    all_pass, detail = get_all_pass(data)
    if not all_pass and not args.force:
        print(f"PUBLISH_SHEET_REFUSED {detail}")
        return 2
    if not all_pass and args.force:
        print(f"PUBLISH_SHEET_WARNING forcing write despite failing checks: {detail}")
    else:
        print(f"PUBLISH_SHEET_CHECKS_OK {detail}")

    if not args.dry_run:
        try:
            project_id = google_auth.resolve_project_id()
            already, newly = google_auth.ensure_apis_enabled(
                project_id, ["sheets.googleapis.com", "drive.googleapis.com"])
            if newly:
                print(f"PUBLISH_SHEET_ENABLED_APIS {newly} on project {project_id}")
            else:
                print(f"PUBLISH_SHEET_APIS_ALREADY_ENABLED {already} on project {project_id}")
        except google_auth.GoogleAuthError as e:
            print(f"PUBLISH_SHEET_ERROR {e}")
            return 1

    sheet_id = args.sheet
    if args.create:
        if args.dry_run:
            print(f"[dry-run] would create spreadsheet titled '{args.create}' in mcohen@spikeball.com's My Drive")
            sheet_id = sheet_id or "DRY_RUN_SHEET_ID"
        else:
            try:
                resp = google_auth.authed_request("POST", SHEETS_BASE, json={"properties": {"title": args.create}})
            except google_auth.GoogleAuthError as e:
                print(f"PUBLISH_SHEET_ERROR {e}")
                return 1
            if resp.status_code != 200:
                print(f"PUBLISH_SHEET_ERROR create spreadsheet HTTP {resp.status_code}: {resp.text[:500]}")
                return 1
            body = resp.json()
            sheet_id = body["spreadsheetId"]
            url = body.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
            print(f"PUBLISH_SHEET_CREATED id={sheet_id} url={url}")
            store_sheet_id(sheet_id)

    try:
        tables = build_tables(data)
    except Exception as e:  # noqa: BLE001
        print(f"PUBLISH_SHEET_ERROR building tables from {args.data}: {e}")
        return 1

    needed_titles = list(dict.fromkeys(list(tables.keys()) + ["run_log"]))  # meta is already in tables.keys()

    # Protected-tab guard (research/09 Section 3, PRD-v2.md:264/428-430). Under correct
    # operation no builder ever emits a top-level key matching a protected tab TITLE, so
    # this filter is a no-op every normal run; it exists as a belt-and-suspenders catch
    # for a future bug (e.g. someone re-exports the parsed Demand Plan under a matching
    # key). Filter first, then re-verify -- if a protected title is STILL present after
    # filtering, that means the filter itself is broken, so refuse to write anything at
    # all rather than risk a partial write that clobbers what the CFO typed.
    protected_tabs = load_protected_tabs()
    excluded = [t for t in needed_titles if t in protected_tabs]
    if excluded:
        print(f"[publish_sheet] excluding protected tab(s) from this run's write set: {excluded}")
    needed_titles = [t for t in needed_titles if t not in protected_tabs]
    tables = {k: v for k, v in tables.items() if k not in protected_tabs}
    if any(t in protected_tabs for t in needed_titles) or any(k in protected_tabs for k in tables):
        print(f"PUBLISH_SHEET_ERROR a protected tab ({protected_tabs}) is still present in the "
              f"write set after filtering -- refusing to write anything this run")
        return 1

    if args.dry_run:
        print(f"[dry-run] target sheet_id={sheet_id}")
        print(f"[dry-run] tabs to ensure exist: {needed_titles}")
        row_counts = write_all_tabs(sheet_id, tables, dry_run=True)
        write_run_log(sheet_id, data, dry_run=True, trigger=args.trigger, request_row=args.request_row)
        result = {"dry_run": True, "sheet_id": sheet_id,
                  "tabs": {t: {"rows": n, "truncated": trunc} for t, (n, trunc) in row_counts.items()}}
        print("PUBLISH_SHEET_RESULT " + json.dumps(result))
        return 0

    try:
        existing = get_existing_tabs(sheet_id)
        existing = sync_tab_structure(sheet_id, needed_titles, existing)

        row_counts = write_all_tabs(sheet_id, tables, dry_run=False)
        result = {"dry_run": False, "sheet_id": sheet_id,
                  "tabs": {t: {"rows": n, "truncated": trunc} for t, (n, trunc) in row_counts.items()}}

        write_run_log(sheet_id, data, dry_run=False, trigger=args.trigger, request_row=args.request_row)
    except (RuntimeError, google_auth.GoogleAuthError) as e:
        print(f"PUBLISH_SHEET_ERROR {e}")
        return 1

    result["sheet_url"] = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print("PUBLISH_SHEET_RESULT " + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
