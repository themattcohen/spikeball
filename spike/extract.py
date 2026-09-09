"""extract.py -- FD1a/FD1b hardened extract for the Spikeball Financial Dashboard.

READ-ONLY. Pulls the v2 KPI set (`spike/CONTRACT.md`) with real numbers from NetSuite
production (account 4201313, SuiteQL SELECT only) and Amazon SP-API Orders API
(`amazon_orders.py`, GET-only) into one JSON file. Never PATCHes/POSTs a NetSuite record,
never touches Celigo, never writes to Amazon, never writes to Sheets/BigQuery (that is
`publish_sheet.py`/`publish_bq.py`, a different owner).

Built from research/00-findings.md, research/01a-netsuite-coverage.md (every NetSuite query
used here), spike/CONTRACT.md (the binding v2 output shape), spike/config/rollups.json (the
channel roll-up / region-clumping configuration, never hardcoded), and PRD.md Sections 5
(E1/E2), 7 (T1-T8), 12 (rulings R6-R15, R18-R21).

_lib.py in this same folder is the proven OAuth1/SuiteQL client (paginated, retrying, raises
on error instead of returning an empty list). checks.py implements the E2 self-checks as
`meta.checks`. amazon_orders.py implements the FD1b Amazon Orders API pull (the flat-file
report path is dead -- 403/400, see research/01e-spapi-report-403.md -- Orders API is the
only live path per ruling R12).

Run:
  doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python spike/extract.py
  doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python spike/extract.py --skip-amazon
  doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python spike/extract.py \
      --out spike/data/latest.json --amazon-max-minutes 25 --write-state spike/data/state.json
  doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python spike/extract.py \
      --prev-state spike/data/state.json --skip-amazon

Credentials from env only (via doppler). Never prints or persists a secret value -- only
structural results (row counts, marketplace ids/names, HTTP statuses) are ever printed.

Exit code: 0 iff `meta.checks.all_pass`. The JSON is always written first, even on a failed
check, so a non-zero exit is diagnosable from the file itself (never a silent skip).
"""
from __future__ import annotations

import argparse
import calendar
import datetime
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from _lib import SuiteQLError, fnum, load_env, suiteql, try_suiteql
from checks import run_checks
from amazon_orders import run_amazon_orders
# v2 (actual-only) sections live in isolated modules so this file's v1 logic is unchanged.
from extract_v2 import (fetch_accounts, build_pnl_by_account_month, build_pnl_channel_gross_net,
                        build_ebitda_month, build_cf_month, build_ar_aging, build_ap_aging,
                        build_open_orders, build_item_cost, build_sku_sales_month,
                        build_demand_vs_actual)
from extract_v2_bs import build_bs_by_account_month
from extract_v2_bs_snapshot import build_bs_snapshot_current
from checks_v2 import run_checks_v2

MT = ZoneInfo("America/Denver")

# Known, documented, scheduled-for-deletion artifact (spikeball/claudedocs/golive-recon/
# CUTOVER-CHECKLIST-2026-08-20.md, "8/27 note"). Not adjusted here -- recorded only, per
# the spike brief ("just record known_artifacts in metadata, do not adjust numbers"); also
# the exemption list for checks.py check (g) (a documented month-level revenue swing).
KNOWN_ARTIFACTS = [
    {
        "type": "amazon_daily_fba_double_count",
        "ship_day": "2026-08-19",
        "records": [
            {"tranid": "INV818429", "otherrefnum": "DAILY-FBA-US-2026-08-19", "foreigntotal": 10109.00},
            {"tranid": "INV818963", "otherrefnum": "DAILY-FBA-US-2026-08-19-TU1", "foreigntotal": 6041.39},
            {"tranid": "INV820126", "otherrefnum": "DAILY-FBA-US-2026-08-19-TU2", "foreigntotal": 1410.66},
            {"tranid": "INV818428", "otherrefnum": "DAILY-FBA-CA-2026-08-19", "foreigntotal": 2557.24},
            {"tranid": "INV820127", "otherrefnum": "DAILY-FBA-CA-2026-08-19-TU1", "foreigntotal": 98.77},
            {"tranid": "INV818427", "otherrefnum": "DAILY-FBA-UK-2026-08-19", "foreigntotal": 409.68},
            {"tranid": "INV818962", "otherrefnum": "DAILY-FBA-UK-2026-08-19-TU1", "foreigntotal": 343.95},
        ],
        "total": 20970.69,
        "scheduled_deletion": "2026-08-27 or after",
        "note": (
            "Consolidated ship-day 8/19 double-counts against the 269 legacy per-order "
            "invoices that already own that day (the true-up engine's D+7 checkpoint "
            "re-touched 8/19 through 8/26; from 8/27 it moves to 8/20 and stops). Not "
            "adjusted in this extract -- Amazon MTD/YTD figures pulled before the 8/27 "
            "deletion include this double-count. Also exempts 2026-08 from checks.py's "
            "closed-month stability check once that deletion lands and August is closed."
        ),
    }
]

# T5 sentinel SKUs (PRD Section 7): two component SKUs used to prove the Income-line join
# METHOD (checks.py's t5_bom_rule), not to assert either SKU is absent from sku_sales --
# that original formulation was disproven live 2026-08-26 (P-RIM-003-BLA, "Replacement Rim
# - 3.0 - Black", turned out to have real standalone sales across Spikeball.com/Major
# Retail/PE/Rec; see build_t5_sentinels()' docstring and checks.py).
T5_SENTINEL_SKUS = ["P-RIM-003-BLA", "A-STICKER-001"]


# ---------------------------------------------------------------------------
# Date helpers -- all "today"/"now" math happens client-side in MT; NS SYSDATE is PST
# and is never used for date-boundary math (spikeball/CLAUDE.md gotcha).
# ---------------------------------------------------------------------------

def add_months(d: datetime.date, delta: int) -> datetime.date:
    total = d.year * 12 + (d.month - 1) + delta
    y, m = divmod(total, 12)
    return datetime.date(y, m + 1, 1)


