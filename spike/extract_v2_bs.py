"""extract_v2_bs.py -- V-D balance sheet by account x month, anchor+increment method.

READ-ONLY. SuiteQL SELECT only. Never creates/edits/deletes any record, saved search, or
report. Productionizes spike/parity/balance_sheet_parity.py's proven method (70/76 workbook
leaves tie to the cent, research/07's anchor-plus-increment method) into a builder
extract.py can import.

Method: given an ANCHOR (a trusted month-end balance snapshot keyed by NetSuite account id
-- in production a cr=-202 pull; bootstrapped here from the CFO workbook fixture with zero
owner 2FA needed via bootstrap_anchor_from_workbook), roll every tracked balance-sheet
account forward from the anchor month through the as-of date by its own GL posting flow
for each month after the anchor. Rows are emitted ONLY for the anchor month
(method="anchor", value copied exactly, no query) and every month strictly after it
(method="increment"). Months before the anchor are never emitted -- research/07 proved a
life-to-date GL sum does not reproduce this instance's balance-sheet balances (processor
bank accounts and the Byline-8970 LOC-swept leg accumulate with no clean life-to-date net).

Sign rule (empirically confirmed in spike/parity/balance_sheet_parity.py against every
leaf on Balance Sheet_2026, one rule per accttype, no per-account exception):
  DIRECT (value = SUM(amount)):  Bank, AcctRec, OthCurrAsset, FixedAsset, OthAsset,
                                  DeferExpense (debit-normal balance-sheet types)
  NEGATE (value = -SUM(amount)): AcctPay, CredCard, OthCurrLiab, LongTermLiab, Equity
                                  (credit-normal balance-sheet types)
OthAsset and DeferExpense were not on the workbook's 76 leaves (so were not parity-tested
against a fixture value) -- they are classified DIRECT per the universal NetSuite/GAAP
debit-normal convention for those two types, not a fixture-verified fact. Flagged here so a
future drift on one of those 3 accounts (measured 2026-08-27: 1 OthAsset, 2 DeferExpense)
is not mistaken for a bug in this rule.

The set of tracked accounts is NOT the workbook's 76 leaves -- it is every ACTIVE account in
the live chart whose accttype is one of the 9 above, unioned with every id present in the
anchor. This makes the pipeline self-healing for a brand-new account that activates with no
anchor entry (exactly what Highbeam-1408 was for the Jan-2026 anchor: see
claudedocs/vd-balance-sheet-parity-2026-08-27.md) -- it is simply picked up at anchor=0.0
and incremented normally, rather than silently dropped.

"Net Income" (the workbook's row 155) is not a GL account; it is NetSuite's computed
cumulative P&L rollup for the fiscal year to date. It is represented here as one synthetic
row per month, account_id = "synthetic:net_income", accttype "Equity" (it is the dynamic
piece of total equity; the static Equity accounts -- Capital Stock, Shareholder
Distributions, Retained Earnings, Additional Paid-In Capital -- are tracked as ordinary
accounts above). Anchored the same way and incremented by the month's total P&L flow
(Income+COGS+Expense+OthIncome+OthExpense combined, same convention as
spike/parity/pnl_account_parity.py).

Run (bootstraps the anchor + a June/July round-trip check, read-only, writes only
spike/config/bs_anchor.json):
  doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python spike/extract_v2_bs.py
"""
from __future__ import annotations

import calendar
import json
import re
import sys
from pathlib import Path

from _lib import fnum, load_env, suiteql
from extract_v2 import fetch_accounts

# NOTE: openpyxl is imported lazily inside bootstrap_anchor_from_workbook() only. It is a
# local-only dependency (reading the workbook fixture) and is NOT installed in the nightly
# cloud sandbox. Importing it at module level crashed the 2026-08-28 03:06 nightly run
# (extract.py imports this module). The nightly path (build_bs_by_account_month, which loads
# bs_anchor.json) never touches openpyxl.

SYNTHETIC_NET_INCOME_ID = "synthetic:net_income"

