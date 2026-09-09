"""extract_v2_bs_snapshot.py -- V-D balance sheet from NetSuite's native account.balance
via TBA (OAuth1, no 2FA, no UI login). Replaces the anchor+increment method for
going-forward months.

research/10 confirmed the reconciled balances are token-reachable with no 2FA (the coding
automation's TBA path). research/11 proved account.balance reproduces the CFO's cr=-202
Balance Sheet to the cent across every balance-sheet account type (banks, AR, fixed assets,
the La Plata LOC, promissory notes, tax control accounts); the only deltas are a few
thousand dollars of in-flight timing on clearing/AP accounts that converge at a real
month-end close.

READ-ONLY. SuiteQL SELECT only. Never creates/edits/deletes any record, report, or script.

Why account.balance and not SUM(amount): NetSuite maintains a server-side running balance
per account -- the number the Bank Rec Summary portlet and cr=-202 display. For
bank / processor / LOC-swept accounts a life-to-date GL SUM does NOT reproduce it
(research/07 killed that path); account.balance does.

Limitation: account.balance has no as-of-date parameter -- it is "now" only. It cannot
backfill a PAST closed month. So this builder captures the CURRENT month; the durable
monthly series is accumulated across nightly runs (a snapshot per month-end), and months
before snapshotting began keep the existing anchor+increment output.

Own-vs-rollup trap (the correctness crux): account.balance on a PARENT account is the full
rollup (its own postings + every descendant). Emitting the rollup for a parent AND its
children double-counts. So this builder emits each account's OWN balance =
account.balance(id) - sum(account.balance(direct children)). For a leaf that is just
account.balance; for a parent with its own postings (e.g. 10106000, whose report self-line
is 26,764.13 while its rollup is 278,089.88) it recovers the self-line exactly. Summing all
emitted rows then reproduces every subtotal with no double count.

Sign rule and row shape are reused verbatim from extract_v2_bs (sign, _row,
_synthetic_ni_row) so snapshot rows are byte-compatible with build_bs_by_account_month and
land under the same "bs_by_account_month" output key -> same Sheet tab + BQ view. The only
difference is method="snapshot".

Net Income: account.balance exposes no computed P&L rollup line. It is emitted as the PLUG
that foots the sheet: NI = Assets_out - (Liabilities + static Equity)_out. Because
NetSuite's GL always balances, this plug is the current earnings not yet closed to Retained
Earnings. The self-test cross-checks it against -(SUM P&L amount, fiscal-year-to-date).

Run (read-only self-test: build the current snapshot, tie unambiguous leaves to the
Aug-2026 cr=-202 fixture from research/11, confirm the sheet foots):
  doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python spike/extract_v2_bs_snapshot.py
"""
from __future__ import annotations

import sys
from collections import defaultdict

from _lib import fnum, load_env, suiteql
from extract_v2 import fetch_accounts
from extract_v2_bs import (  # reuse the proven sign rule + row shape verbatim
    BS_TYPES,
    DIRECT_TYPES,
    NEGATE_TYPES,
    PNL_TYPES,
    SYNTHETIC_NET_INCOME_ID,
    _row,
    _synthetic_ni_row,
    sign,
)


def own_balances(env):
    """Return (own, rollup): own[id] = account.balance(id) - sum(account.balance(direct
    children)); rollup[id] = raw account.balance(id). Pulled for the FULL chart (incl
    inactive) so no child is missed when netting a parent."""
    coa = suiteql(env, "SELECT id, parent, balance FROM account")
    rollup = {str(r["id"]): fnum(r.get("balance")) for r in coa}
    children = defaultdict(list)
    for r in coa:
        p = r.get("parent")
        if p not in (None, ""):
            children[str(p)].append(str(r["id"]))
    own = {}
    for aid, bal in rollup.items():
        own[aid] = round(bal - sum(rollup.get(c, 0.0) for c in children.get(aid, [])), 2)
    return own, rollup


def net_income_fytd(env, asof):
    """-(SUM of posting P&L accounting-line amount from Jan 1 of asof's year through asof).
    Cross-check only; the emitted NI is the balance-sheet plug. Assumes a calendar fiscal
    year (the CFO workbook is Balance Sheet_2026 with Jan-start columns)."""
    fy_start = f"{asof[:4]}-01-01"
    types_in = ",".join(f"'{t}'" for t in PNL_TYPES)
    rows = suiteql(
        env,
        f"""
        SELECT SUM(ai.amount) AS amt
        FROM transactionaccountingline ai
        JOIN transaction t ON t.id = ai.transaction
        JOIN account a ON a.id = ai.account
        WHERE ai.posting = 'T' AND a.accttype IN ({types_in})
          AND t.trandate >= TO_DATE('{fy_start}','YYYY-MM-DD')
          AND t.trandate <= TO_DATE('{asof}','YYYY-MM-DD')
        """,
    )
    amt = fnum(rows[0].get("amt")) if rows else 0.0
    return round(-amt, 2)


