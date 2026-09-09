"""extract_v2.py -- actual-only v2 section builders for the Spikeball Financial Dashboard.

READ-ONLY. Isolated module so the live extract.py gains only an import plus a few run()
calls, never a refactor of the working v1 logic. Every builder returns (data, nrows) to
slot into extract.py's run(name, fn) wrapper. Sign convention matches extract.py:
Income/OthIncome value = -SUM(amount); COGS/Expense/OthExpense = SUM(amount).

Scope (PRD-v2 v0.6, ruling V2R1 actual-only): P&L by account (V-A), gross/net by channel,
EBITDA actuals (V-C), balance sheet by the anchor+increment method (V-D, research/07),
cash flow (V-E), working capital (V-F: AR/AP aging, open orders, item cost), demand
cost-coverage (V-H static). Forecast, the cash engine, and the CFO-inputs mechanism are
DEFERRED and are NOT in this module.

Proven by the parity tests in spike/parity/ before wiring: pnl_account_parity.py (V-A,
339/345 cells, all drifts named late postings), balance_sheet_parity.py (V-D anchor+
increment), cash_flow_parity.py (V-E + cf_map), ebitda_parity.py (V-C),
working_capital_parity.py (V-F).
"""
from __future__ import annotations

import json
from pathlib import Path

from _lib import fnum, suiteql, try_suiteql

PNL_MAP = json.loads((Path(__file__).parent / "config" / "pnl_map.json").read_text(encoding="utf-8"))
CF_MAP = json.loads((Path(__file__).parent / "config" / "cf_map.json").read_text(encoding="utf-8"))
INCOME_TYPES = set(PNL_MAP["income_accttypes"])
PL_TYPES = ("Income", "COGS", "Expense", "OthIncome", "OthExpense")