DIRECT_TYPES = {"Bank", "AcctRec", "OthCurrAsset", "FixedAsset", "OthAsset", "DeferExpense"}
NEGATE_TYPES = {"AcctPay", "CredCard", "OthCurrLiab", "LongTermLiab", "Equity"}
BS_TYPES = DIRECT_TYPES | NEGATE_TYPES
PNL_TYPES = {"Income", "COGS", "Expense", "OthIncome", "OthExpense"}

WORKBOOK_SHEET = "Balance Sheet_2026"
WORKBOOK_COLS = {2: "2026-01", 3: "2026-02", 4: "2026-03", 5: "2026-04", 6: "2026-05", 7: "2026-06"}
NET_INCOME_LABEL = "Net Income"


def sign(accttype):
    if accttype in DIRECT_TYPES:
        return 1.0
    if accttype in NEGATE_TYPES:
        return -1.0
    raise ValueError(f"Unmapped accttype for sign rule: {accttype}")


def _sql_in_strs(vals):
    return "(" + ",".join(f"'{v}'" for v in vals) + ")"


def _next_ym(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y+1}-01" if m == 12 else f"{y}-{m+1:02d}"


def _month_bounds(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def _months_between(start_ym, end_ym):
    """Inclusive list of YYYY-MM strings from start_ym to end_ym. String comparison of
    YYYY-MM is safe across year boundaries (fixed-width, zero-padded)."""
    if start_ym > end_ym:
        return []
    out, cur = [], start_ym
    while cur <= end_ym:
        out.append(cur)
        cur = _next_ym(cur)
    return out


# ---------------------------------------------------------------------------
# Workbook fixture reading (bootstrap only -- production anchors come from cr=-202,
# not from this workbook). Identical logic to spike/parity/balance_sheet_parity.py.
# ---------------------------------------------------------------------------

def read_workbook_leaves(workbook_path, sheet=WORKBOOK_SHEET):
    import openpyxl  # lazy: local-only dep, absent in the nightly sandbox (see module header)
    ws = openpyxl.load_workbook(workbook_path, data_only=True)[sheet]
    leaves = []
    for r in range(8, ws.max_row + 1):
        label = ws.cell(r, 1).value
        if label is None:
            continue
        label = str(label).strip()
        if label.startswith("Total"):
            continue
        vals = {}
        for c, ym in WORKBOOK_COLS.items():
            v = ws.cell(r, c).value
            if v is not None:
                vals[ym] = round(fnum(v), 2)
        if not vals:
            continue
        m = re.match(r"^(\d{6,9})\s*-\s*(.+)$", label)
        acctnum = m.group(1) if m else None
        name = m.group(2) if m else label
        leaves.append({"row": r, "label": label, "acctnum": acctnum, "name": name, "vals": vals})
    return leaves


def resolve_workbook_leaves(env, leaves):
    """Resolve each workbook leaf row to its live NetSuite account id(s) + accttype.
    Same resolution rules as balance_sheet_parity.py: numbered leaves match by acctnumber
    (preferring the active account on a number collision, disambiguated by name substring
    when more than one active account shares a number); GST Paid / VAT on Sales / Retained
    Earnings (no number printed on the workbook row) resolve by exact/prefix name match
    against the live chart; "Net Income" is not a GL account (special marker)."""
    coa = suiteql(env, "SELECT id, acctnumber, fullname, accttype, isinactive FROM account ORDER BY id")
    by_num = {}
    for a in coa:
        by_num.setdefault(a.get("acctnumber"), []).append(a)

    resolved = []
    for leaf in leaves:
        if leaf["name"] == NET_INCOME_LABEL and leaf["acctnum"] is None:
            resolved.append({**leaf, "ids": None, "accttype": None, "special": "net_income"})
            continue
        if leaf["acctnum"] is None:
            if leaf["name"] == "GST Paid":
                matches = [a for a in coa if a["fullname"] == "GST Paid"]
            elif leaf["name"] == "VAT on Sales":
                matches = [a for a in coa if re.match(r"^VAT on Sales(\s*\[\d+\])?$", a["fullname"])]
            elif leaf["name"] == "Retained Earnings":
                matches = [a for a in coa if a["fullname"].endswith("Retained Earnings")]
            else:
                raise SystemExit(f"No resolution rule for unnumbered leaf: {leaf['label']!r}")
        else:
            cands = by_num.get(leaf["acctnum"], [])
            active = [a for a in cands if a["isinactive"] == "F"]
            if len(active) == 1:
                matches = active
            elif len(active) > 1:
                name_key = leaf["name"].lower()
                matches = [a for a in active if name_key in a["fullname"].lower()]
                if len(matches) != 1:
                    raise SystemExit(f"Ambiguous active match for {leaf['label']!r}: {active}")
            else:
                matches = cands
            if len(matches) != 1:
                raise SystemExit(f"Could not uniquely resolve {leaf['label']!r} -> {cands}")
        ids = [str(a["id"]) for a in matches]
        accttypes = {a["accttype"] for a in matches}
        if len(accttypes) != 1:
            raise SystemExit(f"Mixed accttypes for {leaf['label']!r}: {matches}")
        resolved.append({**leaf, "ids": ids, "accttype": accttypes.pop(), "special": None})
    return resolved


def bootstrap_anchor_from_workbook(env, workbook_path, ym="2026-06"):
    """Build a valid anchor dict from the CFO workbook fixture -- zero owner 2FA needed.
    Production overwrites spike/config/bs_anchor.json from a live cr=-202 pull at each
    month close instead of calling this.

    Caveat (recorded in the returned dict's "notes"): "VAT on Sales" is FIVE distinct,
    identically-named, active live accounts (ids 526/529/532/535/538, all OthCurrLiab) that
    NetSuite's own report merges into one displayed line. The workbook gives one blended
    value for the group; this bootstrap assigns the full value to the lowest id (526) and
    0.0 to the other four, since the true per-account split is not recoverable from the
    workbook. A real cr=-202 pull does not have this problem -- it reports each account's
    actual balance directly."""
    if ym not in WORKBOOK_COLS.values():
        raise ValueError(f"ym {ym!r} not in this workbook's columns {sorted(WORKBOOK_COLS.values())}")
    leaves = read_workbook_leaves(workbook_path)
    resolved = resolve_workbook_leaves(env, leaves)

    balances = {}
    resolution_log = []
    for leaf in resolved:
        val = leaf["vals"].get(ym)
        if val is None:
            continue
        if leaf["special"] == "net_income":
            balances[SYNTHETIC_NET_INCOME_ID] = val
            resolution_log.append({"label": leaf["label"], "account_ids": [SYNTHETIC_NET_INCOME_ID], "note": "computed P&L rollup, not a GL account"})
            continue
        ids = sorted(leaf["ids"], key=int)
        if len(ids) == 1:
            balances[ids[0]] = val
            resolution_log.append({"label": leaf["label"], "account_ids": ids})
        else:
            balances[ids[0]] = val
            for other in ids[1:]:
                balances[other] = 0.0
            resolution_log.append({
                "label": leaf["label"], "account_ids": ids,
                "note": f"workbook shows one blended value for {len(ids)} identically-named live accounts; "
                        f"full value assigned to id {ids[0]}, 0.0 to the rest (bootstrap-only approximation)",
            })

    return {
        "ym": ym,
        "source": f"BOOTSTRAP from workbook fixture {workbook_path!r} (column for {ym}). "
                   f"NOT a production anchor -- production must overwrite this from a cr=-202 pull at each close.",
        "balances": balances,
        "resolution_log": resolution_log,
    }


# ---------------------------------------------------------------------------
# Production builder
# ---------------------------------------------------------------------------

def _row(aid, meta, ym, balance, method):
    return {
        "account_id": aid,
        "acctnumber": meta.get("acctnumber", ""),
        "account_name": meta.get("account_name", ""),
        "accttype": meta.get("accttype"),
        "parent_id": meta.get("parent_id"),
        "level": meta.get("level"),
        "path": meta.get("path"),
        "is_leaf": True,
        "ym": ym,
        "balance": balance,
        "method": method,
    }


def _synthetic_ni_row(ym, balance, method):
    return {
        "account_id": SYNTHETIC_NET_INCOME_ID,
        "acctnumber": "",
        "account_name": "Net Income (computed P&L rollup, not a GL account)",
        "accttype": "Equity",
        "parent_id": None,
        "level": None,
        "path": "synthetic/net_income",
        "is_leaf": True,
        "ym": ym,
        "balance": balance,
        "method": method,
    }


def build_bs_by_account_month(env, D, anchor):
    """D["asof"]: YYYY-MM-DD (yesterday MT). D["trailing_months"]: list of YYYY-MM (used
    only to sanity-check the window; the emitted months are driven by anchor["ym"] and
    D["asof"] directly, per contract). anchor: {"ym", "source", "balances": {account_id:
    balance}} -- "synthetic:net_income" is a valid key in balances.

    Returns (rows, nrows). Emits the anchor month (method="anchor") and every month AFTER
    it through the month containing D["asof"] (method="increment"; the open month's window
    is capped at D["asof"], not the full calendar month). Never emits a month before the
    anchor."""
    anchor_ym = anchor["ym"]
    asof = D["asof"]
    asof_ym = asof[:7]
    if asof_ym < anchor_ym:
        raise ValueError(f"D['asof']={asof!r} is before the anchor month {anchor_ym!r}")
    increment_months = [] if asof_ym == anchor_ym else _months_between(_next_ym(anchor_ym), asof_ym)

    accounts = fetch_accounts(env)  # id -> {acctnumber, account_name, accttype, parent_id, level, path}
    active_rows = suiteql(env, "SELECT id, isinactive FROM account")
    active_ids = {str(r["id"]) for r in active_rows if r.get("isinactive") == "F"}

    tracked_ids = {aid for aid, meta in accounts.items() if meta.get("accttype") in BS_TYPES and aid in active_ids}
    anchor_ids = {k for k in anchor["balances"] if k != SYNTHETIC_NET_INCOME_ID}
    for aid in anchor_ids - tracked_ids:
        if aid in accounts:
            tracked_ids.add(aid)  # anchored but inactive/type-changed since -- keep it, it has history
        else:
            print(f"WARNING: anchor account id {aid} not found in live chart of accounts; dropped", file=sys.stderr)
    tracked_ids &= set(accounts)  # drop any stray id extract_v2's fetch_accounts didn't resolve

    flow = {}
    net_income_flow = {}
    if increment_months:
        win_start, _ = _month_bounds(increment_months[0])
        win_end = asof
        id_list = ",".join(str(i) for i in sorted(int(x) for x in tracked_ids))
        flow_rows = suiteql(env, f"""
            SELECT ai.account AS aid, TO_CHAR(t.trandate,'YYYY-MM') AS ym, SUM(ai.amount) AS amt
            FROM transactionaccountingline ai
            JOIN transaction t ON t.id = ai.transaction
            WHERE ai.posting = 'T' AND ai.account IN ({id_list})
              AND t.trandate >= TO_DATE('{win_start}','YYYY-MM-DD')
              AND t.trandate <= TO_DATE('{win_end}','YYYY-MM-DD')
            GROUP BY ai.account, TO_CHAR(t.trandate,'YYYY-MM')
        """)
        for r in flow_rows:
            flow[(str(r["aid"]), r["ym"])] = fnum(r.get("amt"))

        pnl_rows = suiteql(env, f"""
            SELECT a.accttype AS ty, TO_CHAR(t.trandate,'YYYY-MM') AS ym, SUM(ai.amount) AS amt
            FROM transactionaccountingline ai
            JOIN transaction t ON t.id = ai.transaction
            JOIN account a ON a.id = ai.account
            WHERE ai.posting = 'T' AND a.accttype IN {_sql_in_strs(PNL_TYPES)}
              AND t.trandate >= TO_DATE('{win_start}','YYYY-MM-DD')
              AND t.trandate <= TO_DATE('{win_end}','YYYY-MM-DD')
            GROUP BY a.accttype, TO_CHAR(t.trandate,'YYYY-MM')
        """)
        pnl_by_ym = {}
        for r in pnl_rows:
            pnl_by_ym.setdefault(r["ym"], {})[r["ty"]] = fnum(r.get("amt"))
        for ym in increment_months:
            by_ty = pnl_by_ym.get(ym, {})
            net_income_flow[ym] = -sum(by_ty.get(t, 0.0) for t in PNL_TYPES)

    rows = []
    running = {aid: round(fnum(anchor["balances"].get(aid, 0.0)), 2) for aid in tracked_ids}
    for aid in sorted(tracked_ids):
        rows.append(_row(aid, accounts[aid], anchor_ym, running[aid], "anchor"))
    running_ni = round(fnum(anchor["balances"].get(SYNTHETIC_NET_INCOME_ID, 0.0)), 2)
    rows.append(_synthetic_ni_row(anchor_ym, running_ni, "anchor"))

    for ym in increment_months:
        for aid in sorted(tracked_ids):
            meta = accounts[aid]
            delta = sign(meta["accttype"]) * flow.get((aid, ym), 0.0)
            running[aid] = round(running[aid] + delta, 2)
            rows.append(_row(aid, meta, ym, running[aid], "increment"))
        running_ni = round(running_ni + net_income_flow.get(ym, 0.0), 2)
        rows.append(_synthetic_ni_row(ym, running_ni, "increment"))

    return rows, len(rows)


# ---------------------------------------------------------------------------
# Self-test: bootstrap the June anchor, verify it round-trips to the workbook, then roll
# forward into July and check the balance sheet foots (Assets == Liabilities + Equity).
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    WORKBOOK = Path(__file__).resolve().parents[1] / "inputs" / "cfo-july-financials-forecast-2026-08-27.xlsx"
    ANCHOR_OUT = Path(__file__).parent / "config" / "bs_anchor.json"

    env = load_env()

    anchor = bootstrap_anchor_from_workbook(env, WORKBOOK, ym="2026-06")
    ANCHOR_OUT.write_text(json.dumps(anchor, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {ANCHOR_OUT} ({len(anchor['balances'])} anchored accounts, ym={anchor['ym']})")

    # (a) June rows must equal the workbook to the cent (method="anchor")
    leaves = read_workbook_leaves(WORKBOOK)
    resolved = resolve_workbook_leaves(env, leaves)
    D_june = {"asof": "2026-06-30", "trailing_months": ["2026-06"]}
    rows_june, _ = build_bs_by_account_month(env, D_june, anchor)
    bal_by_id_june = {r["account_id"]: r["balance"] for r in rows_june if r["ym"] == "2026-06"}
    june_mismatches = []
    for leaf in resolved:
        wb_val = leaf["vals"].get("2026-06")
        if wb_val is None:
            continue
        if leaf["special"] == "net_income":
            got = bal_by_id_june.get(SYNTHETIC_NET_INCOME_ID)
        else:
            got = round(sum(bal_by_id_june.get(i, 0.0) for i in leaf["ids"]), 2)
        if got is None or abs(got - wb_val) > 0.01:
            june_mismatches.append((leaf["label"], wb_val, got))
    print(f"June anchor round-trip: {len(resolved) - len(june_mismatches)}/{len(resolved)} leaves tie to the cent")
    for label, wb_val, got in june_mismatches:
        print(f"  MISMATCH {label}: workbook={wb_val} rebuilt={got}")

    # (b) roll forward into July, check the foot, (c) confirm no crash on the net_income line
    D_july = {"asof": "2026-07-31", "trailing_months": ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]}
    rows_july, n_july = build_bs_by_account_month(env, D_july, anchor)
    july_rows = [r for r in rows_july if r["ym"] == "2026-07"]
    assets = round(sum(r["balance"] for r in july_rows if r["accttype"] in DIRECT_TYPES), 2)
    liab_eq = round(sum(r["balance"] for r in july_rows if r["accttype"] in NEGATE_TYPES), 2)
    ni_row = next(r for r in july_rows if r["account_id"] == SYNTHETIC_NET_INCOME_ID)
    print(f"net_income row for 2026-07 built without error: balance={ni_row['balance']}")
    print(f"July ({n_july} total rows, {len(july_rows)} at ym=2026-07): assets={assets:,.2f} "
          f"liab_eq={liab_eq:,.2f} diff={round(assets - liab_eq, 2):,.2f}")

    ok = not june_mismatches and abs(assets - liab_eq) <= 1.00
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
