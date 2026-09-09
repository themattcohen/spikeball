#!/usr/bin/env python3
"""Publish spike/data/latest.json to the Spikeball-owned BigQuery dataset, per PRD FD2 /
CONTRACT.md "Publishers". Same table set as publish_sheet.py's tabs (imports its
`build_tables()` so the two publishers can never drift), plus amazon_orders_raw /
amazon_order_items_raw loaded from spike/data/amazon/*.jsonl when present. Delete-then-
load per table (WRITE_TRUNCATE via a load job with an explicit schema, autodetect off)
-- this also resets each table's 60-day sandbox-mode expiration clock every run (see
routine/README.md "Sandbox mode"). run_log and run_state are append-only via a
WRITE_APPEND load job (streaming inserts are unavailable in BigQuery sandbox mode, so
every write in this script -- including single-row appends -- goes through the batch
load-job API, never tabledata.insertAll).

Usage:
    doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- \\
        python spike/publish_bq.py --data spike/data/latest.json --dataset spikeball_finance
    python spike/publish_bq.py --data spike/data/latest.json --dataset spikeball_finance --dry-run

    doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- \\
        python spike/publish_bq.py fetch-state --dataset spikeball_finance --out spike/data/state_prev.json

Refuses to write when meta.checks.all_pass is false unless --force (same gate as
publish_sheet.py). --project defaults to auto-resolving the GCP project that owns the
Spikeball OAuth client (google_auth.resolve_project_id()).
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import google_auth  # noqa: E402
from publish_sheet import build_tables, flatten_row, get_all_pass, now_mt_iso  # noqa: E402

BQ_BASE = "https://bigquery.googleapis.com/bigquery/v2"
BQ_UPLOAD_BASE = "https://bigquery.googleapis.com/upload/bigquery/v2"

RUN_LOG_FIELDS = [
    {"name": "pulled_at_mt", "type": "STRING", "mode": "NULLABLE"},
    {"name": "asof_date", "type": "STRING", "mode": "NULLABLE"},
    {"name": "all_pass", "type": "BOOL", "mode": "NULLABLE"},
    {"name": "sections_json", "type": "STRING", "mode": "NULLABLE"},
    {"name": "checks_json", "type": "STRING", "mode": "NULLABLE"},
    {"name": "published_at_mt", "type": "STRING", "mode": "NULLABLE"},
]
RUN_STATE_FIELDS = [
    {"name": "recorded_at_mt", "type": "STRING", "mode": "NULLABLE"},
    {"name": "state_json", "type": "STRING", "mode": "NULLABLE"},
]
# Durable monthly V-D balance-sheet snapshot store (append-only; one nightly run appends
# one month's worth of rows). Fixed schema, matching extract_v2_bs_snapshot._row /
# _synthetic_ni_row's shape plus captured_at, so appends never drift between runs the way
# an inferred schema could (e.g. a field that happens to be null on one run and not another).
BS_SNAPSHOT_FIELDS = [
    {"name": "account_id", "type": "STRING", "mode": "NULLABLE"},
    {"name": "acctnumber", "type": "STRING", "mode": "NULLABLE"},
    {"name": "account_name", "type": "STRING", "mode": "NULLABLE"},
    {"name": "accttype", "type": "STRING", "mode": "NULLABLE"},
    {"name": "parent_id", "type": "STRING", "mode": "NULLABLE"},
    {"name": "level", "type": "INT64", "mode": "NULLABLE"},
    {"name": "path", "type": "STRING", "mode": "NULLABLE"},
    {"name": "is_leaf", "type": "BOOL", "mode": "NULLABLE"},
    {"name": "ym", "type": "STRING", "mode": "NULLABLE"},
    {"name": "balance", "type": "FLOAT64", "mode": "NULLABLE"},
    {"name": "method", "type": "STRING", "mode": "NULLABLE"},
    {"name": "captured_at", "type": "STRING", "mode": "NULLABLE"},
]

# Looker views: name -> (project, dataset) -> SELECT sql. Every source table is a v1/v2
# CONTRACT key or its build_tables() split; skipped gracefully when a source table
# wasn't loaded this run (v1 payload predating a v2 key, or the raw amazon_orders
# tables when extract.py hasn't produced *.jsonl yet).
VIEWS = {
    "v_rollup_by_month": lambda p, d: f"SELECT * FROM `{p}.{d}.rollup_by_month`",
    "v_rollup_by_period": lambda p, d: f"SELECT * FROM `{p}.{d}.rollup_by_period`",
    "v_sku_sales_ytd": lambda p, d: f"SELECT * FROM `{p}.{d}.sku_sales_ytd`",
    "v_inventory_top_sellers": lambda p, d: (
        f"SELECT o.*, dh.units_90d, dh.avg_daily_units, dh.days_on_hand "
        f"FROM `{p}.{d}.inventory_onhand` o "
        f"JOIN `{p}.{d}.inventory_days_on_hand` dh ON o.sku = dh.sku"
    ),
    # direct channels only (Amazon, Wholesale/Retail, Major Retail, SMB, Spikeball.com): the small channels have
    # credits against near-zero revenue and produce meaningless rates (-100%) on a chart
    "v_returns": lambda p, d: f"SELECT * FROM `{p}.{d}.returns_by_channel` WHERE channel_id IN (1, 2, 3, 4, 5)",
    # Looker-shaped views (2026-08-27): pre-filtered so report charts need no Looker-side filters.
    # v_kpi is the single Total row of the roll-up plus the run's freshness fields (one row).
    "v_kpi": lambda p, d: (
        f"SELECT r.*, "
        f"(SELECT value FROM `{p}.{d}.meta` WHERE key = 'asof_date') AS asof_date, "
        f"(SELECT value FROM `{p}.{d}.meta` WHERE key = 'pulled_at_mt') AS pulled_at_mt, "
        f"(SELECT value FROM `{p}.{d}.meta` WHERE key = 'checks_all_pass') AS checks_all_pass "
        f"FROM `{p}.{d}.rollup_by_period` r WHERE r.key = 'total'"
    ),
    # roll-up groups only (no Total row) so sums and stacks do not double count
    "v_rollup_groups_period": lambda p, d: f"SELECT * FROM `{p}.{d}.rollup_by_period` WHERE key != 'total'",
    "v_rollup_groups_month": lambda p, d: (
        f"SELECT PARSE_DATE('%Y-%m', ym) AS month, * FROM `{p}.{d}.rollup_by_month` WHERE key != 'total'"
    ),
    "v_rollup_total_month": lambda p, d: (
        f"SELECT PARSE_DATE('%Y-%m', ym) AS month, * FROM `{p}.{d}.rollup_by_month` WHERE key = 'total'"
    ),
    "v_dtc_by_region": lambda p, d: f"SELECT key AS region_group, mtd, ytd, trailing13 FROM `{p}.{d}.dtc_by_region_rollup`",
    "v_sku_top5": lambda p, d: f"SELECT * FROM `{p}.{d}.sku_sales_top5`",
    "v_amazon_marketplaces": lambda p, d: f"SELECT * FROM `{p}.{d}.amazon_orders_by_marketplace_mtd`",
    "v_inventory_summary": lambda p, d: f"SELECT * FROM `{p}.{d}.inventory_summary`",
    # v2 actual-only views (PRD-v2 v0.6). Each skips gracefully if its source table was not
    # loaded this run (e.g. a v1 payload predating v2). Looker charts filter/aggregate these.
    "v_pnl_account_month": lambda p, d: (
        f"SELECT *, PARSE_DATE('%Y-%m', ym) AS month FROM `{p}.{d}.pnl_by_account_month`"
    ),
    "v_pnl_channel_gross_net": lambda p, d: (
        f"SELECT *, PARSE_DATE('%Y-%m', ym) AS month FROM `{p}.{d}.pnl_channel_gross_net`"
    ),
    "v_ebitda_month": lambda p, d: (
        f"SELECT *, PARSE_DATE('%Y-%m', ym) AS month FROM `{p}.{d}.ebitda_month`"
    ),
    # Snapshot-wins assembly (durable V-D balance-sheet series). bs_snapshot is the
    # append-only store of every nightly current-month snapshot ever captured (research/10 +
    # research/11's account.balance method); bs_by_account_month is truncate-loaded fresh
    # each run and holds this run's anchor+increment history PLUS this run's own current-month
    # snapshot rows (method='snapshot'/'snapshot+re_rollup') -- the hist CTE below excludes
    # those so they come only from the durable store, never duplicated from the transient one.
    # For each (account_id, ym): prefer the LATEST captured_at row from bs_snapshot; fall back
    # to the anchor/increment row in bs_by_account_month for any (account_id, ym) bs_snapshot
    # has never captured (pre-snapshot history, or an account not yet snapshotted).
    "v_bs_month": lambda p, d: (
        "WITH snap_latest AS ("
        "  SELECT * EXCEPT(rn) FROM ("
        "    SELECT s.*, ROW_NUMBER() OVER ("
        "      PARTITION BY account_id, ym ORDER BY captured_at DESC"
        "    ) AS rn "
        f"    FROM `{p}.{d}.bs_snapshot` s"
        "  ) WHERE rn = 1"
        "), hist AS ("
        f"  SELECT * FROM `{p}.{d}.bs_by_account_month` "
        "  WHERE method IN ('anchor', 'increment')"
        ") "
        "SELECT account_id, acctnumber, account_name, accttype, parent_id, level, path, "
        "  is_leaf, ym, balance, method, PARSE_DATE('%Y-%m', ym) AS month "
        "FROM snap_latest "
        "UNION ALL "
        "SELECT h.account_id, h.acctnumber, h.account_name, h.accttype, h.parent_id, h.level, "
        "  h.path, h.is_leaf, h.ym, h.balance, h.method, PARSE_DATE('%Y-%m', h.ym) AS month "
        "FROM hist h "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM snap_latest s WHERE s.account_id = h.account_id AND s.ym = h.ym"
        ")"
    ),
    "v_cash_positions": lambda p, d: f"SELECT * FROM `{p}.{d}.cash_positions`",
    "v_cf_month": lambda p, d: (
        f"SELECT *, PARSE_DATE('%Y-%m', ym) AS month FROM `{p}.{d}.cf_month`"
    ),
    "v_ar_aging": lambda p, d: f"SELECT * FROM `{p}.{d}.ar_aging`",
    "v_ap_aging": lambda p, d: f"SELECT * FROM `{p}.{d}.ap_aging`",
    "v_open_orders": lambda p, d: f"SELECT * FROM `{p}.{d}.open_orders`",
    "v_item_cost": lambda p, d: f"SELECT * FROM `{p}.{d}.item_cost`",
    # research/09-cfo-input-mechanism-design.md: plan side from the CFO's read-only Demand
    # Plan tab, actual side from the new sku_sales_month grain. Skips gracefully (like every
    # view above) until the Demand Plan tab is seeded and this run's read is valid.
    "v_demand_vs_actual": lambda p, d: (
        f"SELECT *, PARSE_DATE('%Y-%m', ym) AS month FROM `{p}.{d}.demand_vs_actual_plan_vs_actual`"
    ),
    "v_demand_cost_coverage": lambda p, d: f"SELECT * FROM `{p}.{d}.demand_vs_actual_cost_coverage`",
}


# ---------------------------------------------------------------------------
# schema inference
# ---------------------------------------------------------------------------

def sanitize_field_name(name):
    name = re.sub(r"[^a-zA-Z0-9_]", "_", str(name))
    if not name or name[0].isdigit():
        name = "_" + name
    return name[:300]


def infer_field_type(values):
    if not values:
        return "STRING"
    if all(isinstance(v, bool) for v in values):
        return "BOOL"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "INT64"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "FLOAT64"
    return "STRING"


def build_schema_and_order(rows):
    """Union of keys across all rows, insertion order, so every row's field lines up
    with the same schema regardless of which fields any single row happens to carry."""
    order = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                order.append(k)
    cols = {k: [] for k in order}
    for r in rows:
        for k in order:
            v = r.get(k)
            if v is not None:
                cols[k].append(v)
    fields = []
    name_map = {}
    for k in order:
        fname = sanitize_field_name(k)
        name_map[k] = fname
        fields.append({"name": fname, "type": infer_field_type(cols[k]), "mode": "NULLABLE"})
    if not fields:
        fields = [{"name": "_empty", "type": "BOOL", "mode": "NULLABLE"}]
    return fields, order, name_map


def rows_to_ndjson(rows, order, name_map):
    lines = []
    for r in rows:
        if not r:
            continue
        obj = {name_map[k]: r.get(k) for k in order}
        lines.append(json.dumps(obj, separators=(",", ":")))
    return ("\n".join(lines)).encode("utf-8")


# ---------------------------------------------------------------------------
# BigQuery REST
# ---------------------------------------------------------------------------

def ensure_dataset(project, dataset, dry_run):
    if dry_run:
        print(f"[dry-run] would ensure dataset {project}.{dataset} exists (location US)")
        return "dry_run"
    url = f"{BQ_BASE}/projects/{project}/datasets/{dataset}"
    r = google_auth.authed_request("GET", url)
    if r.status_code == 200:
        return "exists"
    if r.status_code != 404:
        raise RuntimeError(f"get dataset HTTP {r.status_code}: {r.text[:500]}")
    r = google_auth.authed_request("POST", f"{BQ_BASE}/projects/{project}/datasets", json={
        "datasetReference": {"projectId": project, "datasetId": dataset},
        "location": "US",
    })
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create dataset HTTP {r.status_code}: {r.text[:500]}")
    return "created"


def delete_table_if_exists(project, dataset, table):
    url = f"{BQ_BASE}/projects/{project}/datasets/{dataset}/tables/{table}"
    r = google_auth.authed_request("DELETE", url)
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"delete table {table} HTTP {r.status_code}: {r.text[:500]}")


def build_multipart_body(metadata, media_bytes, boundary):
    b = boundary.encode()
    parts = [
        b"--" + b + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata).encode("utf-8") + b"\r\n",
        b"--" + b + b"\r\nContent-Type: application/octet-stream\r\n\r\n"
        + media_bytes + b"\r\n",
        b"--" + b + b"--",
    ]
    return b"".join(parts)


def poll_job(project, job_id, location=None, timeout_sec=240):
    url = f"{BQ_BASE}/projects/{project}/jobs/{job_id}"
    params = {"location": location} if location else {}
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = google_auth.authed_request("GET", url, params=params)
        if r.status_code != 200:
            raise RuntimeError(f"job status HTTP {r.status_code}: {r.text[:500]}")
        body = r.json()
        if body.get("status", {}).get("state") == "DONE":
            return body["status"]
        time.sleep(2)
    raise RuntimeError(f"job {job_id} did not reach DONE within {timeout_sec}s")


def run_load_job(project, dataset, table, rows, write_disposition, schema=None, dry_run=False):
    if dry_run:
        print(f"[dry-run] would {write_disposition} load {len(rows)} row(s) into {table}")
        return {"table": table, "rows_in_payload": len(rows), "dry_run": True}
    if schema is None:
        schema, order, name_map = build_schema_and_order(rows)
    else:
        order = [f["name"] for f in schema]
        name_map = {k: k for k in order}
    ndjson = rows_to_ndjson(rows, order, name_map)
    metadata = {"configuration": {"load": {
        "destinationTable": {"projectId": project, "datasetId": dataset, "tableId": table},
        "sourceFormat": "NEWLINE_DELIMITED_JSON",
        "schema": {"fields": schema},
        "writeDisposition": write_disposition,
        "createDisposition": "CREATE_IF_NEEDED",
        "autodetect": False,
    }}}
    boundary = "spikeball_dash_" + uuid.uuid4().hex
    body = build_multipart_body(metadata, ndjson, boundary)
    r = google_auth.authed_request(
        "POST", f"{BQ_UPLOAD_BASE}/projects/{project}/jobs?uploadType=multipart",
        data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
    if r.status_code != 200:
        raise RuntimeError(f"load job insert for {table} HTTP {r.status_code}: {r.text[:800]}")
    job = r.json()
    job_ref = job["jobReference"]
    status = poll_job(job_ref["projectId"], job_ref["jobId"], job_ref.get("location"))
    if status.get("errorResult"):
        raise RuntimeError(f"load job {table} failed: {status['errorResult']}")
    return {"table": table, "schema_fields": len(schema), "rows_in_payload": len(rows)}


def load_table(project, dataset, table, rows, dry_run):
    """Truncate-load a data table in place. No delete first: WRITE_TRUNCATE + CREATE_IF_NEEDED replaces
    the rows and recreates a table the sandbox has expired. Deleting before loading tripped BigQuery's
    metadata consistency in the cloud run of 2026-08-27 00:11 MT (COUNT(*) returned 404 "table not
    found" right after the recreate)."""
    return run_load_job(project, dataset, table, rows, "WRITE_TRUNCATE", dry_run=dry_run)


def append_row(project, dataset, table, row, fields, dry_run):
    """Append-only via a WRITE_APPEND load job (sandbox-safe; no streaming insert)."""
    return run_load_job(project, dataset, table, [row], "WRITE_APPEND", schema=fields, dry_run=dry_run)


def append_rows(project, dataset, table, rows, fields, dry_run):
    """Same WRITE_APPEND load-job mechanism as append_row, for more than one row per call
    (bs_snapshot: one row per tracked account, appended as a single batch each run)."""
    return run_load_job(project, dataset, table, rows, "WRITE_APPEND", schema=fields, dry_run=dry_run)


def run_query(project, sql, timeout_ms=30000):
    r = google_auth.authed_request("POST", f"{BQ_BASE}/projects/{project}/queries",
                                    json={"query": sql, "useLegacySql": False, "timeoutMs": timeout_ms})
    if r.status_code != 200:
        raise RuntimeError(f"query HTTP {r.status_code}: {r.text[:800]}")
    body = r.json()
    if not body.get("jobComplete", True):
        job_ref = body["jobReference"]
        status = poll_job(job_ref["projectId"], job_ref["jobId"], job_ref.get("location"))
        if status.get("errorResult"):
            raise RuntimeError(f"query job failed: {status['errorResult']}")
        params = {"location": job_ref.get("location")} if job_ref.get("location") else {}
        r2 = google_auth.authed_request(
            "GET", f"{BQ_BASE}/projects/{project}/queries/{job_ref['jobId']}", params=params)
        if r2.status_code != 200:
            raise RuntimeError(f"query result fetch HTTP {r2.status_code}: {r2.text[:500]}")
        body = r2.json()
    if body.get("errors"):
        raise RuntimeError(f"query errors: {body['errors']}")
    return body


def query_scalar_rows(project, sql):
    body = run_query(project, sql)
    names = [f["name"] for f in body.get("schema", {}).get("fields", [])]
    out = []
    for row in body.get("rows", []) or []:
        out.append({n: c.get("v") for n, c in zip(names, row.get("f", []))})
    return out


def query_count(project, dataset, table):
    last = None
    for attempt in range(4):
        try:
            rows = query_scalar_rows(project, f"SELECT COUNT(*) AS n FROM `{project}.{dataset}.{table}`")
            return int(rows[0]["n"]) if rows else 0
        except RuntimeError as e:
            last = e
            msg = str(e)
            if "Not found" not in msg and "notFound" not in msg and "404" not in msg:
                raise
            time.sleep(5 * (attempt + 1))  # freshly (re)created table: metadata can lag a few seconds
    raise RuntimeError(f"query_count({table}) still not found after retries: {last}")


def create_or_replace_view(project, dataset, view_name, select_sql, dry_run):
    if dry_run:
        print(f"[dry-run] would CREATE OR REPLACE VIEW {view_name}")
        return "dry_run"
    ddl = f"CREATE OR REPLACE VIEW `{project}.{dataset}.{view_name}` AS {select_sql}"
    try:
        run_query(project, ddl)
        return "created"
    except RuntimeError as e:
        msg = str(e).lower()
        if "not found" in msg or "does not exist" in msg:
            print(f"[publish_bq] skipping view {view_name}: source table(s) not loaded this run")
            return "skipped_missing_source"
        raise


def load_amazon_raw(project, dataset, amazon_dir, dry_run):
    results = {}
    if not amazon_dir or not os.path.isdir(amazon_dir):
        print(f"[publish_bq] amazon raw dir not found ({amazon_dir}); skipping "
              f"amazon_orders_raw/amazon_order_items_raw (order-level Amazon data "
              f"not yet produced by extract.py)")
        return results
    files = sorted(glob.glob(os.path.join(amazon_dir, "*.jsonl")))
    if not files:
        print(f"[publish_bq] no *.jsonl files in {amazon_dir}; skipping amazon raw tables")
        return results
    buckets = {"amazon_orders_raw": [], "amazon_order_items_raw": []}
    for path in files:
        table = "amazon_order_items_raw" if "item" in os.path.basename(path).lower() else "amazon_orders_raw"
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    # Amazon's raw rows carry nested "Money"-shaped fields (e.g.
                    # OrderTotal: {"CurrencyCode": "USD", "Amount": "74.99"}, or null)
                    # -- flatten exactly like every other table so schema inference
                    # and the NDJSON payload agree on a flat, consistent shape.
                    buckets[table].append(flatten_row(json.loads(line)))
    for table, rows in buckets.items():
        if rows:
            results[table] = load_table(project, dataset, table, rows, dry_run)
    return results


def resolve_project_for_cli(args):
    """dry-run never needs a real token; --project (or a placeholder) is enough."""
    if args.dry_run:
        return args.project or "DRY_RUN_PROJECT"
    return google_auth.resolve_project_id(override=args.project)


def fetch_state(project, dataset, out_path, dry_run):
    if dry_run:
        print(f"[dry-run] would fetch latest run_state from {project}.{dataset} into {out_path}")
        return 0
    try:
        rows = query_scalar_rows(
            project, f"SELECT state_json FROM `{project}.{dataset}.run_state` "
                     f"ORDER BY recorded_at_mt DESC LIMIT 1")
    except RuntimeError as e:
        print(f"FETCH_STATE_EMPTY {e}")
        return 3
    if not rows or not rows[0].get("state_json"):
        print("FETCH_STATE_EMPTY no prior run_state rows found")
        return 3
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(rows[0]["state_json"])
    print(f"FETCH_STATE_OK wrote {out_path}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    argv = sys.argv[1:]
    action = "load"
    if argv and argv[0] in ("fetch-state", "load", "views"):
        action = argv[0]
        argv = argv[1:]

    ap = argparse.ArgumentParser(description="Publish latest.json to Spikeball BigQuery, or fetch prior run_state.")
    ap.add_argument("--data", default=None, help="Path to the data JSON (required for load).")
    ap.add_argument("--project", default=None, help="GCP project id (default: auto-resolve).")
    ap.add_argument("--dataset", default="spikeball_finance", help="BigQuery dataset name.")
    ap.add_argument("--state", default=None, help="Path to a --write-state JSON to append into run_state (load only).")
    ap.add_argument("--amazon-dir", default=None,
                     help="Dir of amazon_orders*.jsonl raw files (default: <dir of --data>/amazon).")
    ap.add_argument("--out", default=None, help="Output path for fetch-state.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Load even if meta.checks.all_pass is false.")
    args = ap.parse_args(argv)

    if action == "fetch-state":
        if not args.out:
            print("PUBLISH_BQ_ERROR fetch-state needs --out PATH")
            return 1
        try:
            project = resolve_project_for_cli(args)
        except google_auth.GoogleAuthError as e:
            print(f"PUBLISH_BQ_ERROR {e}")
            return 1
        return fetch_state(project, args.dataset, args.out, args.dry_run)

    if action == "views":
        # (re)create the Looker views only; no table loads. Used after a VIEWS change between nightly runs.
        try:
            project = resolve_project_for_cli(args)
            out = {}
            for view_name, sql_fn in VIEWS.items():
                out[view_name] = create_or_replace_view(project, args.dataset, view_name,
                                                        sql_fn(project, args.dataset), args.dry_run)
        except (RuntimeError, google_auth.GoogleAuthError) as e:
            print(f"PUBLISH_BQ_ERROR {e}")
            return 1
        print("PUBLISH_BQ_VIEWS " + json.dumps(out, default=str))
        return 0

    # action == load
    if not args.data:
        print("PUBLISH_BQ_ERROR --data PATH required")
        return 1
    if not os.path.isfile(args.data):
        print(f"PUBLISH_BQ_ERROR data file not found: {args.data}")
        return 1
    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)

    all_pass, detail = get_all_pass(data)
    if not all_pass and not args.force:
        print(f"PUBLISH_BQ_REFUSED {detail}")
        return 2
    if not all_pass and args.force:
        print(f"PUBLISH_BQ_WARNING forcing write despite failing checks: {detail}")
    else:
        print(f"PUBLISH_BQ_CHECKS_OK {detail}")

    try:
        project = resolve_project_for_cli(args)
    except google_auth.GoogleAuthError as e:
        print(f"PUBLISH_BQ_ERROR {e}")
        return 1

    if not args.dry_run:
        already, newly = google_auth.ensure_apis_enabled(project, ["bigquery.googleapis.com"])
        if newly:
            print(f"PUBLISH_BQ_ENABLED_APIS {newly} on project {project}")
        else:
            print(f"PUBLISH_BQ_APIS_ALREADY_ENABLED {already} on project {project}")

    try:
        ds_status = ensure_dataset(project, args.dataset, args.dry_run)
        print(f"PUBLISH_BQ_DATASET {project}.{args.dataset} status={ds_status}")

        tables = build_tables(data)
        result = {"dry_run": args.dry_run, "project": project, "dataset": args.dataset, "tables": {}}

        for table, rows in tables.items():
            info = load_table(project, args.dataset, table, rows, args.dry_run)
            if not args.dry_run:
                info["loaded_row_count"] = query_count(project, args.dataset, table)
            result["tables"][table] = info

        amazon_dir = args.amazon_dir or os.path.join(os.path.dirname(os.path.abspath(args.data)), "amazon")
        result["amazon_raw"] = load_amazon_raw(project, args.dataset, amazon_dir, args.dry_run)

        # Durable V-D balance-sheet snapshot store. build_tables() skips this key (see
        # publish_sheet.SKIP_TOP_LEVEL_KEYS) so it never became a truncate-loaded table
        # above; append it here, one batch per run, before the views loop so bs_snapshot
        # exists (CREATE_IF_NEEDED, same ordering as run_log/run_state) by the time
        # v_bs_month is (re)created.
        bs_snapshot_rows = data.get("bs_snapshot_append") or []
        if bs_snapshot_rows:
            append_rows(project, args.dataset, "bs_snapshot", bs_snapshot_rows, BS_SNAPSHOT_FIELDS, args.dry_run)
            result["bs_snapshot_appended"] = len(bs_snapshot_rows)
        else:
            print("[publish_bq] bs_snapshot_append empty this run; bs_snapshot not appended")
            result["bs_snapshot_appended"] = 0

        run_log_row = {
            "pulled_at_mt": (data.get("meta") or {}).get("pulled_at_mt"),
            "asof_date": (data.get("meta") or {}).get("asof_date"),
            "all_pass": all_pass,
            "sections_json": json.dumps((data.get("meta") or {}).get("sections") or {}, separators=(",", ":")),
            "checks_json": json.dumps((data.get("meta") or {}).get("checks"), separators=(",", ":"))
            if (data.get("meta") or {}).get("checks") is not None else "",
            "published_at_mt": now_mt_iso(),
        }
        append_row(project, args.dataset, "run_log", run_log_row, RUN_LOG_FIELDS, args.dry_run)
        result["run_log_appended"] = True

        if args.state:
            if not os.path.isfile(args.state):
                print(f"PUBLISH_BQ_WARNING --state file not found, run_state not updated: {args.state}")
            else:
                with open(args.state, encoding="utf-8") as f:
                    state_content = f.read()
                append_row(project, args.dataset, "run_state",
                           {"recorded_at_mt": now_mt_iso(), "state_json": state_content},
                           RUN_STATE_FIELDS, args.dry_run)
                result["run_state_appended"] = True
        else:
            print("PUBLISH_BQ_NOTE no --state given; run_state not updated this run")
            result["run_state_appended"] = False

        result["views"] = {}
        for view_name, sql_fn in VIEWS.items():
            result["views"][view_name] = create_or_replace_view(
                project, args.dataset, view_name, sql_fn(project, args.dataset), args.dry_run)

    except (RuntimeError, google_auth.GoogleAuthError) as e:
        print(f"PUBLISH_BQ_ERROR {e}")
        return 1

    print("PUBLISH_BQ_RESULT " + json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