def ym_str(d: datetime.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def shift_year(d: datetime.date, years: int) -> datetime.date:
    y = d.year + years
    m, day = d.month, d.day
    while True:
        try:
            return datetime.date(y, m, day)
        except ValueError:
            day -= 1  # Feb 29 -> Feb 28 etc.


def normalize_ns_date(s):
    """NetSuite SuiteQL date columns can come back as 'M/D/YYYY' or 'YYYY-MM-DD'
    depending on account locale; normalize to ISO. Unknown formats pass through raw."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def to_int_or_none(v):
    if v in (None, ""):
        return None
    return int(v)


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def load_rollups(path=None) -> dict:
    p = Path(path) if path else Path(__file__).parent / "config" / "rollups.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# NetSuite: shared lookups
# ---------------------------------------------------------------------------

def load_picklist(env, table):
    rows, err = try_suiteql(env, f"SELECT id, name, isinactive FROM {table} ORDER BY id")
    if err:
        rows, err2 = try_suiteql(env, f"SELECT id, name FROM {table} ORDER BY id")
        if err2:
            raise SuiteQLError(err2)
    return rows


def build_channels(env):
    rows = load_picklist(env, "customrecord_cseg_appf_channel")
    out = [
        {"id": int(r["id"]), "name": r["name"], "inactive": r.get("isinactive") == "T"}
        for r in rows
    ]
    return out, len(rows)


def build_regions(env):
    rows = load_picklist(env, "customrecord_cseg_appf_region")
    out = [
        {"id": int(r["id"]), "name": r["name"], "inactive": r.get("isinactive") == "T"}
        for r in rows
    ]
    return out, len(rows)


def get_item_meta(env, item_ids):
    if not item_ids:
        return {}
    meta = {}
    for chunk in chunks(sorted(set(item_ids)), 200):
        ids_csv = ",".join(str(i) for i in chunk)
        rows, err = try_suiteql(env, f"SELECT id, itemid, itemtype, displayname FROM item WHERE id IN ({ids_csv})")
        if err:
            rows, err2 = try_suiteql(env, f"SELECT id, itemid, itemtype FROM item WHERE id IN ({ids_csv})")
            if err2:
                raise SuiteQLError(err2)
            for r in rows:
                r["displayname"] = None
        for r in rows:
            meta[int(r["id"])] = {
                "sku": r.get("itemid"),
                "itemtype": r.get("itemtype"),
                "display_name": r.get("displayname") or r.get("itemid"),
            }
    return meta


def get_item_classes(env, item_ids):
    """item.class -> classification.name, chunked. Returns ({item_id: classname_or_None},
    note_or_None). On a join failure the whole batch returns empty (every id null) plus a
    note string; per-row nulls are otherwise just "no class assigned" (a real, common
    NetSuite state, not a failure)."""
    if not item_ids:
        return {}, None
    out = {}
    note = None
    for chunk in chunks(sorted(set(item_ids)), 200):
        ids_csv = ",".join(str(i) for i in chunk)
        rows, err = try_suiteql(env, f"""
            SELECT i.id AS id, c.name AS classname
            FROM item i LEFT JOIN classification c ON c.id = i.class
            WHERE i.id IN ({ids_csv})
        """)
        if err:
            note = f"item.class -> classification join failed, item_class left null: {err[:300]}"
            continue
        for r in rows:
            out[int(r["id"])] = r.get("classname")
    return out, note


def get_location_lookup(env):
    """{location_id: {name, country, state, city}}. Primary: one join to
    locationmainaddress (small table, ~51 rows, no GROUP BY -- not the
    inventoryitemlocations 500-on-GROUP-BY-JOIN trap). Fallback: pull location and
    locationmainaddress separately and merge client-side, per R19. Returns
    (lookup, row_count, note_or_None)."""
    rows, err = try_suiteql(env, """
        SELECT l.id AS id, l.name AS name, a.country AS country, a.state AS state, a.city AS city
        FROM location l LEFT JOIN locationmainaddress a ON a.nkey = l.mainaddress
    """)
    if not err:
        out = {
            int(r["id"]): {"name": r["name"], "country": r.get("country"), "state": r.get("state"), "city": r.get("city")}
            for r in rows
        }
        return out, len(rows), None

    loc_rows, err2 = try_suiteql(env, "SELECT id, name, mainaddress FROM location")
    if err2:
        raise SuiteQLError(err2)
    addr_rows, err3 = try_suiteql(env, "SELECT nkey, country, state, city FROM locationmainaddress")
    addr_by_nkey = {a["nkey"]: a for a in addr_rows} if not err3 else {}
    out = {}
    for l in loc_rows:
        a = addr_by_nkey.get(l.get("mainaddress"), {})
        out[int(l["id"])] = {"name": l["name"], "country": a.get("country"), "state": a.get("state"), "city": a.get("city")}
    note = f"location/locationmainaddress joined query failed ({err[:200]}), fell back to a client-side merge"
    if err3:
        note += f"; locationmainaddress fallback query also failed: {err3[:200]}"
    return out, len(loc_rows) + len(addr_rows if not err3 else []), note


# ---------------------------------------------------------------------------
# NetSuite: raw query builders (shapes proven in research/probes/01,03,04,07,09,10)
# ---------------------------------------------------------------------------

def income_by_channel_month(env, start, end):
    sql = f"""
        SELECT TO_CHAR(t.trandate,'YYYY-MM') AS ym, tl.cseg_appf_channel AS chan, SUM(ai.amount) AS amt,
               COUNT(DISTINCT t.id) AS ntxn
        FROM transactionaccountingline ai
        JOIN transactionline tl ON tl.transaction = ai.transaction AND tl.id = ai.transactionline
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype = 'Income'
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY TO_CHAR(t.trandate,'YYYY-MM'), tl.cseg_appf_channel
        ORDER BY TO_CHAR(t.trandate,'YYYY-MM'), tl.cseg_appf_channel
    """
    return suiteql(env, sql)


def cogs_by_channel_month(env, start, end):
    sql = f"""
        SELECT TO_CHAR(t.trandate,'YYYY-MM') AS ym, tl.cseg_appf_channel AS chan, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transactionline tl ON tl.transaction = ai.transaction AND tl.id = ai.transactionline
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype = 'COGS'
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY TO_CHAR(t.trandate,'YYYY-MM'), tl.cseg_appf_channel
        ORDER BY TO_CHAR(t.trandate,'YYYY-MM'), tl.cseg_appf_channel
    """
    return suiteql(env, sql)


def income_by_channel(env, start, end):
    sql = f"""
        SELECT tl.cseg_appf_channel AS chan, SUM(ai.amount) AS amt, COUNT(DISTINCT t.id) AS ntxn
        FROM transactionaccountingline ai
        JOIN transactionline tl ON tl.transaction = ai.transaction AND tl.id = ai.transactionline
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype = 'Income'
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY tl.cseg_appf_channel
    """
    return suiteql(env, sql)


def cogs_by_channel(env, start, end):
    sql = f"""
        SELECT tl.cseg_appf_channel AS chan, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transactionline tl ON tl.transaction = ai.transaction AND tl.id = ai.transactionline
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype = 'COGS'
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY tl.cseg_appf_channel
    """
    return suiteql(env, sql)


def income_total(env, start, end):
    sql = f"""
        SELECT SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting='T' AND a.accttype='Income'
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
    """
    # NetSuite trap (research 01a Trap 3, reproduced live in the cloud 2026-08-26 23:47 MT): a large
    # aggregate over transactionaccountingline can return an EMPTY result set with no error. An
    # empty result here is a query failure, never a genuine zero: retry, then raise so check (a)
    # fails loudly instead of comparing against a fabricated 0.0.
    last_err = None
    for attempt in range(3):
        try:
            rows = suiteql(env, sql)
        except Exception as e:  # noqa: BLE001
            last_err = e
            rows = []
        if rows and rows[0].get("amt") is not None:
            return -fnum(rows[0]["amt"])
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"income_total({start}..{end}) returned an empty aggregate 3 times (silent-empty NetSuite failure); last error: {last_err}")


def income_total_by_month(env, start, end):
    sql = f"""
        SELECT TO_CHAR(t.trandate,'YYYY-MM') AS ym, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting='T' AND a.accttype='Income'
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY TO_CHAR(t.trandate,'YYYY-MM')
    """
    last_err = None
    for attempt in range(3):
        try:
            rows = suiteql(env, sql)
        except Exception as e:  # noqa: BLE001
            last_err = e
            rows = []
        if rows:
            return {r["ym"]: -fnum(r["amt"]) for r in rows}
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"income_total_by_month({start}..{end}) returned an empty aggregate 3 times (silent-empty NetSuite failure); last error: {last_err}")


def region_income(env, channel_id, start, end):
    sql = f"""
        SELECT tl.cseg_appf_region AS reg, SUM(ai.amount) AS amt, COUNT(DISTINCT t.id) AS ntxn
        FROM transactionaccountingline ai
        JOIN transactionline tl ON tl.transaction=ai.transaction AND tl.id=ai.transactionline
        JOIN transaction t ON t.id=ai.transaction JOIN account a ON a.id=ai.account
        WHERE ai.posting='T' AND a.accttype='Income' AND tl.cseg_appf_channel={channel_id}
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY tl.cseg_appf_region
    """
    return suiteql(env, sql)


def sku_by_channel_query(env, start, end):
    """The one shared SKU query builder (BOM/Income-line rule) -- used by sku_sales (MTD,
    YTD) and by the inventory days_on_hand trailing-90-day units pull. Never duplicate this
    WHERE clause elsewhere."""
    sql = f"""
        SELECT tl.item AS itemid, tl.cseg_appf_channel AS chan, tl.itemtype AS itemtype,
               SUM(tl.quantity) AS qty, SUM(ai.amount) AS amt
        FROM transactionline tl
        JOIN transactionaccountingline ai ON ai.transaction = tl.transaction AND ai.transactionline = tl.id
        JOIN transaction t ON t.id = tl.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype = 'Income' AND tl.mainline = 'F'
          AND tl.itemtype IN ('InvtPart','Assembly','Kit','NonInvtPart')
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY tl.item, tl.cseg_appf_channel, tl.itemtype
    """
    return suiteql(env, sql)


def returns_query(env, ttype, start, end):
    sql = f"""
        SELECT tl.cseg_appf_channel AS chan, COUNT(DISTINCT t.id) AS ntxn, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transactionline tl ON tl.transaction=ai.transaction AND tl.id=ai.transactionline
        JOIN transaction t ON t.id=ai.transaction JOIN account a ON a.id=ai.account
        WHERE t.type='{ttype}' AND ai.posting='T' AND a.accttype='Income'
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY tl.cseg_appf_channel
    """
    return suiteql(env, sql)


def orders_by_channel_query(env, start, end):
    """Distinct posting CustInvc + CashSale transactions with Income lines, by channel --
    the order-count/AOV source (excludes CustCred/CashRfnd, which returns_by_channel owns)."""
    sql = f"""
        SELECT tl.cseg_appf_channel AS chan, COUNT(DISTINCT t.id) AS ntxn, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transactionline tl ON tl.transaction=ai.transaction AND tl.id=ai.transactionline
        JOIN transaction t ON t.id=ai.transaction JOIN account a ON a.id=ai.account
        WHERE ai.posting='T' AND a.accttype='Income' AND t.type IN ('CustInvc','CashSale')
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY tl.cseg_appf_channel
    """
    return suiteql(env, sql)


# ---------------------------------------------------------------------------
# NetSuite: section builders. Each returns (data, row_count_pulled).
# ---------------------------------------------------------------------------

def build_pnl_by_channel_month(env, D, channels):
    inc_rows = income_by_channel_month(env, D["trailing_start"], D["asof"])
    cogs_rows = cogs_by_channel_month(env, D["trailing_start"], D["asof"])
    py_inc_rows = income_by_channel_month(env, D["py_trailing_start"], D["py_asof"])
    py_cogs_rows = cogs_by_channel_month(env, D["py_trailing_start"], D["py_asof"])

    matrix = {}
    for r in inc_rows:
        key = (r["ym"], to_int_or_none(r.get("chan")))
        e = matrix.setdefault(key, {"revenue": 0.0, "cogs": 0.0, "ntxn": 0})
        e["revenue"] += -fnum(r["amt"])
        e["ntxn"] += int(r.get("ntxn") or 0)
    for r in cogs_rows:
        key = (r["ym"], to_int_or_none(r.get("chan")))
        e = matrix.setdefault(key, {"revenue": 0.0, "cogs": 0.0, "ntxn": 0})
        e["cogs"] += fnum(r["amt"])

    py_matrix = {}
    for r in py_inc_rows:
        key = (r["ym"], to_int_or_none(r.get("chan")))
        e = py_matrix.setdefault(key, {"revenue": 0.0, "cogs": 0.0})
        e["revenue"] += -fnum(r["amt"])
    for r in py_cogs_rows:
        key = (r["ym"], to_int_or_none(r.get("chan")))
        e = py_matrix.setdefault(key, {"revenue": 0.0, "cogs": 0.0})
        e["cogs"] += fnum(r["amt"])

    out = []
    chan_list = channels + [{"id": None, "name": "Unassigned"}]
    for ym in D["trailing_months"]:
        py_ym = f"{int(ym[:4]) - 1}-{ym[5:]}"
        for c in chan_list:
            e = matrix.get((ym, c["id"]), {"revenue": 0.0, "cogs": 0.0, "ntxn": 0})
            py_e = py_matrix.get((py_ym, c["id"]), {"revenue": 0.0, "cogs": 0.0})
            gp = e["revenue"] - e["cogs"]
            margin = (gp / e["revenue"] * 100) if e["revenue"] else None
            out.append({
                "ym": ym, "channel_id": c["id"], "channel": c["name"],
                "revenue": round(e["revenue"], 2), "cogs": round(e["cogs"], 2),
                "gross_profit": round(gp, 2),
                "margin_pct": round(margin, 2) if margin is not None else None,
                "ntxn": e["ntxn"],
                "revenue_py": round(py_e["revenue"], 2), "cogs_py": round(py_e["cogs"], 2),
            })
    return out, len(inc_rows) + len(cogs_rows) + len(py_inc_rows) + len(py_cogs_rows)


def build_pnl_by_channel_period(env, D, channels):
    windows = {
        "mtd": (D["mtd_start"], D["asof"]),
        "ytd": (D["ytd_start"], D["asof"]),
        "mtd_prior_year": (D["prior_mtd_start"], D["prior_mtd_end"]),
        "ytd_prior_year": (D["prior_ytd_start"], D["prior_ytd_end"]),
    }
    per_window = {}
    total_rows = 0
    for wname, (s, e) in windows.items():
        inc_rows = income_by_channel(env, s, e)
        cogs_rows = cogs_by_channel(env, s, e)
        total_rows += len(inc_rows) + len(cogs_rows)
        agg = {}
        for r in inc_rows:
            chan = to_int_or_none(r.get("chan"))
            agg.setdefault(chan, {"revenue": 0.0, "cogs": 0.0})
            agg[chan]["revenue"] += -fnum(r["amt"])
        for r in cogs_rows:
            chan = to_int_or_none(r.get("chan"))
            agg.setdefault(chan, {"revenue": 0.0, "cogs": 0.0})
            agg[chan]["cogs"] += fnum(r["amt"])
        per_window[wname] = agg

    chan_list = channels + [{"id": None, "name": "Unassigned"}]
    out = []
    totals = {wname: {"revenue": 0.0, "cogs": 0.0} for wname in windows}
    for c in chan_list:
        row = {"channel_id": c["id"], "channel": c["name"]}
        for wname in windows:
            e = per_window[wname].get(c["id"], {"revenue": 0.0, "cogs": 0.0})
            gp = e["revenue"] - e["cogs"]
            margin = (gp / e["revenue"] * 100) if e["revenue"] else None
            row[wname] = {
                "revenue": round(e["revenue"], 2), "cogs": round(e["cogs"], 2),
                "gp": round(gp, 2), "margin_pct": round(margin, 2) if margin is not None else None,
            }
            totals[wname]["revenue"] += e["revenue"]
            totals[wname]["cogs"] += e["cogs"]
        out.append(row)

    total_row = {"channel_id": "TOTAL", "channel": "TOTAL"}
    for wname in windows:
        rev, cogs = totals[wname]["revenue"], totals[wname]["cogs"]
        gp = rev - cogs
        margin = (gp / rev * 100) if rev else None
        total_row[wname] = {
            "revenue": round(rev, 2), "cogs": round(cogs, 2),
            "gp": round(gp, 2), "margin_pct": round(margin, 2) if margin is not None else None,
        }
    out.append(total_row)
    return out, total_rows


def _month_date_range(ym: str, asof_iso: str, is_current_month: bool) -> tuple[str, str]:
    y, mo = int(ym[:4]), int(ym[5:7])
    start = datetime.date(y, mo, 1)
    if is_current_month:
        end = datetime.date.fromisoformat(asof_iso)
    else:
        end = add_months(start, 1) - datetime.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _retry_window_pair(env, start: str, end: str) -> tuple[float, float]:
    """Re-runs BOTH sides of the channel-foot comparison for one window, back to back
    (channel-summed via a fresh income_by_channel() call, not the earlier pnl_month_rows,
    since THAT query is what may have run before a concurrent posting landed). Returns
    (channel_sum, unsegmented_total), both rounded to cents."""
    inc_rows = income_by_channel(env, start, end)
    cs = round(sum(-fnum(r["amt"]) for r in inc_rows), 2)
    us = round(income_total(env, start, end), 2)
    return cs, us


def build_self_check(env, D, pnl_month_rows):
    """E2(a)/checks.py check (a): channel-summed revenue (incl. Unassigned) vs. an
    independently-queried unsegmented Income total, for MTD, YTD, and each of 13 months.

    Retry-on-race (2026-08-26, team-lead fix): the channel-summed side comes from
    pnl_month_rows, queried earlier in the pipeline; the unsegmented side is queried here,
    separately, possibly minutes later. On a live, continuously-transacting production
    account, a new posting dated inside an affected window can land in that gap -- both
    queries are individually correct, they just observed different instants. Confirmed live
    2026-08-26: a same-run MTD/YTD/current-month diff of exactly -359.55 in one run
    reproduced as an exact 0.0 tie when both sides were re-queried moments later (see
    spike/README.md). So: any window (MTD, YTD, or an individual month) whose initial diff
    exceeds $0.01 gets BOTH sides re-run once, back to back, with fresh queries (never reused
    from pnl_month_rows); a second-pair diff within $0.01 overwrites the window's numbers and
    counts as a pass with the retry logged; a second-pair diff still over $0.01 is a real
    failure, not a race."""
    if pnl_month_rows is None:
        raise RuntimeError("pnl_by_channel_month section failed; cannot self-check against it")

    chan_sum_by_month = {}
    for row in pnl_month_rows:
        chan_sum_by_month[row["ym"]] = chan_sum_by_month.get(row["ym"], 0.0) + row["revenue"]

    unseg_by_month = income_total_by_month(env, D["trailing_start"], D["asof"])
    n_rows = len(unseg_by_month)

    months_check = []
    for ym in D["trailing_months"]:
        cs = round(chan_sum_by_month.get(ym, 0.0), 2)
        us = round(unseg_by_month.get(ym, 0.0), 2)
        months_check.append({"ym": ym, "channel_sum": cs, "unsegmented_total": us, "diff": round(cs - us, 2)})

    cur_month = D["trailing_months"][-1]
    mtd_channel_sum = chan_sum_by_month.get(cur_month, 0.0)
    mtd_unsegmented = income_total(env, D["mtd_start"], D["asof"])
    n_rows += 1

    ytd_prefix = f"{D['asof_date'].year:04d}-"
    ytd_months = [m for m in D["trailing_months"] if m.startswith(ytd_prefix)]
    ytd_channel_sum = sum(chan_sum_by_month.get(m, 0.0) for m in ytd_months)
    ytd_unsegmented = income_total(env, D["ytd_start"], D["asof"])
    n_rows += 1

    result = {
        "mtd_channel_sum_incl_unassigned": round(mtd_channel_sum, 2),
        "mtd_unsegmented_total": round(mtd_unsegmented, 2),
        "diff": round(mtd_channel_sum - mtd_unsegmented, 2),
        "ytd_channel_sum_incl_unassigned": round(ytd_channel_sum, 2),
        "ytd_unsegmented_total": round(ytd_unsegmented, 2),
        "ytd_diff": round(ytd_channel_sum - ytd_unsegmented, 2),
        "months": months_check,
    }

    retry_notes = []
    if abs(result["diff"]) > 0.01:
        cs2, us2 = _retry_window_pair(env, D["mtd_start"], D["asof"])
        n_rows += 1
        diff2 = round(cs2 - us2, 2)
        tied = abs(diff2) <= 0.01
        retry_notes.append(f"MTD: re-ran after concurrent posting: first diff {result['diff']}, second diff {diff2}" + ("" if tied else " -- still differs, not a race"))
        if tied:
            result["mtd_channel_sum_incl_unassigned"], result["mtd_unsegmented_total"], result["diff"] = cs2, us2, diff2

    if abs(result["ytd_diff"]) > 0.01:
        cs2, us2 = _retry_window_pair(env, D["ytd_start"], D["asof"])
        n_rows += 1
        diff2 = round(cs2 - us2, 2)
        tied = abs(diff2) <= 0.01
        retry_notes.append(f"YTD: re-ran after concurrent posting: first diff {result['ytd_diff']}, second diff {diff2}" + ("" if tied else " -- still differs, not a race"))
        if tied:
            result["ytd_channel_sum_incl_unassigned"], result["ytd_unsegmented_total"], result["ytd_diff"] = cs2, us2, diff2

    for m in result["months"]:
        if abs(m["diff"]) > 0.01:
            start, end = _month_date_range(m["ym"], D["asof"], m["ym"] == cur_month)
            cs2, us2 = _retry_window_pair(env, start, end)
            n_rows += 1
            diff2 = round(cs2 - us2, 2)
            tied = abs(diff2) <= 0.01
            retry_notes.append(f"{m['ym']}: re-ran after concurrent posting: first diff {m['diff']}, second diff {diff2}" + ("" if tied else " -- still differs, not a race"))
            if tied:
                m["channel_sum"], m["unsegmented_total"], m["diff"] = cs2, us2, diff2

    all_diffs = [abs(result["diff"]), abs(result["ytd_diff"])] + [abs(m["diff"]) for m in result["months"]]
    result["pass"] = all(d <= 0.01 for d in all_diffs)
    result["retried"] = bool(retry_notes)
    result["retry_detail"] = "; ".join(retry_notes) if retry_notes else None
    return result, n_rows


def build_dtc_by_region(env, D, regions, dtc_channel_id, us_region_ids, dq_region_ids):
    mtd_rows = region_income(env, dtc_channel_id, D["mtd_start"], D["asof"])
    ytd_rows = region_income(env, dtc_channel_id, D["ytd_start"], D["asof"])
    trailing_rows = region_income(env, dtc_channel_id, D["trailing_start"], D["asof"])
    total_rows = len(mtd_rows) + len(ytd_rows) + len(trailing_rows)

    region_name = {r["id"]: r["name"] for r in regions}

    def to_map(rows):
        m = {}
        for r in rows:
            rid = to_int_or_none(r.get("reg"))
            m[rid] = m.get(rid, 0.0) + (-fnum(r["amt"]))
        return m

    mtd_m, ytd_m, tr_m = to_map(mtd_rows), to_map(ytd_rows), to_map(trailing_rows)
    all_region_ids = set(mtd_m) | set(ytd_m) | set(tr_m) | {r["id"] for r in regions}

    us_ids = set(us_region_ids)
    dq_ids = set(dq_region_ids)

    by_region = []
    data_quality = []
    for rid in sorted(all_region_ids, key=lambda x: (x is None, x)):
        name = region_name.get(rid, "NULL") if rid is not None else "NULL"
        row = {
            "region_id": rid, "region": name,
            "mtd": round(mtd_m.get(rid, 0.0), 2), "ytd": round(ytd_m.get(rid, 0.0), 2),
            "trailing13": round(tr_m.get(rid, 0.0), 2),
        }
        by_region.append(row)
        if rid in dq_ids and (row["mtd"] or row["ytd"] or row["trailing13"]):
            data_quality.append(row)

    def us_other(m):
        us = sum(v for k, v in m.items() if k in us_ids)
        other = sum(v for k, v in m.items() if k not in us_ids and k not in dq_ids)
        return us, other

    us_mtd, other_mtd = us_other(mtd_m)
    us_ytd, other_ytd = us_other(ytd_m)
    us_tr, other_tr = us_other(tr_m)

    result = {
        "by_region": by_region,
        "rollup": {
            "US": {"mtd": round(us_mtd, 2), "ytd": round(us_ytd, 2), "trailing13": round(us_tr, 2)},
            "Other": {"mtd": round(other_mtd, 2), "ytd": round(other_ytd, 2), "trailing13": round(other_tr, 2)},
        },
        "data_quality": data_quality,
    }
    return result, total_rows


def build_sku_sales(env, D, channels):
    mtd_rows = sku_by_channel_query(env, D["mtd_start"], D["asof"])
    ytd_rows = sku_by_channel_query(env, D["ytd_start"], D["asof"])
    total_rows = len(mtd_rows) + len(ytd_rows)

    item_ids = {int(r["itemid"]) for r in mtd_rows + ytd_rows if r.get("itemid") is not None}
    item_meta = get_item_meta(env, item_ids)
    chan_name = {c["id"]: c["name"] for c in channels}

    def to_list(rows):
        out = []
        for r in rows:
            amt = -fnum(r["amt"])
            if amt == 0:
                continue
            iid = to_int_or_none(r.get("itemid"))
            meta = item_meta.get(iid, {})
            chan = to_int_or_none(r.get("chan"))
            out.append({
                "channel_id": chan,
                "channel": chan_name.get(chan, "Unassigned") if chan is not None else "Unassigned",
                "sku": meta.get("sku"),
                "item_id": iid,
                "itemtype": r.get("itemtype"),
                "display_name": meta.get("display_name"),
                # tl.quantity follows the same credit-negative convention as ai.amount
                # (a sale line carries qty=-1, a CustCred/return line carries qty=+1) --
                # confirmed live against S-CM-002 Income-joined lines. Flip for a natural
                # "net units sold" figure, same as the revenue flip above.
                "units": round(-fnum(r.get("qty")), 2),
                "revenue": round(amt, 2),
            })
        out.sort(key=lambda x: (x["channel"] or "", -x["revenue"]))
        return out

    mtd_list = to_list(mtd_rows)
    ytd_list = to_list(ytd_rows)

    def top5_concentration(lst, period):
        by_chan = {}
        for r in lst:
            by_chan.setdefault(r["channel"], []).append(r)
        out = []
        for chan, items in by_chan.items():
            items_sorted = sorted(items, key=lambda x: -x["revenue"])
            chan_total = sum(x["revenue"] for x in items_sorted)
            top5 = sum(x["revenue"] for x in items_sorted[:5])
            share = (top5 / chan_total * 100) if chan_total else None
            out.append({
                "channel": chan, "period": period,
                "top5_share_pct": round(share, 2) if share is not None else None,
                "top5_revenue": round(top5, 2), "channel_revenue": round(chan_total, 2),
            })
        return out

    top5 = top5_concentration(mtd_list, "mtd") + top5_concentration(ytd_list, "ytd")
    result = {"mtd": mtd_list, "ytd": ytd_list, "top5_concentration": top5}
    return result, total_rows


def build_inventory(env, D, sku_sales_result, dtc_channel_id, amazon_channel_id):
    if sku_sales_result is None:
        raise RuntimeError("sku_sales section failed; cannot build inventory drilldowns")

    ytd, mtd = sku_sales_result["ytd"], sku_sales_result["mtd"]
    total_rows_pulled = 0
    notes = []

    combined = {}
    for r in ytd:
        if r["item_id"] is None:
            continue
        e = combined.setdefault(r["item_id"], {"sku": r["sku"], "itemtype": r["itemtype"], "revenue": 0.0})
        e["revenue"] += r["revenue"]
    top25_ids = [iid for iid, _ in sorted(combined.items(), key=lambda kv: -kv[1]["revenue"])[:25]]
    top25_id_set = set(top25_ids)

    drill_ids = {
        r["item_id"] for r in mtd
        if r["item_id"] is not None and r["channel_id"] in (dtc_channel_id, amazon_channel_id)
    }
    target_ids = top25_id_set | drill_ids

    itemtype_by_id = {iid: e["itemtype"] for iid, e in combined.items()}
    sku_by_id = {iid: e["sku"] for iid, e in combined.items()}
    for r in mtd:
        if r["item_id"] is not None:
            itemtype_by_id.setdefault(r["item_id"], r["itemtype"])
            sku_by_id.setdefault(r["item_id"], r["sku"])

    invtpart_ids = sorted(i for i in target_ids if itemtype_by_id.get(i) == "InvtPart")
    assembly_ids = sorted(i for i in target_ids if itemtype_by_id.get(i) == "Assembly")

    loc_lookup, loc_rows_count, loc_note = get_location_lookup(env)
    total_rows_pulled += loc_rows_count
    if loc_note:
        notes.append(loc_note)

    item_class_by_id, class_note = get_item_classes(env, invtpart_ids + assembly_ids)
    if class_note:
        notes.append(class_note)

    onhand = []
    for chunk in chunks(invtpart_ids, 200):
        ids_csv = ",".join(str(i) for i in chunk)
        rows = suiteql(env, f"SELECT item, location, quantityonhand, quantityavailable, onhandvaluemli "
                             f"FROM inventoryitemlocations WHERE item IN ({ids_csv})")
        total_rows_pulled += len(rows)
        for r in rows:
            iid = int(r["item"])
            loc_id = to_int_or_none(r.get("location"))
            loc = loc_lookup.get(loc_id, {})
            onhand.append({
                "sku": sku_by_id.get(iid), "item_id": iid, "itemtype": "InvtPart",
                "item_class": item_class_by_id.get(iid),
                "location_id": loc_id, "location": loc.get("name"),
                "location_country": loc.get("country"), "location_state": loc.get("state"),
                "location_city": loc.get("city"),
                "onhand": fnum(r.get("quantityonhand")),
                "available": fnum(r.get("quantityavailable")),
                "onhand_value": fnum(r.get("onhandvaluemli")),
            })

    for chunk in chunks(assembly_ids, 200):
        ids_csv = ",".join(str(i) for i in chunk)
        rows = suiteql(env, f"SELECT item, location, quantityonhand, quantityavailable, onhandvaluemli "
                             f"FROM aggregateitemlocation WHERE item IN ({ids_csv})")
        total_rows_pulled += len(rows)
        for r in rows:
            iid = int(r["item"])
            loc_id = to_int_or_none(r.get("location"))
            loc = loc_lookup.get(loc_id, {})
            onhand.append({
                "sku": sku_by_id.get(iid), "item_id": iid, "itemtype": "Assembly",
                "item_class": item_class_by_id.get(iid),
                "location_id": loc_id, "location": loc.get("name"),
                "location_country": loc.get("country"), "location_state": loc.get("state"),
                "location_city": loc.get("city"),
                "onhand": fnum(r.get("quantityonhand")),
                "available": fnum(r.get("quantityavailable")),
                "onhand_value": fnum(r.get("onhandvaluemli")),
            })

    # Kit-type SKUs carry zero on-hand rows anywhere in this NS instance (structural,
    # confirmed universal in research Finding 5/6) -- list every active Kit SKU, not
    # just ones in the drill/top-25 set, since the gap is total.
    kit_rows, err = try_suiteql(env, "SELECT itemid FROM item WHERE isinactive='F' AND itemtype='Kit' ORDER BY itemid")
    if err:
        raise SuiteQLError(err)
    total_rows_pulled += len(kit_rows)
    kit_skus_without_onhand = sorted({r["itemid"] for r in kit_rows if r.get("itemid")})

    # OOS check across the full active InvtPart universe (research Finding 5 pattern).
    items, err = try_suiteql(env, "SELECT id, itemid FROM item WHERE isinactive='F' AND itemtype='InvtPart'")
    if err:
        raise SuiteQLError(err)
    total_rows_pulled += len(items)
    item_ids_all = [int(i["id"]) for i in items]
    item_sku_all = {int(i["id"]): i["itemid"] for i in items}

    loc_rows_by_item = {}
    for chunk in chunks(item_ids_all, 200):
        ids_csv = ",".join(str(i) for i in chunk)
        rows = suiteql(env, f"SELECT item, quantityonhand FROM inventoryitemlocations WHERE item IN ({ids_csv})")
        total_rows_pulled += len(rows)
        for r in rows:
            loc_rows_by_item.setdefault(int(r["item"]), []).append(fnum(r.get("quantityonhand")))

    oos_skus = []
    for iid in item_ids_all:
        qohs = loc_rows_by_item.get(iid)
        if not qohs or all(q <= 0 for q in qohs):
            oos_skus.append(item_sku_all[iid])

    rows, err = try_suiteql(env, "SELECT SUM(onhandvaluemli) AS totval FROM inventoryitemlocations")
    invtpart_val = fnum(rows[0]["totval"]) if not err and rows else 0.0
    rows, err = try_suiteql(
        env, "SELECT SUM(onhandvaluemli) AS totval FROM aggregateitemlocation al "
             "JOIN item i ON i.id=al.item WHERE i.itemtype='Assembly'"
    )
    assembly_val = fnum(rows[0]["totval"]) if not err and rows else 0.0
    total_value_locations = invtpart_val + assembly_val

    rows, err = try_suiteql(
        env, "SELECT SUM(totalvalue) AS totval FROM item WHERE isinactive='F' "
             "AND itemtype IN ('InvtPart','Assembly','Kit')"
    )
    total_value_item_header = fnum(rows[0]["totval"]) if not err and rows else 0.0
    total_rows_pulled += 3

    # days_on_hand: top-25 YTD sellers, trailing-90-day units via the SAME shared SKU
    # query builder (sku_by_channel_query) used by sku_sales -- no separate BOM rule.
    units90_rows = sku_by_channel_query(env, D["trailing90_start"], D["asof"])
    total_rows_pulled += len(units90_rows)
    units90_by_item = {}
    for r in units90_rows:
        iid = to_int_or_none(r.get("itemid"))
        if iid is None:
            continue
        units90_by_item[iid] = units90_by_item.get(iid, 0.0) + (-fnum(r.get("qty")))

    onhand_total_by_item = {}
    for row in onhand:
        iid = row["item_id"]
        onhand_total_by_item[iid] = onhand_total_by_item.get(iid, 0.0) + (row["onhand"] or 0.0)

    days_on_hand = []
    for iid in sorted(top25_ids, key=lambda i: -combined[i]["revenue"]):
        itype = itemtype_by_id.get(iid)
        sku = sku_by_id.get(iid)
        units_90d = round(units90_by_item.get(iid, 0.0), 2)
        avg_daily = round(units_90d / 90.0, 4)
        if itype == "Kit":
            days_on_hand.append({
                "sku": sku, "item_id": iid, "itemtype": itype,
                "onhand_total": None, "units_90d": units_90d,
                "avg_daily_units": avg_daily, "days_on_hand": None,
            })
            continue
        onhand_total = onhand_total_by_item.get(iid)
        doh = round(onhand_total / avg_daily, 1) if (onhand_total is not None and avg_daily > 0) else None
        days_on_hand.append({
            "sku": sku, "item_id": iid, "itemtype": itype,
            "onhand_total": round(onhand_total, 2) if onhand_total is not None else None,
            "units_90d": units_90d, "avg_daily_units": avg_daily, "days_on_hand": doh,
        })

    result = {
        "onhand_by_item_location": onhand,
        "oos": {
            "active_invtpart": len(item_ids_all),
            "oos_everywhere": len(oos_skus),
            "sample": oos_skus[:50],
        },
        "total_value_locations": round(total_value_locations, 2),
        "total_value_item_header": round(total_value_item_header, 2),
        "tie_out_diff": round(total_value_locations - total_value_item_header, 2),
        "kit_skus_without_onhand": kit_skus_without_onhand,
        "days_on_hand": days_on_hand,
        "notes": notes,
    }
    return result, total_rows_pulled


def build_life_to_date(env):
    inc_rows = suiteql(env, """
        SELECT TO_CHAR(t.trandate,'YYYY') AS yr, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai JOIN transaction t ON t.id = ai.transaction JOIN account a ON a.id = ai.account
        WHERE ai.posting='T' AND a.accttype='Income'
        GROUP BY TO_CHAR(t.trandate,'YYYY') ORDER BY TO_CHAR(t.trandate,'YYYY')
    """)
    cogs_rows = suiteql(env, """
        SELECT TO_CHAR(t.trandate,'YYYY') AS yr, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai JOIN transaction t ON t.id = ai.transaction JOIN account a ON a.id = ai.account
        WHERE ai.posting='T' AND a.accttype='COGS'
        GROUP BY TO_CHAR(t.trandate,'YYYY') ORDER BY TO_CHAR(t.trandate,'YYYY')
    """)
    first_rows = suiteql(env, """
        SELECT MIN(t.trandate) AS mn
        FROM transactionaccountingline ai JOIN transaction t ON t.id = ai.transaction JOIN account a ON a.id = ai.account
        WHERE ai.posting='T' AND a.accttype='Income'
    """)

    income_by_year = {r["yr"]: round(-fnum(r["amt"]), 2) for r in inc_rows}
    cogs_by_year = {r["yr"]: round(fnum(r["amt"]), 2) for r in cogs_rows}
    income_total_ = round(sum(income_by_year.values()), 2)
    cogs_total_ = round(sum(cogs_by_year.values()), 2)
    gp = round(income_total_ - cogs_total_, 2)
    margin = (gp / income_total_ * 100) if income_total_ else None
    first_income_date = normalize_ns_date(first_rows[0]["mn"]) if first_rows else None

    result = {
        "income_by_year": income_by_year,
        "cogs_by_year": cogs_by_year,
        "income_total": income_total_,
        "cogs_total": cogs_total_,
        "gross_profit": gp,
        "margin_pct": round(margin, 2) if margin is not None else None,
        "first_income_date": first_income_date,
    }
    return result, len(inc_rows) + len(cogs_rows) + len(first_rows)


def build_returns_by_channel(env, D, channels, revenue_lookup):
    chan_name = {c["id"]: c["name"] for c in channels}
    total_rows = 0

    def agg(start, end):
        nonlocal total_rows
        m = {}
        for ttype in ("CustCred", "CashRfnd"):
            rows = returns_query(env, ttype, start, end)
            total_rows += len(rows)
            for r in rows:
                chan = to_int_or_none(r.get("chan"))
                e = m.setdefault(chan, {"amt": 0.0, "n": 0})
                e["amt"] += -fnum(r["amt"])
                e["n"] += int(r.get("ntxn") or 0)
        return m

    mtd_m = agg(D["mtd_start"], D["asof"])
    ytd_m = agg(D["ytd_start"], D["asof"])

    all_ids = set(mtd_m) | set(ytd_m) | {c["id"] for c in channels}
    out = []
    for cid in sorted(all_ids, key=lambda x: (x is None, x)):
        name = chan_name.get(cid, "Unassigned") if cid is not None else "Unassigned"
        mtd_e = mtd_m.get(cid, {"amt": 0.0, "n": 0})
        ytd_e = ytd_m.get(cid, {"amt": 0.0, "n": 0})
        mtd_rev = revenue_lookup.get((cid, "mtd"), 0.0)
        ytd_rev = revenue_lookup.get((cid, "ytd"), 0.0)
        mtd_rate = round(-mtd_e["amt"] / mtd_rev * 100, 2) if mtd_rev else None
        ytd_rate = round(-ytd_e["amt"] / ytd_rev * 100, 2) if ytd_rev else None
        out.append({
            "channel_id": cid, "channel": name,
            "mtd_credits": round(mtd_e["amt"], 2), "mtd_credit_count": mtd_e["n"],
            "ytd_credits": round(ytd_e["amt"], 2), "ytd_credit_count": ytd_e["n"],
            "mtd_return_rate_pct": mtd_rate, "ytd_return_rate_pct": ytd_rate,
        })
    return out, total_rows


def build_orders_by_channel(env, D, channels, amazon_channel_id):
    mtd_rows = orders_by_channel_query(env, D["mtd_start"], D["asof"])
    ytd_rows = orders_by_channel_query(env, D["ytd_start"], D["asof"])
    total_rows = len(mtd_rows) + len(ytd_rows)
    chan_name = {c["id"]: c["name"] for c in channels}

    def to_map(rows):
        m = {}
        for r in rows:
            cid = to_int_or_none(r.get("chan"))
            m[cid] = {"ntxn": int(r.get("ntxn") or 0), "revenue": -fnum(r["amt"])}
        return m

    mtd_m, ytd_m = to_map(mtd_rows), to_map(ytd_rows)
    all_ids = set(mtd_m) | set(ytd_m) | {c["id"] for c in channels}
    out = []
    for cid in sorted(all_ids, key=lambda x: (x is None, x)):
        name = chan_name.get(cid, "Unassigned") if cid is not None else "Unassigned"
        if cid == amazon_channel_id:
            out.append({
                "channel_id": cid, "channel": name,
                "mtd_orders": None, "ytd_orders": None, "mtd_aov": None, "ytd_aov": None,
                "note": "consolidated invoices since 2026-08-20, no order grain in NetSuite; see amazon_orders",
            })
            continue
        mtd_e = mtd_m.get(cid, {"ntxn": 0, "revenue": 0.0})
        ytd_e = ytd_m.get(cid, {"ntxn": 0, "revenue": 0.0})
        mtd_aov = round(mtd_e["revenue"] / mtd_e["ntxn"], 2) if mtd_e["ntxn"] else None
        ytd_aov = round(ytd_e["revenue"] / ytd_e["ntxn"], 2) if ytd_e["ntxn"] else None
        out.append({
            "channel_id": cid, "channel": name,
            "mtd_orders": mtd_e["ntxn"], "ytd_orders": ytd_e["ntxn"],
            "mtd_aov": mtd_aov, "ytd_aov": ytd_aov, "note": None,
        })
    return out, total_rows


def build_picklist_snapshot(channels, regions):
    result = {
        "channels": {str(c["id"]): c["name"] for c in channels},
        "regions": {str(r["id"]): r["name"] for r in regions},
    }
    return result, len(channels) + len(regions)


def build_t5_sentinels(env, D, sku_sales_result):
    """T5 (PRD Section 7), redefined 2026-08-26: the original formulation ("P-RIM-003-BLA
    and A-STICKER-001 absent from sku_sales") was disproven live -- P-RIM-003-BLA
    ("Replacement Rim - 3.0 - Black") turns out to be a genuinely, independently sold spare
    part (real Income postings across Spikeball.com/Major Retail/PE/Rec), so "absent" was
    never a valid invariant for that SKU. What T5 actually needs to prove is the METHOD: for
    each sentinel SKU, the Income-joined sku_sales units are DOMINATED by component
    consumption relative to every line touching that item (naive_qty, no Income-line
    filter), and (when a real list price exists) the realized revenue-per-unit is in a
    plausible range of that price -- not exact equality, since sale channel/discount/bundle
    pricing varies.

    naive_qty_ytd_all_lines: SUM(tl.quantity) over every transactionline for the item
    (mainline='F', ANY account/posting status), YTD window -- this INCLUDES the BOM
    component-consumption lines with zero GL impact that the Income-join deliberately
    excludes, so it is always >= the Income-joined figure in magnitude for a SKU that is
    mostly a component.

    list_price_reference: the largest non-zero `pricing.unitprice` found for the item across
    every price level (in the item's home currency) -- NetSuite's "Base Price" level (id 1)
    is $0 for both sentinels in this account, so a single fixed price level is not usable;
    the max non-zero level found live was "Spikeball Store" $6.49 for P-RIM-003-BLA,
    A-STICKER-001 had $0 at every level (no price ever set -- itself further evidence it is
    a pure component, never independently listed for sale). `None` when no level has any
    nonzero price; checks.py skips the price-consistency sub-check in that case rather than
    dividing by a meaningless $0."""
    if sku_sales_result is None:
        raise RuntimeError("sku_sales section failed; cannot build t5_sentinels")

    skus_csv = ",".join(f"'{s}'" for s in T5_SENTINEL_SKUS)
    item_rows, err = try_suiteql(env, f"SELECT id, itemid FROM item WHERE itemid IN ({skus_csv})")
    if err:
        raise SuiteQLError(err)
    item_id_by_sku = {r["itemid"]: int(r["id"]) for r in item_rows}
    total_rows = len(item_rows)

    result = {}
    if not item_id_by_sku:
        return result, total_rows

    ids_csv = ",".join(str(i) for i in item_id_by_sku.values())

    naive_rows, err = try_suiteql(env, f"""
        SELECT tl.item AS itemid, SUM(tl.quantity) AS qty
        FROM transactionline tl
        JOIN transaction t ON t.id = tl.transaction
        WHERE tl.mainline='F' AND tl.item IN ({ids_csv})
          AND t.trandate >= TO_DATE('{D['ytd_start']}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{D['asof']}','YYYY-MM-DD')
        GROUP BY tl.item
    """)
    if err:
        raise SuiteQLError(err)
    total_rows += len(naive_rows)
    naive_qty_by_item = {int(r["itemid"]): fnum(r.get("qty")) for r in naive_rows}

    price_rows, price_err = try_suiteql(env, f"SELECT item, unitprice FROM pricing WHERE item IN ({ids_csv}) AND currency=1")
    list_price_by_item = {}
    if not price_err:
        total_rows += len(price_rows)
        for r in price_rows:
            iid = int(r["item"])
            up = fnum(r.get("unitprice"))
            if up > 0:
                list_price_by_item[iid] = max(list_price_by_item.get(iid, 0.0), up)

    ytd_rows = sku_sales_result.get("ytd") or []
    sku_agg = {}
    for r in ytd_rows:
        if r.get("sku") in T5_SENTINEL_SKUS:
            e = sku_agg.setdefault(r["sku"], {"units": 0.0, "revenue": 0.0})
            e["units"] += r.get("units", 0.0) or 0.0
            e["revenue"] += r.get("revenue", 0.0) or 0.0

    for sku in T5_SENTINEL_SKUS:
        iid = item_id_by_sku.get(sku)
        agg = sku_agg.get(sku, {"units": 0.0, "revenue": 0.0})
        naive_qty = naive_qty_by_item.get(iid) if iid is not None else None
        units = agg["units"]
        avg_price = round(agg["revenue"] / units, 4) if units else None
        result[sku] = {
            "item_id": iid,
            "naive_qty_ytd_all_lines": round(abs(naive_qty), 2) if naive_qty is not None else None,
            "sku_sales_units_ytd": round(units, 2),
            "sku_sales_revenue_ytd": round(agg["revenue"], 2),
            "list_price_reference": round(list_price_by_item[iid], 2) if iid in list_price_by_item else None,
            "avg_realized_price": avg_price,
        }
    return result, total_rows


# ---------------------------------------------------------------------------
# E3 roll-up layer: applies spike/config/rollups.json. Configuration changes when the
# owner rules; no code change (PRD Section 5, E3).
# ---------------------------------------------------------------------------

def _window_math(rev, cogs):
    gp = rev - cogs
    margin = (gp / rev * 100) if rev else None
    return {"revenue": round(rev, 2), "cogs": round(cogs, 2), "gp": round(gp, 2),
            "margin_pct": round(margin, 2) if margin is not None else None}


def _yoy_pct(cur, py):
    if py["revenue"]:
        return round((cur["revenue"] - py["revenue"]) / abs(py["revenue"]) * 100, 2)
    return None


def build_rollup_by_period(pnl_period_rows, rollups):
    by_id = {}
    total_row = None
    for row in pnl_period_rows:
        if row["channel_id"] == "TOTAL":
            total_row = row
        else:
            by_id[row["channel_id"]] = row

    if total_row is None:
        raise RuntimeError("pnl_by_channel_period missing its TOTAL row; cannot build rollup_by_period")

    def sum_window(channel_ids, wname):
        rev = cogs = 0.0
        for cid in channel_ids:
            r = by_id.get(cid)
            if r:
                rev += r[wname]["revenue"]
                cogs += r[wname]["cogs"]
        return _window_math(rev, cogs)

    out = []
    for g in rollups["groups"]:
        ids = g["channel_ids"]
        windows = {w: sum_window(ids, w) for w in ("mtd", "ytd", "mtd_prior_year", "ytd_prior_year")}
        out.append({
            "key": g["key"], "label": g["label"], "channel_ids": ids,
            "mtd": windows["mtd"], "ytd": windows["ytd"],
            "mtd_prior_year": windows["mtd_prior_year"], "ytd_prior_year": windows["ytd_prior_year"],
            "yoy_mtd_pct": _yoy_pct(windows["mtd"], windows["mtd_prior_year"]),
            "yoy_ytd_pct": _yoy_pct(windows["ytd"], windows["ytd_prior_year"]),
            "show_margin": g.get("show_margin", True), "margin_note": g.get("margin_note"),
        })

    u = rollups["unassigned"]
    r = by_id.get(None)
    zero = {"revenue": 0.0, "cogs": 0.0, "gp": 0.0, "margin_pct": None}
    u_windows = {
        w: (r[w] if r else zero) for w in ("mtd", "ytd", "mtd_prior_year", "ytd_prior_year")
    }
    out.append({
        "key": u["key"], "label": u["label"], "channel_ids": [None],
        "mtd": u_windows["mtd"], "ytd": u_windows["ytd"],
        "mtd_prior_year": u_windows["mtd_prior_year"], "ytd_prior_year": u_windows["ytd_prior_year"],
        "yoy_mtd_pct": _yoy_pct(u_windows["mtd"], u_windows["mtd_prior_year"]),
        "yoy_ytd_pct": _yoy_pct(u_windows["ytd"], u_windows["ytd_prior_year"]),
        "show_margin": True, "margin_note": None,
    })

    out.append({
        "key": "total", "label": "Total", "channel_ids": None,
        "mtd": total_row["mtd"], "ytd": total_row["ytd"],
        "mtd_prior_year": total_row["mtd_prior_year"], "ytd_prior_year": total_row["ytd_prior_year"],
        "yoy_mtd_pct": _yoy_pct(total_row["mtd"], total_row["mtd_prior_year"]),
        "yoy_ytd_pct": _yoy_pct(total_row["ytd"], total_row["ytd_prior_year"]),
        "show_margin": True, "margin_note": None,
    })

    # Roll-up totals must equal the TOTAL row of pnl_by_channel_period to the cent.
    for wname in ("mtd", "ytd", "mtd_prior_year", "ytd_prior_year"):
        summed_rev = round(sum(r[wname]["revenue"] for r in out[:-1]), 2)
        summed_cogs = round(sum(r[wname]["cogs"] for r in out[:-1]), 2)
        if abs(summed_rev - total_row[wname]["revenue"]) > 0.01:
            raise RuntimeError(
                f"rollup_by_period {wname} revenue {summed_rev} != pnl_by_channel_period TOTAL "
                f"{total_row[wname]['revenue']} -- a channel id is missing from rollups.json groups"
            )
        if abs(summed_cogs - total_row[wname]["cogs"]) > 0.01:
            raise RuntimeError(
                f"rollup_by_period {wname} cogs {summed_cogs} != pnl_by_channel_period TOTAL "
                f"{total_row[wname]['cogs']} -- a channel id is missing from rollups.json groups"
            )

    return out, len(out)


def build_rollup_by_month(pnl_month_rows, rollups):
    idx = {(row["ym"], row["channel_id"]): row for row in pnl_month_rows}
    yms = sorted({row["ym"] for row in pnl_month_rows})
    groups = list(rollups["groups"]) + [
        {"key": rollups["unassigned"]["key"], "label": rollups["unassigned"]["label"], "channel_ids": [None]}
    ]

    out = []
    for ym in yms:
        for g in groups:
            rev = cogs = rev_py = 0.0
            for cid in g["channel_ids"]:
                r = idx.get((ym, cid))
                if r:
                    rev += r["revenue"]
                    cogs += r["cogs"]
                    rev_py += r.get("revenue_py") or 0.0
            gp = rev - cogs
            margin = (gp / rev * 100) if rev else None
            out.append({
                "ym": ym, "key": g["key"], "label": g["label"],
                "revenue": round(rev, 2), "cogs": round(cogs, 2), "gp": round(gp, 2),
                "margin_pct": round(margin, 2) if margin is not None else None,
                "revenue_py": round(rev_py, 2),
            })
    return out, len(out)


def build_write_state(output: dict) -> dict:
    """The small prior-run state shape (spike/CONTRACT.md 'Prior-run state'), plus one
    additive `sections` block (row counts) so checks.py's check (c) has something to
    compare against. Written by `--write-state PATH`, read back by `--prev-state PATH`."""
    trailing = output["meta"]["trailing_months"]
    closed = trailing[:-1] if len(trailing) > 1 else []
    totals = {}
    for row in output.get("pnl_by_channel_month", []) or []:
        ym = row["ym"]
        e = totals.setdefault(ym, {"revenue": 0.0, "cogs": 0.0, "ntxn": 0})
        e["revenue"] += row.get("revenue", 0.0) or 0.0
        e["cogs"] += row.get("cogs", 0.0) or 0.0
        e["ntxn"] += row.get("ntxn", 0) or 0

    closed_months = {}
    for ym in closed:
        e = totals.get(ym, {"revenue": 0.0, "cogs": 0.0, "ntxn": 0})
        closed_months[ym] = {"revenue": round(e["revenue"], 2), "cogs": round(e["cogs"], 2), "ntxn": e["ntxn"]}

    return {
        "pulled_at_mt": output["meta"]["pulled_at_mt"],
        "picklist_snapshot": output.get("picklist_snapshot", {"channels": {}, "regions": {}}),
        "closed_months": closed_months,
        "sections": {name: {"rows": s.get("rows", 0)} for name, s in output["meta"]["sections"].items()},
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main_recheck(args):
    """--recheck PATH: load an existing output JSON, run only the T5 sentinel queries
    (cheap: 2 items, one YTD window, three small queries) plus checks.py's run_checks over
    everything else already in the file, and write the result back. Never re-pulls
    pnl_by_channel_month/inventory/etc. -- trusts what is already on disk for those."""
    recheck_path = Path(args.recheck)
    output = json.loads(recheck_path.read_text(encoding="utf-8"))
    env = load_env()

    meta = output.setdefault("meta", {})
    sections_meta = meta.setdefault("sections", {})
    D = {"ytd_start": meta["ytd_start"], "asof": meta["asof_date"]}

    t0 = time.time()
    try:
        t5_sentinels, nrows = build_t5_sentinels(env, D, output.get("sku_sales"))
        sections_meta["t5_sentinels"] = {"status": "ok", "rows": nrows, "seconds": round(time.time() - t0, 2), "error": None}
        print(f"[extract] --recheck t5_sentinels: ok, {nrows} rows, {sections_meta['t5_sentinels']['seconds']}s", file=sys.stderr)
    except Exception as e:
        t5_sentinels = None
        sections_meta["t5_sentinels"] = {"status": "error", "rows": 0, "seconds": round(time.time() - t0, 2), "error": str(e)[:2000]}
        print(f"[extract] --recheck t5_sentinels: ERROR: {e}", file=sys.stderr)

    meta["t5_sentinels"] = t5_sentinels or meta.get("t5_sentinels") or {}

    prev_state = {}
    if args.prev_state:
        p = Path(args.prev_state)
        if p.exists():
            prev_state = json.loads(p.read_text(encoding="utf-8"))
        else:
            print(f"[extract] WARNING: --prev-state {p} does not exist; checks (c)/(f)/(g) run as 'no prior state'", file=sys.stderr)

    checks_result = run_checks(output, prev_state)
    meta["checks"] = checks_result

    out_path = Path(args.out) if args.out else recheck_path
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"[extract] --recheck wrote {out_path}", file=sys.stderr)

    print("\n=== meta.t5_sentinels ===")
    print(json.dumps(meta["t5_sentinels"], indent=2))
    print("\n=== meta.checks ===")
    print(json.dumps(checks_result, indent=2))

    if not checks_result["all_pass"]:
        print("[extract] meta.checks.all_pass is false -- exiting non-zero (JSON was still written)", file=sys.stderr)
        sys.exit(1)


def _prev_month_end(ym):
    """YYYY-MM -> the YYYY-MM-DD of the LAST calendar day of the month before ym. Used to
    cap the anchor+increment (V-D history) builder at the month strictly before the
    current-month snapshot, so the snapshot and increment methods never both emit the
    same (account_id, ym)."""
    y, m = int(ym[:4]), int(ym[5:7])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    last_day = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-{last_day:02d}"


def main():
    parser = argparse.ArgumentParser(
        description="Read-only Spikeball financial-dashboard v2 extract (NetSuite prod 4201313 + Amazon SP-API Orders API)."
    )
    parser.add_argument("--out", default=None, help="Output JSON path (default: spike/data/latest.json next to this script)")
    parser.add_argument("--skip-amazon", action="store_true", help="Skip the Amazon Orders API module entirely")
    parser.add_argument("--amazon-max-minutes", type=int, default=40,
                         help="Wall-clock budget for the whole Amazon module, split across NA/EU orders pull "
                              "and both regions' getOrderItems backlog (default 40). Resumable: a run cut short "
                              "leaves state for the next run to continue from.")
    parser.add_argument("--asof", default=None,
                         help="As-of date YYYY-MM-DD, Mountain Time (default: yesterday MT, ruling R10). "
                              "Overrides for tests only -- sets meta.asof_override=true, which checks.py's "
                              "check (e) reads to skip the 'must equal yesterday' sub-check.")
    parser.add_argument("--write-state", default=None,
                         help="Write the small prior-run state JSON to PATH (for a future run's --prev-state)")
    parser.add_argument("--prev-state", default=None,
                         help="Path to a prior --write-state file; feeds checks.py checks (c), (f), (g)")
    parser.add_argument("--demand-plan", default=None,
                         help="Path to the parsed Demand Plan JSON written by demand_plan.py "
                              "(run_nightly.py wires this in; never fetched by this script itself). "
                              "Absent or unreadable degrades demand_vs_actual to an empty plan side.")
    parser.add_argument("--recheck", default=None,
                         help="Fast path: re-run only meta.checks (plus the small T5 sentinel queries) "
                              "against an EXISTING output JSON at PATH, without re-pulling any of the "
                              "heavy NetSuite sections (trusts what is already in the file). Writes back "
                              "to PATH, or to --out if given. For iterating on checks.py/T5 logic without "
                              "waiting for a full extract. All other flags except --out/--prev-state are "
                              "ignored in this mode.")
    args = parser.parse_args()

    if args.recheck:
        main_recheck(args)
        return

    out_path = Path(args.out) if args.out else Path(__file__).parent / "data" / "latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    env = load_env()
    rollups = load_rollups()

    now_dt = datetime.datetime.now(MT)
    if args.asof:
        asof_date = datetime.date.fromisoformat(args.asof)
        asof_override = True
    else:
        asof_date = (now_dt - datetime.timedelta(days=1)).date()  # yesterday MT, ruling R10
        asof_override = False

    mtd_start_date = asof_date.replace(day=1)
    ytd_start_date = datetime.date(asof_date.year, 1, 1)
    trailing_start_date = add_months(datetime.date(asof_date.year, asof_date.month, 1), -12)
    trailing_months = [
        ym_str(add_months(datetime.date(asof_date.year, asof_date.month, 1), -i))
        for i in range(12, -1, -1)
    ]
    prior_mtd_start = shift_year(mtd_start_date, -1)
    prior_mtd_end = shift_year(asof_date, -1)
    prior_ytd_start = shift_year(ytd_start_date, -1)
    prior_ytd_end = shift_year(asof_date, -1)
    py_trailing_start = shift_year(trailing_start_date, -1)
    py_asof = shift_year(asof_date, -1)
    trailing90_start = asof_date - datetime.timedelta(days=90)

    D = {
        "asof": asof_date.isoformat(), "asof_date": asof_date,
        "mtd_start": mtd_start_date.isoformat(),
        "mtd_start_dt": datetime.datetime.combine(mtd_start_date, datetime.time(0, 0, 0), tzinfo=MT),
        "now_dt": now_dt,
        "ytd_start": ytd_start_date.isoformat(),
        "trailing_start": trailing_start_date.isoformat(),
        "trailing_months": trailing_months,
        "prior_mtd_start": prior_mtd_start.isoformat(), "prior_mtd_end": prior_mtd_end.isoformat(),
        "prior_ytd_start": prior_ytd_start.isoformat(), "prior_ytd_end": prior_ytd_end.isoformat(),
        "py_trailing_start": py_trailing_start.isoformat(), "py_asof": py_asof.isoformat(),
        "trailing90_start": trailing90_start.isoformat(),
    }

    print(
        f"[extract] as-of {D['asof']} MT (override={asof_override}) | mtd_start={D['mtd_start']} "
        f"ytd_start={D['ytd_start']} trailing_start={D['trailing_start']} "
        f"trailing_months={D['trailing_months'][0]}..{D['trailing_months'][-1]}",
        file=sys.stderr,
    )

    sections_meta = {}

    def run(name, fn):
        t0 = time.time()
        try:
            data, nrows = fn()
            sections_meta[name] = {"status": "ok", "rows": nrows, "seconds": round(time.time() - t0, 2), "error": None}
            print(f"[extract] {name}: ok, {nrows} rows, {sections_meta[name]['seconds']}s", file=sys.stderr)
            return data
        except Exception as e:
            sections_meta[name] = {"status": "error", "rows": 0, "seconds": round(time.time() - t0, 2), "error": str(e)[:2000]}
            print(f"[extract] {name}: ERROR: {e}", file=sys.stderr)
            return None

    channels = run("channels", lambda: build_channels(env)) or []
    regions = run("regions", lambda: build_regions(env)) or []

    dtc_channel_id = rollups.get("dtc_channel_id")
    if dtc_channel_id is None:
        dtc_channel_id = next((c["id"] for c in channels if c["name"] == "Spikeball.com"), 5)
    amazon_channel_id = rollups.get("amazon_channel_id")
    if amazon_channel_id is None:
        amazon_channel_id = next((c["id"] for c in channels if c["name"] == "Amazon"), 1)

    pnl_month = run("pnl_by_channel_month", lambda: build_pnl_by_channel_month(env, D, channels))
    pnl_period = run("pnl_by_channel_period", lambda: build_pnl_by_channel_period(env, D, channels))
    self_check = run("self_check", lambda: build_self_check(env, D, pnl_month))
    if self_check and self_check.get("retried"):
        sections_meta["self_check"]["retried"] = True
        sections_meta["self_check"]["retry_detail"] = self_check.get("retry_detail")
        print(f"[extract] self_check: retried after concurrent-posting race -- {self_check.get('retry_detail')}", file=sys.stderr)

    us_region_ids = rollups.get("regions", {}).get("us_region_ids", [])
    dq_region_ids = rollups.get("regions", {}).get("data_quality_region_ids", [])
    dtc_region = run("dtc_by_region", lambda: build_dtc_by_region(env, D, regions, dtc_channel_id, us_region_ids, dq_region_ids))

    sku_sales = run("sku_sales", lambda: build_sku_sales(env, D, channels))
    t5_sentinels = run("t5_sentinels", lambda: build_t5_sentinels(env, D, sku_sales))
    inventory = run("inventory", lambda: build_inventory(env, D, sku_sales, dtc_channel_id, amazon_channel_id))
    life_to_date = run("life_to_date", lambda: build_life_to_date(env))

    revenue_lookup = {}
    if pnl_period:
        for row in pnl_period:
            if row["channel_id"] == "TOTAL":
                continue
            revenue_lookup[(row["channel_id"], "mtd")] = row["mtd"]["revenue"]
            revenue_lookup[(row["channel_id"], "ytd")] = row["ytd"]["revenue"]
    returns_by_channel = run("returns_by_channel", lambda: build_returns_by_channel(env, D, channels, revenue_lookup))

    orders_by_channel = run("orders_by_channel", lambda: build_orders_by_channel(env, D, channels, amazon_channel_id))
    picklist_snapshot = run("picklist_snapshot", lambda: build_picklist_snapshot(channels, regions))

    rollup_by_period = run("rollup_by_period", lambda: build_rollup_by_period(pnl_period, rollups)) if pnl_period else None
    rollup_by_month = run("rollup_by_month", lambda: build_rollup_by_month(pnl_month, rollups)) if pnl_month else None

    if args.skip_amazon:
        amazon_result = {
            "status": "skipped", "pulled_through_utc": None, "marketplaces": [],
            "by_marketplace_mtd": [], "sku_by_marketplace_mtd": [],
            "incremental_state": {}, "notes": {"skipped": "via --skip-amazon"},
        }
        sections_meta["amazon_orders"] = {"status": "skipped", "rows": 0, "seconds": 0.0, "error": None}
        print("[extract] amazon_orders: skipped via --skip-amazon", file=sys.stderr)
    else:
        t0 = time.time()
        try:
            amazon_result = run_amazon_orders(D, rollups, max_minutes=args.amazon_max_minutes)
        except Exception as e:
            amazon_result = {
                "status": "error", "pulled_through_utc": None, "marketplaces": [],
                "by_marketplace_mtd": [], "sku_by_marketplace_mtd": [],
                "incremental_state": {}, "notes": {"error": str(e)[:2000]},
            }
        elapsed = round(time.time() - t0, 2)
        sections_meta["amazon_orders"] = {
            "status": amazon_result["status"],
            "rows": len(amazon_result.get("by_marketplace_mtd", [])) + len(amazon_result.get("sku_by_marketplace_mtd", [])),
            "seconds": elapsed,
            "error": amazon_result.get("notes") if amazon_result["status"] == "error" else None,
        }
        print(f"[extract] amazon_orders: {amazon_result['status']}, "
              f"{sections_meta['amazon_orders']['rows']} rows, {elapsed}s", file=sys.stderr)

    # ------------------------------------------------------------------
    # v2 actual-only sections (PRD-v2 v0.6, ruling V2R1). Isolated builders; each fails
    # soft via run() (empty on error) so a v2 problem never breaks the v1 gate.
    # ------------------------------------------------------------------
    try:
        v2_accounts = fetch_accounts(env)
    except Exception as e:  # noqa: BLE001
        v2_accounts = {}
        print(f"[extract] v2 fetch_accounts failed: {e}", file=sys.stderr)

    pnl_by_account = run("pnl_by_account_month", lambda: build_pnl_by_account_month(env, D, v2_accounts))
    pnl_gross_net = run("pnl_channel_gross_net", lambda: build_pnl_channel_gross_net(env, D, channels))
    ebitda_month = run("ebitda_month", lambda: build_ebitda_month(env, D, pnl_by_account or []))

    bs_anchor_path = Path(__file__).parent / "config" / "bs_anchor.json"
    bs_anchor = None
    if bs_anchor_path.exists():
        try:
            bs_anchor = json.loads(bs_anchor_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[extract] v2 bs_anchor load failed: {e}", file=sys.stderr)

    # V-D balance sheet, two methods stitched into one series (research/10 + research/11):
    # the CURRENT month comes from NetSuite's native account.balance (the snapshot method --
    # reproduces the CFO's cr=-202 Balance Sheet to the cent, but account.balance has no
    # as-of-date parameter, so it can only ever read "now"). Every month BEFORE the current
    # one keeps coming from the anchor+increment method (account.balance cannot backfill a
    # past closed month), capped at the month strictly before the snapshot month so the two
    # methods never both emit the same (account_id, ym).
    current_snap = run("bs_snapshot_current", lambda: build_bs_snapshot_current(env, D)) or []
    hist_rows = []
    if bs_anchor:
        prev_end = _prev_month_end(D["asof"][:7])
        if prev_end[:7] >= bs_anchor["ym"]:
            D_hist = {**D, "asof": prev_end}
            hist_rows = run("bs_by_account_month_hist",
                            lambda: build_bs_by_account_month(env, D_hist, bs_anchor)) or []
    bs_by_account = hist_rows + current_snap
    if bs_by_account:
        cp = [r for r in bs_by_account if r.get("accttype") == "Bank"]
        cash_positions = cp
        sections_meta["cash_positions"] = {"status": "ok", "rows": len(cp), "seconds": 0.0, "error": None}
    else:
        cash_positions = None
        sections_meta["cash_positions"] = {"status": "skipped", "rows": 0, "seconds": 0.0,
                                           "error": "bs_by_account_month absent"}
    # NOTE: at the increment->snapshot boundary month, cf_month's balance-sheet-delta term
    # reflects a one-time METHOD correction (the increment method's rolled-forward balance
    # vs the snapshot method's independently-pulled account.balance, for the same accounts)
    # rather than real period cash flow. This is expected and confined to that one
    # transition month; every month before or after it is delta-clean.
    cf_month = run("cf_month", lambda: build_cf_month(env, D, bs_by_account or [], ebitda_month or [])) if bs_by_account else None

    # Append-only store for the durable monthly snapshot series (each nightly run captures
    # ONE month's worth of snapshot rows, tagged with this run's MT timestamp). Stashed
    # under a dedicated top-level key that publish_sheet.build_tables() explicitly skips
    # (see SKIP_TOP_LEVEL_KEYS there) so it never becomes a redundant truncate-loaded table
    # or Sheet tab of its own -- publish_bq.py reads it directly off the parsed JSON and
    # appends it to the `bs_snapshot` BigQuery table via the same WRITE_APPEND mechanism
    # run_log/run_state already use.
    bs_snapshot_append = [{**row, "captured_at": now_dt.isoformat()} for row in current_snap]

    ar_aging = run("ar_aging", lambda: build_ar_aging(env, D))
    ap_aging = run("ap_aging", lambda: build_ap_aging(env, D))
    open_orders = run("open_orders", lambda: build_open_orders(env, D))
    item_cost = run("item_cost", lambda: build_item_cost(env, D))

    # Demand vs actuals (research/09-cfo-input-mechanism-design.md). demand_plan.json is
    # produced by run_nightly.py calling spike/demand_plan.py BEFORE this script runs --
    # this script only ever reads that file, never the Sheet itself. Absent/unreadable
    # degrades to an empty plan side (never crashes the extract, matching item_cost above).
    demand_plan_data = None
    if args.demand_plan:
        dp_path = Path(args.demand_plan)
        if dp_path.exists():
            try:
                demand_plan_data = json.loads(dp_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(f"[extract] WARNING: --demand-plan {dp_path} unreadable: {e}", file=sys.stderr)
        else:
            print(f"[extract] WARNING: --demand-plan {dp_path} does not exist", file=sys.stderr)
    demand_plan_meta = {k: v for k, v in (demand_plan_data or {}).items() if k != "rows"} or None

    sku_sales_month = run("sku_sales_month", lambda: build_sku_sales_month(env, D))
    demand_vs_actual = run("demand_vs_actual",
                           lambda: build_demand_vs_actual(demand_plan_data, sku_sales_month, item_cost))

    output = {
        "meta": {
            "pulled_at_mt": now_dt.isoformat(),
            "asof_date": D["asof"],
            "asof_override": asof_override,
            "period_rule": "through_yesterday_close",
            "window_note": "windows end at yesterday's close; late postings dated inside the window can still arrive",
            "mtd_start": D["mtd_start"],
            "ytd_start": D["ytd_start"],
            "prior_year_mtd": [D["prior_mtd_start"], D["prior_mtd_end"]],
            "prior_year_ytd": [D["prior_ytd_start"], D["prior_ytd_end"]],
            "trailing_months": D["trailing_months"],
            "source_account": "4201313",
            "known_artifacts": KNOWN_ARTIFACTS,
            "features": rollups.get("features", {}),
            "rollups": rollups,
            "t5_sentinels": t5_sentinels or {},
            "sections": sections_meta,
        },
        "channels": channels,
        "regions": regions,
        "pnl_by_channel_month": pnl_month or [],
        "pnl_by_channel_period": pnl_period or [],
        "rollup_by_period": rollup_by_period or [],
        "rollup_by_month": rollup_by_month or [],
        "self_check": self_check or {},
        "dtc_by_region": dtc_region or {},
        "sku_sales": sku_sales or {"mtd": [], "ytd": [], "top5_concentration": []},
        "inventory": inventory or {},
        "life_to_date": life_to_date or {},
        "returns_by_channel": returns_by_channel or [],
        "orders_by_channel": orders_by_channel or [],
        "picklist_snapshot": picklist_snapshot or {"channels": {}, "regions": {}},
        "amazon_orders": amazon_result,
        "pnl_by_account_month": pnl_by_account or [],
        "pnl_channel_gross_net": pnl_gross_net or [],
        "ebitda_month": ebitda_month or [],
        "bs_by_account_month": bs_by_account or [],
        "bs_snapshot_append": bs_snapshot_append,
        "cash_positions": cash_positions or [],
        "cf_month": cf_month or [],
        "ar_aging": ar_aging or [],
        "ap_aging": ap_aging or [],
        "open_orders": open_orders or [],
        "item_cost": item_cost or [],
        "sku_sales_month": sku_sales_month or [],
        "demand_plan_meta": demand_plan_meta or {},
        "demand_vs_actual": demand_vs_actual or {"plan_vs_actual": [], "cost_coverage": []},
    }

    prev_state = {}
    if args.prev_state:
        p = Path(args.prev_state)
        if p.exists():
            prev_state = json.loads(p.read_text(encoding="utf-8"))
        else:
            print(f"[extract] WARNING: --prev-state {p} does not exist; checks (c)/(f)/(g) run as 'no prior state'", file=sys.stderr)

    checks_result = run_checks(output, prev_state)
    output["meta"]["checks"] = checks_result
    # v2 checks are informational: recorded under meta.checks_v2, NEVER folded into all_pass,
    # so a v2 parity problem cannot block the v1 dashboard the CFO relies on (prod-safe split).
    try:
        output["meta"]["checks_v2"] = run_checks_v2(output)
    except Exception as e:  # noqa: BLE001
        output["meta"]["checks_v2"] = {"v2_pass": False, "error": str(e)[:500]}
        print(f"[extract] checks_v2 raised: {e}", file=sys.stderr)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"[extract] wrote {out_path}", file=sys.stderr)

    if args.write_state:
        state_path = Path(args.write_state)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(build_write_state(output), f, indent=2, default=str)
        print(f"[extract] wrote state {state_path}", file=sys.stderr)

    print("\n=== meta.sections ===")
    print(json.dumps(sections_meta, indent=2))
    print("\n=== self_check ===")
    print(json.dumps(self_check if self_check else {}, indent=2))
    print("\n=== meta.checks ===")
    print(json.dumps(checks_result, indent=2))

    if not checks_result["all_pass"]:
        print("[extract] meta.checks.all_pass is false -- exiting non-zero (JSON was still written)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