def to_int_or_none(v):
    """Match extract.py's channel normalization: NULL / empty -> None (Unassigned)."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# account metadata (shared): number, name, type, parent -> recursive hierarchy
# ---------------------------------------------------------------------------

def fetch_accounts(env):
    """All accounts with number/name/type/parent. Used to attach hierarchy (parent_id,
    level, path) to the account-grain P&L and balance sheet."""
    rows = suiteql(env, "SELECT id, acctnumber, fullname, accttype, parent FROM account ORDER BY id")
    by_id = {}
    for r in rows:
        by_id[str(r["id"])] = {
            "account_id": str(r["id"]),
            "acctnumber": str(r.get("acctnumber") or ""),
            "account_name": str(r.get("fullname") or ""),
            "accttype": r.get("accttype"),
            "parent_id": str(r["parent"]) if r.get("parent") else None,
        }
    # level + path (root-to-leaf acctnumbers) via parent chain
    for aid, a in by_id.items():
        chain = []
        cur = a
        seen = set()
        while cur and cur["account_id"] not in seen:
            seen.add(cur["account_id"])
            chain.append(cur["acctnumber"])
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
        a["level"] = len(chain) - 1
        a["path"] = "/".join(reversed(chain))
    return by_id


# ---------------------------------------------------------------------------
# V-A: P&L by account x month (PRD-v2 P3; proven by pnl_account_parity.py)
# ---------------------------------------------------------------------------

def build_pnl_by_account_month(env, D, accounts=None):
    """One SuiteQL over all P&L posting accounts, account x accounting month, from the
    trailing window start through the as-of date. Signed to the CFO's convention (income
    positive, expense positive, contra-income negative). Zero-activity accounts are simply
    absent, matching NetSuite; subtotals are NOT rows (they are recomputed at render by
    `path`). Reproduces IncomeStatement_2026 to the cent for closed months."""
    accounts = accounts or fetch_accounts(env)
    start, end = D["trailing_start"], D["asof"]
    sql = f"""
        SELECT a.id AS acct_id, a.acctnumber AS num, a.accttype AS ty,
               TO_CHAR(t.trandate,'YYYY-MM') AS ym, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype IN {PL_TYPES}
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY a.id, a.acctnumber, a.accttype, TO_CHAR(t.trandate,'YYYY-MM')
        ORDER BY a.acctnumber, TO_CHAR(t.trandate,'YYYY-MM')
    """
    rows = suiteql(sql=sql, env=env)
    out = []
    for r in rows:
        ty = r["ty"]
        val = fnum(r["amt"])
        val = -val if ty in INCOME_TYPES else val
        meta = accounts.get(str(r["acct_id"]), {})
        out.append({
            "ym": r["ym"],
            "account_id": str(r["acct_id"]),
            "acctnumber": str(r["num"]),
            "account_name": meta.get("account_name", ""),
            "accttype": ty,
            "parent_id": meta.get("parent_id"),
            "level": meta.get("level"),
            "path": meta.get("path"),
            "is_leaf": True,
            "amount": round(val, 2),
        })
    out.sort(key=lambda x: (x["ym"], x["acctnumber"]))
    return out, len(out)


# ---------------------------------------------------------------------------
# V-A: gross / net revenue by channel x month (the CFO's rows 4-10)
# ---------------------------------------------------------------------------

def build_pnl_channel_gross_net(env, D, channels):
    """Channel x month gross and net revenue, split into the CFO's components (Sales,
    Tournaments, Shipping, Discounts, Refunds, Returns). Income accounts only, negated.
    net_revenue must equal pnl_by_channel_month.revenue for the same channel-month
    (check i). Income accttype only (matches v1 income_by_channel_month and the CFO's Net
    Revenue = Total 40000000 Revenue); OthIncome (interest, FX) is not revenue."""
    start, end = D["trailing_start"], D["asof"]
    sql = f"""
        SELECT TO_CHAR(t.trandate,'YYYY-MM') AS ym, tl.cseg_appf_channel AS chan,
               a.acctnumber AS num, SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transactionline tl ON tl.transaction = ai.transaction AND tl.id = ai.transactionline
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype = 'Income'
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY TO_CHAR(t.trandate,'YYYY-MM'), tl.cseg_appf_channel, a.acctnumber
        ORDER BY TO_CHAR(t.trandate,'YYYY-MM'), a.acctnumber
    """
    rows = suiteql(sql=sql, env=env)
    gross_map = PNL_MAP["gross_component_labels"]
    discount = set(PNL_MAP["discount_accounts"])
    refund = set(PNL_MAP["refund_accounts"])
    returns = set(PNL_MAP["return_accounts"])
    chan_by_id = {str(c["id"]): c["name"] for c in channels}

    agg = {}
    for r in rows:
        chan_int = to_int_or_none(r.get("chan"))
        chan_id = str(chan_int) if chan_int is not None else None
        key = (r["ym"], chan_id)
        e = agg.setdefault(key, {"gross_sales": 0.0, "tournaments": 0.0, "shipping": 0.0,
                                 "discounts": 0.0, "refunds": 0.0, "returns": 0.0, "net_revenue": 0.0})
        num = str(r["num"])
        val = -fnum(r["amt"])
        e["net_revenue"] += val
        if num in gross_map:
            e[gross_map[num]] += val
        elif num in discount:
            e["discounts"] += val
        elif num in refund:
            e["refunds"] += val
        elif num in returns:
            e["returns"] += val
    out = []
    for (ym, chan_id), e in sorted(agg.items(), key=lambda kv: (kv[0][0], kv[0][1] or "~")):
        gross_revenue = e["gross_sales"] + e["tournaments"] + e["shipping"]
        out.append({
            "ym": ym,
            "channel_id": chan_id,
            "channel": chan_by_id.get(chan_id, "Unassigned") if chan_id else "Unassigned",
            "gross_sales": round(e["gross_sales"], 2),
            "tournaments": round(e["tournaments"], 2),
            "shipping": round(e["shipping"], 2),
            "gross_revenue": round(gross_revenue, 2),
            "discounts": round(e["discounts"], 2),
            "refunds": round(e["refunds"], 2),
            "returns": round(e["returns"], 2),
            "net_revenue": round(e["net_revenue"], 2),
        })
    return out, len(out)


# ---------------------------------------------------------------------------
# V-C: EBITDA / Adjusted EBITDA by month (proven by ebitda_parity.py, PASS)
# Confirmed accounts: D&A 70000000, Bank Interest 80200000, Other Interest 80200100,
# Inventory Adjustments add-back 50200000. All in the expense bucket (SUM(amount)).
# non_recurring is 0 in actual-only v2 (the July tariff refund needs a CFO input, deferred).
# ---------------------------------------------------------------------------

def build_ebitda_month(env, D, pnl_account_rows):
    """Reuse the account-grain P&L rows to compute EBITDA and Adjusted EBITDA per month,
    exactly per the CFO's definition (2026 Forecast rows 96/97/99). Net Income =
    income - expense; EBITDA adds back bank interest, other interest, D&A; Adjusted adds
    back the 50200000 Inventory Adjustments line."""
    add = PNL_MAP["ebitda_addback_accounts"]
    bank, other, da = add["bank_interest"], add["other_interest"], add["depreciation_amortization"]
    inv_adj = PNL_MAP["inventory_adjustment_account"]
    per = {}
    for r in pnl_account_rows or []:
        ym = r["ym"]
        e = per.setdefault(ym, {"income": 0.0, "expense": 0.0, "bank": 0.0, "other": 0.0, "da": 0.0, "inv": 0.0})
        amt = r["amount"]
        if r["accttype"] in INCOME_TYPES:
            e["income"] += amt
        else:
            e["expense"] += amt
        num = r["acctnumber"]
        if num == bank:
            e["bank"] += amt
        elif num == other:
            e["other"] += amt
        elif num == da:
            e["da"] += amt
        elif num == inv_adj:
            e["inv"] += amt
    out = []
    for ym in sorted(per):
        e = per[ym]
        net_income = e["income"] - e["expense"]
        ebitda = net_income + e["bank"] + e["other"] + e["da"]
        adjusted = ebitda + e["inv"]
        out.append({
            "ym": ym,
            "net_income": round(net_income, 2),
            "bank_interest": round(e["bank"], 2),
            "other_interest": round(e["other"], 2),
            "da": round(e["da"], 2),
            "ebitda": round(ebitda, 2),
            "inventory_adjustments": round(e["inv"], 2),
            "adjusted_ebitda": round(adjusted, 2),
            "non_recurring": 0.0,
            "adjusted_ebitda_ex_nr": round(adjusted, 2),
        })
    return out, len(out)


# ---------------------------------------------------------------------------
# V-E: cash flow statement (indirect) by month (cf_map proven by cash_flow_parity.py, PASS)
# Consumes bs_by_account_month (V-D) month-over-month deltas + Net Income and D&A from the
# EBITDA rows. Every June line tied to the cent against Cash Flow Statement_2026.
# ---------------------------------------------------------------------------

def build_cf_month(env, D, bs_rows, ebitda_rows):
    """Indirect-method cash flow per month, from balance-sheet account deltas + NI + D&A.
    Skips the first month (no prior for a delta). Ties to the cent for closed months
    (anchored balances); the open month inherits V-D's provisional balances."""
    if not bs_rows:
        return [], 0
    # balance[(acctnumber, ym)] = balance
    bal = {}
    months = set()
    for r in bs_rows:
        bal[(r["acctnumber"], r["ym"])] = r["balance"]
        months.add(r["ym"])
    months = sorted(months)
    ni_by = {r["ym"]: r["net_income"] for r in (ebitda_rows or [])}
    da_by = {r["ym"]: r["da"] for r in (ebitda_rows or [])}

    def acct_sum(accts, ym):
        return sum(bal.get((a, ym), 0.0) for a in accts)

    out = []
    for i, ym in enumerate(months):
        if i == 0:
            continue  # need a prior month for deltas
        prev = months[i - 1]
        lines = []
        net_change = 0.0
        for ln in CF_MAP["cf_lines"]:
            src = ln["source"]
            if src == "income_statement_total":
                val = round(ni_by.get(ym, 0.0), 2)
            elif src == "income_statement_account":
                val = round(da_by.get(ym, 0.0), 2)
            else:  # bs_account_sum
                delta = acct_sum(ln["accounts"], ym) - acct_sum(ln["accounts"], prev)
                val = round(-delta if ln["sign_rule"] == "negative_of_delta" else delta, 2)
            net_change += val
            lines.append({
                "ym": ym, "cf_line": ln["cf_line"], "cf_category": ln["category"],
                "cf_statement_row": ln["cf_statement_row"], "amount": val,
            })
        # beginning/ending cash from the cash accounts (ground truth for the cash delta)
        cash_accts = CF_MAP["cash_accounts"]["accounts"]
        beg = round(acct_sum(cash_accts, prev), 2)
        end = round(acct_sum(cash_accts, ym), 2)
        cash_delta = round(end - beg, 2)
        # The CFO's accrual->cash bridge (the named lines above) does not itemize non-earnings
        # equity movements (shareholder distributions/contributions) -- par-cf flagged this as a
        # limitation of the workbook's own CF method. The balance sheet foots every month, so the
        # gap between the named lines and the real cash delta is exactly those movements plus any
        # account cf_map does not name. Book it to an explicit financing line so the statement
        # always foots to the actual bank movement (three-statement consistency, check n / P10),
        # and the omission is visible rather than silent.
        other = round(cash_delta - net_change, 2)
        if abs(other) > 0.01:
            lines.append({"ym": ym, "cf_line": "Distributions & Other Equity", "cf_category": "financing",
                          "cf_statement_row": 25, "amount": other})
        lines.append({"ym": ym, "cf_line": "Net Change in Cash", "cf_category": "total",
                      "cf_statement_row": 26, "amount": cash_delta})
        lines.append({"ym": ym, "cf_line": "Beginning Cash", "cf_category": "total",
                      "cf_statement_row": 27, "amount": beg})
        lines.append({"ym": ym, "cf_line": "Ending Cash", "cf_category": "total",
                      "cf_statement_row": 28, "amount": end})
        out.extend(lines)
    return out, len(out)


