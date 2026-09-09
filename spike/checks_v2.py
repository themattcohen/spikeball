"""checks_v2.py -- actual-only v2 self-checks, isolated from the live checks.py.

These are INFORMATIONAL: extract.py records them under meta.checks.v2 and sets a separate
meta.checks.v2_pass, but they do NOT feed meta.checks.all_pass. Rationale (PRD-v2 v0.6,
prod-safe split): a v2 section problem must never block the v1 dashboard the CFO already
relies on. The v1 checks (a-g, T5) remain the sole gate on publishing. When the owner wants
v2 sections to gate their own publication, promote the relevant checks then.

Each check returns {"pass": bool, "detail": str}. run_checks_v2(output) returns the dict
plus v2_pass = all(pass).
"""
from __future__ import annotations

TOL = 0.01


def _ok(detail):
    return {"pass": True, "detail": detail}


def _fail(detail):
    return {"pass": False, "detail": detail}


def check_i_gross_net_reconciles(output):
    """net_revenue in pnl_channel_gross_net equals pnl_by_channel_month.revenue for the
    same channel-month (both are Income-type by cseg_appf_channel)."""
    gn = output.get("pnl_channel_gross_net") or []
    pnl = output.get("pnl_by_channel_month") or []
    rev = {(str(r.get("channel_id")), r["ym"]): r["revenue"] for r in pnl}
    worst = 0.0
    worst_key = None
    n = 0
    for r in gn:
        key = (str(r.get("channel_id")), r["ym"])
        if key not in rev:
            continue
        d = abs(r["net_revenue"] - rev[key])
        n += 1
        if d > worst:
            worst, worst_key = d, key
    if worst > TOL:
        return _fail(f"net_revenue vs pnl_by_channel revenue: worst diff {worst:.2f} at {worst_key} over {n} channel-months")
    return _ok(f"net_revenue ties pnl_by_channel revenue on all {n} channel-months (worst {worst:.4f})")


def check_j_account_total_equals_channel(output):
    """Per month, the account-grain Income total equals the channel-grain net_revenue
    total (the two P&L grains foot to each other)."""
    pa = output.get("pnl_by_account_month") or []
    gn = output.get("pnl_channel_gross_net") or []
    income_types = {"Income"}  # channel net_revenue uses Income only; match it here
    acct_income = {}
    for r in pa:
        if r["accttype"] in income_types:
            acct_income[r["ym"]] = round(acct_income.get(r["ym"], 0.0) + r["amount"], 2)
    chan_net = {}
    for r in gn:
        chan_net[r["ym"]] = round(chan_net.get(r["ym"], 0.0) + r["net_revenue"], 2)
    worst = 0.0
    worst_ym = None
    for ym in set(acct_income) | set(chan_net):
        d = abs(acct_income.get(ym, 0.0) - chan_net.get(ym, 0.0))
        if d > worst:
            worst, worst_ym = d, ym
    if worst > TOL:
        return _fail(f"account Income total vs channel net_revenue: worst diff {worst:.2f} at {worst_ym}")
    return _ok(f"account Income total ties channel net_revenue on every month (worst {worst:.4f})")


def check_k_balance_sheet_balances(output):
    """Assets = Liabilities + Equity at each month-end in bs_by_account_month, within a
    tolerance that absorbs the open-month increment drift. Skipped if bs absent."""
    bs = output.get("bs_by_account_month") or []
    if not bs:
        return _ok("bs_by_account_month absent (V-D not in this run) -- skipped")
    # accttype sign: assets positive, liabilities+equity positive on their side; A - (L+E) ~ 0
    ASSET = {"Bank", "AcctRec", "OthCurrAsset", "FixedAsset", "OthAsset", "DeferExpense", "UnbilledRec"}
    LIABEQ = {"AcctPay", "CredCard", "OthCurrLiab", "LongTermLiab", "DeferRevenue", "Equity"}
    by_month = {}
    for r in bs:
        e = by_month.setdefault(r["ym"], {"a": 0.0, "le": 0.0})
        if r["accttype"] in ASSET:
            e["a"] += r["balance"]
        elif r["accttype"] in LIABEQ:
            e["le"] += r["balance"]
    worst = 0.0
    worst_ym = None
    for ym, e in by_month.items():
        d = abs(e["a"] - e["le"])
        if d > worst:
            worst, worst_ym = d, ym
    # 1.0 tolerance: balance sheet should foot; loosened only to absorb rounding, not drift
    if worst > 1.0:
        return _fail(f"assets != liabilities+equity: worst {worst:.2f} at {worst_ym}")
    return _ok(f"balance sheet foots on every month (worst {worst:.2f})")


def check_p_demand_plan_valid(output):
    """Demand Plan tab validity (research/09-cfo-input-mechanism-design.md Section 3).
    Informational only: recorded under meta.checks_v2 and its own demand_plan_ok flag,
    but feeds neither v2_pass (below) nor meta.checks.all_pass. A CFO-input gap (tab not
    yet seeded, a bad header, a mid-edit read) is a different class of problem than a
    NetSuite computation parity failure (checks i/j/k) and must never be conflated with
    one -- folding it into v2_pass would make every v2 section look broken the moment the
    CFO hasn't typed anything yet, which is the normal state before the tab is seeded."""
    meta = output.get("demand_plan_meta")
    if meta is None:
        return _ok("demand_plan_meta absent (Demand Plan not wired into this run)")
    if meta.get("valid"):
        return _ok(f"Demand Plan tab valid: {meta.get('row_count', 0)} row(s), "
                    f"{meta.get('sku_count', 0)} SKU(s), read_at_mt={meta.get('read_at_mt')}")
    reason = meta.get("reason") or "unknown"
    if meta.get("stale"):
        return _fail(f"Demand Plan read invalid this run ({reason}) -- showing the previous "
                      f"valid snapshot from {meta.get('read_at_mt')}, labeled stale")
    return _fail(f"Demand Plan read invalid this run ({reason}) -- no previous snapshot available")


def run_checks_v2(output):
    checks = {
        "i_gross_net_reconciles": check_i_gross_net_reconciles(output),
        "j_account_total_equals_channel": check_j_account_total_equals_channel(output),
        "k_balance_sheet_balances": check_k_balance_sheet_balances(output),
    }
    checks["v2_pass"] = all(v["pass"] for v in checks.values())
    p_result = check_p_demand_plan_valid(output)
    checks["p_demand_plan_valid"] = p_result
    checks["demand_plan_ok"] = p_result["pass"]
    return checks