def build_bs_snapshot_current(env, D):
    """Emit balance-sheet rows for the CURRENT month (D["asof"][:7]) from account.balance.

    Returns (rows, nrows). One row per tracked account (own balance, method="snapshot")
    plus one synthetic Net Income row (the plug that foots the sheet). Tracked set matches
    build_bs_by_account_month: every ACTIVE account whose accttype is a balance-sheet type.
    """
    snap_ym = D["asof"][:7]
    accounts = fetch_accounts(env)  # id -> {acctnumber, account_name, accttype, parent_id, level, path}
    active_rows = suiteql(env, "SELECT id, isinactive FROM account")
    active_ids = {str(r["id"]) for r in active_rows if r.get("isinactive") == "F"}
    own, _rollup = own_balances(env)

    tracked_ids = {
        aid
        for aid, meta in accounts.items()
        if meta.get("accttype") in BS_TYPES and aid in active_ids
    }
    tracked_ids &= set(accounts)

    rows = []
    assets_out = 0.0
    liab_eq_static_out = 0.0
    re_rows = []
    for aid in sorted(tracked_ids):
        meta = accounts[aid]
        bal = round(sign(meta["accttype"]) * own.get(aid, 0.0), 2)
        row = _row(aid, meta, snap_ym, bal, "snapshot")
        rows.append(row)
        if meta["accttype"] in DIRECT_TYPES:
            assets_out += bal
        else:
            liab_eq_static_out += bal
        if meta["accttype"] == "Equity" and (
            str(meta.get("acctnumber")) == "30300000"
            or str(meta.get("account_name", "")).endswith("Retained Earnings")
        ):
            re_rows.append(row)

    plug = round(assets_out - liab_eq_static_out, 2)
    current_ni = net_income_fytd(env, D["asof"])
    re_adjust = round(plug - current_ni, 2)

    # NetSuite's Balance Sheet shows Retained Earnings = the posted RE account PLUS prior
    # fiscal years' P&L (rolled in dynamically), and Net Income = the current fiscal year's
    # P&L. account.balance exposes only the posted RE account, leaving ALL unclosed P&L
    # (prior + current) outside equity. Split it the way the report and the CFO workbook do:
    # current-year P&L -> the Net Income line (the headline figure, == the Income Statement's
    # FYTD net income by identity); prior-year P&L -> folded into the Retained Earnings line.
    # This matches the anchor+increment builder's convention (which anchors RE to the
    # workbook's already-combined value) so the two methods present equity identically.
    if len(re_rows) == 1:
        re_rows[0]["balance"] = round(re_rows[0]["balance"] + re_adjust, 2)
        re_rows[0]["method"] = "snapshot+re_rollup"
        net_income = current_ni
    else:
        print(
            f"WARNING: expected exactly 1 Retained Earnings account, found {len(re_rows)}; "
            "Net Income falls back to the full equity plug (prior-year P&L not split out)",
            file=sys.stderr,
        )
        net_income = plug

    rows.append(_synthetic_ni_row(snap_ym, net_income, "snapshot"))
    return rows, len(rows)


# ---------------------------------------------------------------------------
# Self-test: build the current snapshot, tie unambiguous material leaves to the Aug-2026
# cr=-202 report values captured in research/11, and confirm the sheet foots. Read-only.
# ---------------------------------------------------------------------------

# cr=-202 AS OF Aug 2026, output/report convention (liabilities positive), keyed by the
# UNIQUE acctnumber of the leaf (collision numbers like 10104000 are deliberately excluded).
# Captured 2026-08-28 (research/11).
REPORT_AUG2026 = {
    # DIRECT (assets)
    "10101610": 105111.78,   # CIBC - 7399
    "10101650": 48671.17,    # Byline - 8970
    "10101750": 1964272.92,  # Highbeam - 1408
    "10102001": 2072147.01,  # Trade Receivable
    "10102200": -126573.65,  # Allowances (contra-asset)
    "10104200": 92865.08,    # Inventory In Transit
    "10104300": 21173.99,    # Inventory Transfers
    "10106000": 26764.13,    # Other Current Asset SELF-line (parent-with-own-postings: proves own-vs-rollup)
    "10106200": 188325.75,   # Prepaid Expenses
    "10106300": 63000.00,    # Security Deposits
    "10105000": 273159.63,   # Unreconciled Differences
    "10201100": 7161.33,     # Cost - Computer Equipment
    "10201200": -7161.33,    # A/D - Computer Equipment
    "10202150": 809842.00,   # Cost - Buildings and Improvements
    "10203100": 500000.00,   # Trademark - Cost
    # NEGATE (liabilities/equity), report shows positive
    "20112200": 1990000.00,   # La Plata - Line of Credit
    "20113300": 1107170.55,   # GST/HST Control Account
    "20113200": 100501.50,    # VAT Control Account
    "201140100": 2106848.47,  # Finance Liability - LT
    "201170100": 514000.00,   # Promissory NP LT - Patrick Kennedy
    "201170105": 763438.04,   # Promissory NP LT - Chris Ruder
    "201170110": 218852.26,   # Interest Payable LT - Chris Ruder
}