# ---------------------------------------------------------------------------
# V-F: working capital -- AR/AP aging, open orders, item cost
# (proven by working_capital_parity.py, PASS). Traps par-wc found and fixed:
#  - SuiteQL deep-offset pagination silently loses rows at large offsets: keep the status
#    filter in WHERE so the open-order result set stays small (~1.2k rows).
#  - SalesOrd/PurchOrd t.status is a bare letter code (A/B/D/E), not 'SalesOrd:A'.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402


def _bucket_open(env, ttype, asof):
    """Shared open-item aging: one row per open transaction (foreignamountunpaid<>0),
    bucketed by days past due against asof. Returns (buckets_dict, n_entities, total)."""
    rows = suiteql(env, f"""
        SELECT t.entity AS ent, TO_CHAR(t.duedate,'YYYY-MM-DD') AS duedate, t.foreignamountunpaid AS amt
        FROM transaction t
        WHERE t.type = '{ttype}' AND t.foreignamountunpaid <> 0
        ORDER BY t.id
    """)
    asof_d = _dt.date.fromisoformat(asof)
    buckets = {"not_yet_due": [0.0, 0], "1_30": [0.0, 0], "31_60": [0.0, 0],
               "61_90": [0.0, 0], "over_90": [0.0, 0], "no_duedate": [0.0, 0]}
    entities = set()
    total = 0.0
    for r in rows:
        amt = fnum(r.get("amt"))
        total += amt
        if r.get("ent") is not None:
            entities.add(r["ent"])
        dd = r.get("duedate")
        if not dd:
            b = "no_duedate"
        else:
            days = (asof_d - _dt.date.fromisoformat(dd)).days
            if days <= 0:
                b = "not_yet_due"
            elif days <= 30:
                b = "1_30"
            elif days <= 60:
                b = "31_60"
            elif days <= 90:
                b = "61_90"
            else:
                b = "over_90"
        buckets[b][0] += amt
        buckets[b][1] += 1
    return buckets, len(entities), round(total, 2)


