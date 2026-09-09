#!/usr/bin/env python3
"""Prints Looker Studio "Linking API" URLs that open a new report pre-bound to the
Spikeball dashboard's BigQuery views (primary) and Sheet tabs (fallback), per PRD FD4/
FD7. No API call happens here -- Looker Studio needs an interactive Google session, so
the owner opens the printed URL themselves (PRD Section 8: "The owner clicks this in
the morning"). This script only builds URLs; nothing here can create or modify a
report on its own.

Usage:
    python spike/routine/looker_link.py --project <gcp-project-id> --dataset spikeball_finance
    python spike/routine/looker_link.py --project <id> --dataset spikeball_finance \\
        --sheet <SHEET_ID>   # also prints the Sheets-bound variant

If --sheet is omitted, it falls back to Doppler env SPIKEBALL_FINANCE_SHEET_ID; if
neither is available the Sheets-bound URL is skipped with a note.
"""
import argparse
import os
import sys
import urllib.parse

# Documented form (developers.google.com/looker-studio/integrate/linking-api): the URL must be
# https://datastudio.google.com/reporting/create?<params>. Report name is r.reportName. A report created
# WITHOUT a template (no c.reportId) has exactly one embedded data source and takes NO alias
# (ds.connector=..., not ds.ds0.connector=...); the ds.ds0/ds1... aliases exist only to address the data
# sources of a template report. Getting either wrong fails with "ds0 is not a valid data source alias".
BASE = "https://datastudio.google.com/reporting/create"

# name -> the BigQuery view built by publish_bq.py
BQ_VIEWS = [
    "v_rollup_by_month",
    "v_rollup_by_period",
    "v_sku_sales_ytd",
    "v_inventory_top_sellers",
    "v_returns",
]

# name -> the Sheet tab it mirrors (v1-safe: every one of these tabs is written from
# the v1 CONTRACT shape too, unlike the v2-only rollup_by_month/rollup_by_period views)
SHEET_TABS = [
    "pnl_by_channel_month",
    "pnl_by_channel_period",
    "sku_sales_ytd",
    "inventory_onhand",
    "returns_by_channel",
]

CHARTS = [
    "1. Scorecard -- MTD Revenue (rollup TOTAL row) with a YoY delta indicator.",
    "2. Scorecard -- YTD Revenue with a YoY delta indicator.",
    "3. Scorecard -- YTD Gross Margin % (flagged/excluded per rollups.json show_margin).",
    "4. Time series (stacked area) -- 13-month revenue by channel group, from v_rollup_by_month.",
    "5. Table -- Channel P&L: MTD/YTD revenue, COGS, GP, margin% per group + Total, from v_rollup_by_period.",
    "6. Bar chart -- Top 10 SKUs by revenue, channel-filterable, from v_sku_sales_ytd.",
    "7. Donut chart -- Top-5 SKU concentration vs. the rest, per channel, from sku_sales_top5.",
    "8. Table -- Inventory OOS + top-seller on-hand by location and days-on-hand, from v_inventory_top_sellers.",
    "9. Bar chart -- Return rate % by channel (MTD/YTD), from v_returns.",
    "10. Table + scorecard -- Spikeball.com US vs. Other region split (rollup) plus a freshness "
    "scorecard (as-of date, meta.checks.all_pass) from the meta tab.",
]


def bq_datasource_params(alias, project, dataset, view):
    p = f"ds.{alias}." if alias else "ds."
    return {
        p + "connector": "bigQuery",
        p + "type": "TABLE",
        p + "projectId": project,
        p + "datasetId": dataset,
        p + "tableId": view,
        p + "billingProjectId": project,
        p + "datasourceName": view,
    }


def sheets_datasource_params(alias, spreadsheet_id, worksheet_name):
    p = f"ds.{alias}." if alias else "ds."
    return {
        p + "connector": "googleSheets",
        p + "type": "TABLE",
        p + "spreadsheetId": spreadsheet_id,
        p + "worksheetName": worksheet_name,
        p + "datasourceName": worksheet_name,
    }


def build_bq_url(project, dataset, views, template=None):
    params = {"r.reportName": "Spikeball Finance"}
    if template:
        params["c.reportId"] = template
        for i, v in enumerate(views):
            params.update(bq_datasource_params(f"ds{i}", project, dataset, v))
    else:
        params.update(bq_datasource_params(None, project, dataset, views[0]))
    return BASE + "?" + urllib.parse.urlencode(params)


def build_sheets_url(spreadsheet_id, tabs, template=None):
    params = {"r.reportName": "Spikeball Finance (Sheet-bound)"}
    if template:
        params["c.reportId"] = template
        for i, t in enumerate(tabs):
            params.update(sheets_datasource_params(f"ds{i}", spreadsheet_id, t))
    else:
        params.update(sheets_datasource_params(None, spreadsheet_id, tabs[0]))
    return BASE + "?" + urllib.parse.urlencode(params)


def main():
    ap = argparse.ArgumentParser(description="Print Looker Studio Linking API URLs for the Spikeball dashboard.")
    ap.add_argument("--project", required=True, help="GCP project id that owns the BigQuery dataset.")
    ap.add_argument("--dataset", default="spikeball_finance")
    ap.add_argument("--sheet", default=None, help="Spreadsheet id (default: Doppler SPIKEBALL_FINANCE_SHEET_ID).")
    ap.add_argument("--template", default=None,
                    help="Report id of an existing Spikeball Finance report whose data sources are aliased "
                         "ds0..ds4 in this order: " + ", ".join(BQ_VIEWS) + ". With it, one URL creates a "
                         "full copy bound to all five views; without it only the first view can be embedded.")
    args = ap.parse_args()

    bq_url = build_bq_url(args.project, args.dataset, BQ_VIEWS, args.template)
    if args.template:
        print("Looker Studio report copied from the template, all five BigQuery views rebound:")
    else:
        print("Looker Studio report bound to BigQuery view " + BQ_VIEWS[0] + " (the Linking API embeds "
              "exactly one data source when no template exists; add the other four inside the report "
              "via Add data > BigQuery > " + args.project + " > " + args.dataset + "):")
    print(bq_url)
    print("  remaining views to add: " + ", ".join(BQ_VIEWS[1:]))
    print()

    sheet_id = args.sheet or os.environ.get("SPIKEBALL_FINANCE_SHEET_ID")
    if sheet_id:
        sheets_url = build_sheets_url(sheet_id, SHEET_TABS, args.template)
        print("Looker Studio report bound to the Sheet tab " + SHEET_TABS[0] + " (fallback / v1-compatible; "
              "same one-source rule):")
        print(sheets_url)
    else:
        print("Sheets-bound URL skipped: no --sheet given and SPIKEBALL_FINANCE_SHEET_ID is not "
              "set in the environment. Pass --sheet <SHEET_ID> to get this variant too.")
    print()

    print("Opening either URL requires an interactive Google session signed in as an account with "
          "at least Viewer access to the BigQuery dataset / the Sheet -- Looker Studio will prompt "
          "for data source authorization on first open. If a prefilled field (table id, worksheet "
          "name) isn't recognized, Looker Studio shows its normal data source picker instead of "
          "failing; finish the connection there.")
    print()
    print("10 charts to add once the report opens:")
    for line in CHARTS:
        print("  " + line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