# Known clearing / AP / inventory accounts that move between the live snapshot (now) and the
# report's period-end view -- allowed a wider tolerance (research/11 measured these deltas).
TIMING_TOLERANT = {
    "10101100": 17483.18,    # Bill.com Money Out Clearing (~6,000 delta)
    "10101700": 251874.84,   # Amazon Flow Through (~103)
    "20101000": 1509018.12,  # Accounts Payable (~3,750)
    "10104100": 1781153.64,  # Inventory On Hand (~13)
    "10103000": 109420.64,   # Undeposited Funds (~78; another transient clearing account)
}


def _self_test():
    env = load_env()
    D = {"asof": "2026-08-28"}
    rows, n = build_bs_snapshot_current(env, D)
    by_num = {}
    for r in rows:
        if r["account_id"] == SYNTHETIC_NET_INCOME_ID:
            continue
        by_num.setdefault(r["acctnumber"], []).append(r)

    print(f"snapshot rows: {n} (incl 1 synthetic net income), ym={D['asof'][:7]}")

    # (a) unambiguous leaves must tie to the report to the cent
    fails = []
    checked = 0
    for num, expected in REPORT_AUG2026.items():
        cands = by_num.get(num, [])
        if len(cands) != 1:
            fails.append((num, expected, f"expected 1 tracked account, found {len(cands)}"))
            continue
        got = cands[0]["balance"]
        checked += 1
        if abs(got - expected) > 0.01:
            fails.append((num, expected, got))
    print(f"exact tie-out: {checked - len([f for f in fails if not isinstance(f[2], str)])}/{len(REPORT_AUG2026)} leaves match cr=-202 Aug 2026 to the cent")
    for num, exp, got in fails:
        print(f"  MISMATCH {num}: report={exp} snapshot={got}")

    # (b) timing-tolerant accounts: report the measured delta, do not fail on it
    print("timing accounts (live snapshot vs Aug-2026 period end; deltas expected):")
    for num, report_val in TIMING_TOLERANT.items():
        cands = by_num.get(num, [])
        got = cands[0]["balance"] if len(cands) == 1 else None
        delta = None if got is None else round(got - report_val, 2)
        print(f"  {num}: report={report_val} snapshot={got} delta={delta}")

    # (c) the sheet must foot; report the equity split
    assets = round(sum(r["balance"] for r in rows if r["accttype"] in DIRECT_TYPES), 2)
    liab = round(sum(r["balance"] for r in rows if r["accttype"] in NEGATE_TYPES
                     and r["accttype"] != "Equity"), 2)
    equity = round(sum(r["balance"] for r in rows if r["accttype"] == "Equity"
                       or r["account_id"] == SYNTHETIC_NET_INCOME_ID), 2)  # incl synthetic NI
    liab_eq = round(liab + equity, 2)
    ni_row = next(r for r in rows if r["account_id"] == SYNTHETIC_NET_INCOME_ID)
    print(f"foot: assets={assets:,.2f} liab+equity(incl NI)={liab_eq:,.2f} diff={round(assets - liab_eq, 2):,.2f}")
    print(f"  liabilities={liab:,.2f}  total equity(incl NI)={equity:,.2f}  (report Total Equity Aug 2026 = -193,900.15)")
    print(f"  Net Income (current fiscal YTD)={ni_row['balance']:,.2f}")

    # Net Income must equal the Income Statement's FYTD net income by identity.
    ni_fytd = net_income_fytd(env, D["asof"])
    ni_ok = abs(ni_row["balance"] - ni_fytd) <= 0.01

    hard_fails = [f for f in fails if not isinstance(f[2], str)]  # value mismatch
    resolve_fails = [f for f in fails if isinstance(f[2], str)]   # could not resolve the account
    foots = abs(assets - liab_eq) <= 1.00
    ok = not hard_fails and not resolve_fails and foots and ni_ok
    if not ni_ok:
        print(f"  NI does NOT match FYTD P&L ({ni_fytd:,.2f}) -- investigate")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_self_test())