def _aging_rows(buckets, asof):
    order = ["not_yet_due", "1_30", "31_60", "61_90", "over_90", "no_duedate"]
    return [{"bucket": b, "amount": round(buckets[b][0], 2), "open_count": buckets[b][1], "asof_date": asof}
            for b in order]


def build_ar_aging(env, D):
    buckets, n_cust, total = _bucket_open(env, "CustInvc", D["asof"])
    rows = _aging_rows(buckets, D["asof"])
    rows.append({"bucket": "TOTAL", "amount": total, "open_count": n_cust, "asof_date": D["asof"]})
    return rows, len(rows)


def build_ap_aging(env, D):
    buckets, n_vend, total = _bucket_open(env, "VendBill", D["asof"])
    rows = _aging_rows(buckets, D["asof"])
    rows.append({"bucket": "TOTAL", "amount": total, "open_count": n_vend, "asof_date": D["asof"]})
    return rows, len(rows)


def build_open_orders(env, D):
    """Open sales orders by ship-month (status A/B/D/E) and open purchase orders (status
    B/D/E). Status filter in WHERE (deep-offset trap). PO expected month is null where
    NetSuite has no duedate; the no-date share is surfaced, not hidden."""
    so = suiteql(env, """
        SELECT TO_CHAR(t.shipdate,'YYYY-MM') AS shipmonth, t.foreigntotal AS amt
        FROM transaction t WHERE t.type='SalesOrd' AND t.status IN ('A','B','D','E')
        ORDER BY t.id
    """)
    so_by = {}
    for r in so:
        m = r.get("shipmonth") or "no_shipdate"
        e = so_by.setdefault(m, [0.0, 0])
        e[0] += fnum(r.get("amt"))
        e[1] += 1
    po = suiteql(env, """
        SELECT TO_CHAR(t.duedate,'YYYY-MM') AS duemonth, t.foreigntotal AS amt, t.duedate AS dd
        FROM transaction t WHERE t.type='PurchOrd' AND t.status IN ('B','D','E')
        ORDER BY t.id
    """)
    po_by = {}
    po_total = 0.0
    po_nodate = 0.0
    for r in po:
        po_total += fnum(r.get("amt"))
        m = r.get("duemonth") or "no_duedate"
        if not r.get("dd"):
            po_nodate += fnum(r.get("amt"))
        e = po_by.setdefault(m, [0.0, 0])
        e[0] += fnum(r.get("amt"))
        e[1] += 1
    out = []
    for m in sorted(so_by):
        out.append({"order_type": "SalesOrd", "expected_ym": m if m != "no_shipdate" else None,
                    "amount_open": round(so_by[m][0], 2), "count": so_by[m][1]})
    for m in sorted(po_by):
        out.append({"order_type": "PurchOrd", "expected_ym": m if m != "no_duedate" else None,
                    "amount_open": round(po_by[m][0], 2), "count": po_by[m][1]})
    out.append({"order_type": "PurchOrd_summary", "expected_ym": None,
                "amount_open": round(po_total, 2), "count": len(po),
                "no_duedate_amount": round(po_nodate, 2),
                "no_duedate_share": round(po_nodate / po_total, 4) if po_total else None})
    return out, len(out)


def _read_demand_skus():
    """The demand-plan SKU list, from a committed JSON config -- NOT the workbook. The
    nightly cloud sandbox has neither openpyxl nor the workbook (the 2026-08-28 03:06
    nightly crashed on a module-level openpyxl import). Regenerate demand_skus.json from
    the workbook locally when the demand plan changes. Returns [] if the config is absent,
    so build_item_cost degrades to an empty section rather than crashing the extract."""
    p = Path(__file__).parent / "config" / "demand_skus.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("skus", [])
    except Exception:
        return []


def build_item_cost(env, D, skus=None):
    """NetSuite average cost and last purchase price for the demand SKUs (V-F / V-H
    cost coverage). One row per SKU; MISSING flagged for SKUs with no NetSuite item."""
    skus = skus or _read_demand_skus()
    quoted = ",".join("'" + s.replace("'", "''") + "'" for s in skus)
    rows = suiteql(env, f"""
        SELECT itemid, averagecost, lastpurchaseprice, id
        FROM item WHERE itemid IN ({quoted})
    """)
    by_sku = {str(r["itemid"]): r for r in rows}
    out = []
    for sku in skus:
        r = by_sku.get(sku)
        out.append({
            "sku": sku,
            "item_id": str(r["id"]) if r else None,
            "average_cost": fnum(r.get("averagecost")) if r else None,
            "last_purchase_price": fnum(r.get("lastpurchaseprice")) if r else None,
            "asof_date": D["asof"],
            "in_netsuite": r is not None,
        })
    return out, len(out)


# ---------------------------------------------------------------------------
# V-H: demand vs actuals (research/09-cfo-input-mechanism-design.md; PRD-v2 E7). The plan
# side comes from the CFO's read-only "Demand Plan" tab (spike/demand_plan.py, read by
# run_nightly.py, passed to extract.py as --demand-plan PATH -- never fetched here). The
# actual side needs a monthly SKU-actuals grain that did not exist before this build;
# sku_month_query below duplicates extract.py's sku_by_channel_query WHERE clause (the
# Income-line/BOM rule) rather than importing it, to avoid a circular import (extract.py
# imports extract_v2, never the reverse).
# ---------------------------------------------------------------------------

def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def sku_month_query(env, start, end):
    """Same Income-join BOM rule as extract.py's sku_by_channel_query (an item line
    counts as a sale only with a matching Income accounting line -- the BOM-component
    trap), additionally grouped by accounting month for the plan-vs-actual monthly grain.
    ORDER BY item + month (paginated-query trap, reference_suiteql_pagination_needs_order_by)."""
    sql = f"""
        SELECT tl.item AS itemid, TO_CHAR(t.trandate,'YYYY-MM') AS ym,
               SUM(tl.quantity) AS qty, SUM(ai.amount) AS amt
        FROM transactionline tl
        JOIN transactionaccountingline ai ON ai.transaction = tl.transaction AND ai.transactionline = tl.id
        JOIN transaction t ON t.id = tl.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype = 'Income' AND tl.mainline = 'F'
          AND tl.itemtype IN ('InvtPart','Assembly','Kit','NonInvtPart')
          AND t.trandate >= TO_DATE('{start}','YYYY-MM-DD') AND t.trandate <= TO_DATE('{end}','YYYY-MM-DD')
        GROUP BY tl.item, TO_CHAR(t.trandate,'YYYY-MM')
        ORDER BY tl.item, TO_CHAR(t.trandate,'YYYY-MM')
    """
    return suiteql(env, sql)


def _item_sku_lookup(env, item_ids):
    if not item_ids:
        return {}
    out = {}
    for chunk in _chunks(sorted(item_ids), 200):
        ids_csv = ",".join(str(i) for i in chunk)
        rows = suiteql(env, f"SELECT id, itemid FROM item WHERE id IN ({ids_csv}) ORDER BY id")
        out.update({int(r["id"]): r["itemid"] for r in rows})
    return out


def build_sku_sales_month(env, D):
    """SKU x month actual units/revenue over the trailing 13-month window -- the actuals
    side of the demand-vs-actual join (plan is annual by calendar month; only (sku, ym)
    keys the plan also has are ever surfaced downstream). One row per sku x month with
    nonzero units or revenue; zero-activity sku-months are simply absent, matching every
    other v2 builder's convention."""
    rows = sku_month_query(env, D["trailing_start"], D["asof"])
    item_ids = {int(r["itemid"]) for r in rows if r.get("itemid") is not None}
    sku_by_id = _item_sku_lookup(env, item_ids)
    out = []
    for r in rows:
        iid = to_int_or_none(r.get("itemid"))
        if iid is None:
            continue
        units = round(-fnum(r.get("qty")), 2)
        revenue = round(-fnum(r["amt"]), 2)
        if units == 0 and revenue == 0:
            continue
        out.append({"sku": sku_by_id.get(iid), "item_id": iid, "ym": r["ym"],
                    "units": units, "revenue": revenue})
    out.sort(key=lambda x: (x["sku"] or "", x["ym"]))
    return out, len(out)


def build_demand_vs_actual(demand_plan, sku_sales_month, item_cost_rows):
    """Plan vs actual units, SKU x month (research/09 Section 5), plus a cost-coverage
    table (item_cost joined by SKU, missing-cost SKUs flagged rather than hidden --
    PRD-v2.md:298, the 19,706-unit gap research/02d Section 6 already flags). `demand_plan`
    is the parsed dict fetch_demand_plan()/demand_plan.py wrote (or None/invalid, which
    degrades to an empty plan side -- never crashes the extract, per run() in extract.py's
    soft-fail wrapper). Grain: SKU x month; Customer/Location are on the raw plan rows for
    anyone drilling further, but are summed away here to match the actuals grain, which
    carries no customer/location dimension in NetSuite's Income-line data."""
    plan_rows = (demand_plan or {}).get("rows") or []

    plan_by_sku_month, plan_price_by_sku = {}, {}
    for r in plan_rows:
        sku = r.get("sku")
        if not sku:
            continue
        if r.get("unit_price") is not None:
            plan_price_by_sku.setdefault(sku, r["unit_price"])
        for ym, units in (r.get("months") or {}).items():
            key = (sku, ym)
            plan_by_sku_month[key] = plan_by_sku_month.get(key, 0.0) + (units or 0.0)

    actual_by_sku_month = {}
    for r in sku_sales_month or []:
        sku = r.get("sku")
        if not sku:
            continue
        e = actual_by_sku_month.setdefault((sku, r["ym"]), {"units": 0.0, "revenue": 0.0})
        e["units"] += r.get("units") or 0.0
        e["revenue"] += r.get("revenue") or 0.0

    cost_by_sku = {r["sku"]: r for r in (item_cost_rows or [])}
    inputs_edited_at = (demand_plan or {}).get("read_at_mt")
    inputs_stale = bool((demand_plan or {}).get("stale"))

    plan_vs_actual = []
    for sku, ym in sorted(set(plan_by_sku_month) | set(actual_by_sku_month)):
        plan_units = round(plan_by_sku_month.get((sku, ym), 0.0), 2)
        actual = actual_by_sku_month.get((sku, ym), {"units": 0.0, "revenue": 0.0})
        actual_units = round(actual["units"], 2)
        variance = round(actual_units - plan_units, 2)
        price = plan_price_by_sku.get(sku)
        cost_row = cost_by_sku.get(sku)
        plan_vs_actual.append({
            "sku": sku, "ym": ym,
            "plan_units": plan_units, "actual_units": actual_units,
            "variance_units": variance,
            "variance_pct": round(variance / plan_units * 100, 2) if plan_units else None,
            "plan_dollars": round(plan_units * price, 2) if price is not None else None,
            "actual_dollars": round(actual["revenue"], 2),
            "unit_cost": cost_row.get("average_cost") if cost_row else None,
            "in_netsuite": cost_row.get("in_netsuite") if cost_row else (sku in cost_by_sku),
            "inputs_edited_at": inputs_edited_at, "inputs_stale": inputs_stale,
        })

    plan_units_by_sku = {}
    for (sku, _ym), units in plan_by_sku_month.items():
        plan_units_by_sku[sku] = plan_units_by_sku.get(sku, 0.0) + units
    cost_coverage = []
    for sku in sorted(plan_units_by_sku):
        cost_row = cost_by_sku.get(sku)
        cost_coverage.append({
            "sku": sku,
            "in_netsuite": cost_row.get("in_netsuite") if cost_row else False,
            "average_cost": cost_row.get("average_cost") if cost_row else None,
            "last_purchase_price": cost_row.get("last_purchase_price") if cost_row else None,
            "unit_price": plan_price_by_sku.get(sku),
            "total_plan_units": round(plan_units_by_sku[sku], 2),
            "cost_missing": cost_row is None or cost_row.get("average_cost") is None,
        })

    result = {"plan_vs_actual": plan_vs_actual, "cost_coverage": cost_coverage}
    return result, len(plan_vs_actual) + len(cost_coverage)


# ---------------------------------------------------------------------------
# V-D (balance sheet, anchor+increment) lives in extract_v2_bs.py (par-bs), imported by
# extract.py alongside these builders. V-H cost-coverage is item_cost joined to the demand
# plan; item_cost above is the shippable actual-only piece, demand_vs_actual above the
# CFO-input-dependent piece (research/09).
# ---------------------------------------------------------------------------
