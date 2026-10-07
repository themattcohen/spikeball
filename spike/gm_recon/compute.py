"""Turns raw NetSuite rows (and the optional Amazon orders cache) into the monthly
gross-margin reconciliation data model plus the detail tables the workbook writes.

Two entry points:
- `fetch_raw(env, month, asof, cfg)`: every NetSuite read, through ns_queries (SELECT
  only). Returns a dict of raw row lists.
- `build(month, asof, raw, cfg, cache_orders, cache_info, meta)`: pure. Returns
  (model, details). `model` is the JSON contract written to summary_*.json; `details`
  holds one list of row dicts per detail sheet.

Sign conventions inside this module: `income` is credit positive, `cogs` debit
positive, `deduction` (a retailer bill line on an Income account) debit positive. An
effect is the change to apply to the month's booked income or COGS.
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPIKE = _HERE.parent
for _p in (_SPIKE, _SPIKE / "routine", _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ns_queries  # noqa: E402
import rules  # noqa: E402
from rules import num, r2  # noqa: E402

PO_RE = re.compile(r"PO\d+")


# ---------------------------------------------------------------------------
# Fetch (NetSuite, read-only)
# ---------------------------------------------------------------------------

def fetch_raw(env, month: str, asof: date, cfg: dict, log=print) -> dict:
    S, E = rules.month_bounds(month)
    A = asof
    me = min(E, A)
    creator = cfg["settlement_journal_creator"]
    win = cfg["fee_timing_window_days_before"]
    raw: dict = {}

    def step(name, fn, *a):
        rows = fn(env, *a)
        log(f"[gm_recon] fetched {name}: {len(rows)} rows")
        raw[name] = rows
        return rows

    step("pnl", ns_queries.pnl_by_account, S, A)
    summ = step("summaries", ns_queries.settlement_summaries, S - timedelta(days=win), A)
    step("settlement_journal_lines", ns_queries.settlement_journal_lines, creator, S - timedelta(days=win), A)
    rows = step("settlement_rows", ns_queries.settlement_rows, [s["id"] for s in summ]) if summ else []
    raw.setdefault("settlement_rows", rows)
    oids = sorted({r["o"] for r in rows if r.get("o")})
    step("legacy_invoices", ns_queries.legacy_invoice_dates, oids,
         A - timedelta(days=cfg["legacy_invoice_lookback_days"]), A)
    mj = step("manual_journal_lines", ns_queries.manual_journal_lines, creator,
              S - timedelta(days=cfg["accrual_reversal_window_days"]), A, [cfg["accounts"]["accrued_misc"]])
    have = {r["tid"] for r in mj}
    partners = sorted({r["rev_id"] for r in mj if r.get("rev_id")} - have)
    if partners:
        step("journal_partners", ns_queries.journal_headers, partners, S - timedelta(days=400), A)
    else:
        raw["journal_partners"] = []
    step("late_created", ns_queries.late_created_lines, S, A)
    step("credit_memos", ns_queries.amazon_credit_memos, S, me)
    dinv = step("dup_invoices", ns_queries.duplicate_legacy_documents, "CustInvc", S, me)
    dcm = step("dup_credit_memos", ns_queries.duplicate_legacy_documents, "CustCred", S, me)
    raw["dup_invoice_docs"] = (ns_queries.documents_for_orders(env, "CustInvc", [r["oid"] for r in dinv], S, me)
                               if dinv else [])
    raw["dup_cm_docs"] = (ns_queries.documents_for_orders(env, "CustCred", [r["oid"] for r in dcm], S, me)
                          if dcm else [])
    step("consolidated_invoices", ns_queries.consolidated_invoices, S, A)
    step("legacy_invoice_revenue", ns_queries.legacy_invoice_revenue, S, A)
    wcfg = cfg["wengo"]
    wb = step("wengo_bills", ns_queries.wengo_bills, wcfg["vendor_pattern"],
              S - timedelta(days=wcfg["rate_lookback_days"]), A)
    refs = sorted({p for b in wb for p in PO_RE.findall(b.get("memo") or "")})
    if refs:
        step("wengo_po_lines", ns_queries.purchase_orders_by_tranid, refs,
             S - timedelta(days=wcfg["po_lookback_days"]), A)
    else:
        raw["wengo_po_lines"] = []
    step("month_po_lines", ns_queries.purchase_orders_by_vendor, wcfg["po_vendor_pattern"], S, me)
    step("retailer_lines", ns_queries.retailer_income_lines, S, A)
    return raw


# ---------------------------------------------------------------------------
# Build (pure)
# ---------------------------------------------------------------------------

def _pd(s):
    return rules.parse_ns_date(s)


def _wd(x):
    return rules.whole_dollars(x)


class _Ctx:
    def __init__(self, month, asof, cfg):
        self.month = month
        self.S, self.E = rules.month_bounds(month)
        self.A = asof
        self.cfg = cfg
        self.next_first = self.E + timedelta(days=1)
        self.prev_month = rules.add_months(month, -1)
        self.label = rules.month_label(month)
        self.limitations: list[str] = []
        self.fine: list[str] = []
        self.errors: list[dict] = []
        self.timing: list[dict] = []
        self.review: list[dict] = []
        self.entries: list[dict] = []
        self.details: dict = {}


def _pnl(ctx, raw):
    rows = []
    inc = cogs = 0.0
    for r in raw["pnl"]:
        amt = num(r.get("amt"))
        signed = -amt if r["typ"] == "Income" else amt
        rows.append({"acct": r["acct"], "name": r.get("name") or "", "type": r["typ"], "amount": r2(signed)})
        if r["typ"] == "Income":
            inc += signed
        else:
            cogs += signed
    ctx.details["pnl"] = rows
    gm = rules.gm_pct(inc, cogs)
    return {"income": r2(inc), "cogs": r2(cogs), "gm_pct": gm,
            "by_account": [{"acct": r["acct"], "name": r["name"], "type": r["type"], "amount": r["amount"]} for r in rows]}


def _journals(raw, cfg):
    by: dict = {}
    for l in raw["settlement_journal_lines"]:
        j = by.setdefault(l["tranid"], {"tid": l["tid"], "d": _pd(l["d"]), "lines": []})
        j["lines"].append(l)
    for t, j in by.items():
        j.update(rules.summarize_journal(j["lines"], cfg["memo"]))
    return by


def _settlements(ctx, raw, journals):
    cfg = ctx.cfg
    fx = cfg["fx_to_usd"]
    mk_of = cfg["settlement_account_marketplace"]
    rows_fee = defaultdict(float)
    for r in raw["settlement_rows"]:
        if str(r.get("ty")) in ("1", "2"):
            rows_fee[str(r["sm"])] += -num(r.get("fee"))
    sets = []
    for s in raw["summaries"]:
        dd = _pd(s.get("dd"))
        if dd is None or dd > ctx.A:
            continue
        sets.append({"id": str(s["id"]), "dd": dd, "total_amt": num(s.get("total_amt")), "raw": s,
                     "currency": s.get("cur") or "USD", "rows_fee_local": rows_fee[str(s["id"])]})
    mapping, maprule = rules.map_settlements_to_journals(sets, journals, cfg["journal_match_bank_tolerance"], fx)
    mapped = set(mapping.values())
    ctx.unmapped_journals = sorted(t for t, j in journals.items()
                                   if t not in mapped and ctx.S <= j["d"] <= min(ctx.E, ctx.A)
                                   and (abs(j["bank"]) >= 0.005 or abs(j["feeall"]) >= 0.005))
    out = []
    for s in sorted(sets, key=lambda x: (x["dd"], x["id"])):
        raw_s = s["raw"]
        cur = raw_s.get("cur") or "USD"
        rate = rules.fx_rate(cur, fx)
        je = mapping.get(s["id"])
        j = journals.get(je) if je else None
        rows_usd = rows_fee[s["id"]] * rate
        feeall = j["feeall"] if j else 0.0
        status = rules.fee_status(feeall, rows_usd, j is not None, cfg["fee_full_ratio_tolerance"])
        booked_month = rules.month_key(j["d"] if j else s["dd"])
        out.append({
            "sid": s["id"], "settl_id": raw_s.get("settl_id") or "", "marketplace": mk_of.get(str(raw_s.get("acco")), "?"),
            "currency": cur, "deposit": s["dd"], "deposit_amount": num(raw_s.get("total_amt")),
            "rows_fee_local": r2(rows_fee[s["id"]]), "fx": rate, "rows_fee_usd": r2(rows_usd),
            "journal": je or "", "journal_date": j["d"] if j else None, "booked_month": booked_month,
            "feeall": r2(feeall), "fee5": r2(j["fee5"]) if j else 0.0, "fee_outside": r2(j["fee_outside"]) if j else 0.0,
            "variance": r2(j["var"]) if j else 0.0, "bank": r2(j["bank"]) if j else 0.0,
            "bank_accounts": j["bank_accounts"] if j else [], "fee_accounts": j["fee_accounts"] if j else {},
            "status": status, "unbooked": r2(rows_usd - feeall), "error_id": "", "map_rule": maprule.get(s["id"], ""),
            "in_month": "Y" if booked_month == ctx.month else "N",
        })
    return out


def _e1_e4(ctx, setl, journals):
    cfg = ctx.cfg
    acc = cfg["accounts"]
    in_m = [s for s in setl if s["in_month"] == "Y"]
    fee_acct = rules.choose_fee_cogs_account([s["fee_accounts"] for s in in_m if s["status"] == "full"],
                                             acc["fee_cogs_default"])
    ctx.fee_acct = fee_acct
    fee_name = acc["fee_cogs_names"].get(fee_acct, "")
    flow = acc["amazon_flow_through"]
    flow_name = acc["amazon_flow_through_name"]
    n = 0
    for s in in_m:
        if s["status"] == "full":
            continue
        n += 1
        eid = f"E1-{n}"
        s["error_id"] = eid
        amt = s["unbooked"]
        rec = (f"{s['journal']} ({rules.fmt_md(s['journal_date'])})" if s["journal"]
               else f"no journal, deposit {rules.fmt_md(s['deposit'])}") + f", settlement {s['settl_id']} {s['marketplace']}"
        if s["status"] == "no_journal":
            banks = Counter(b for o in setl if o["marketplace"] == s["marketplace"] for b in o["bank_accounts"])
            bank = banks.most_common(1)[0][0] if banks else "the marketplace's bank account"
            entry = (f"Book the settlement journal: deposit to {bank}, fees to {fee_acct} {fee_name}. "
                     f"Fee part: debit {fee_acct} {_wd(amt)}, credit {flow} {flow_name} {_wd(amt)}.")
            what = f"Settlement deposited {rules.fmt_md(s['deposit'])} ({s['marketplace']}) has no settlement journal"
            date_ = s["deposit"]
        else:
            if amt >= 0:
                entry = f"Debit {fee_acct} {fee_name} {_wd(amt)}, credit {flow} {flow_name} {_wd(amt)}."
            else:
                entry = f"Debit {flow} {flow_name} {_wd(-amt)}, credit {fee_acct} {fee_name} {_wd(-amt)}."
            what = ("Settlement journal has no Amazon fee lines" if s["status"] == "none"
                    else "Settlement journal fee lines do not match the settlement rows")
            date_ = s["journal_date"]
        why = (f"Settlement rows carry {_wd(s['rows_fee_usd'])} of fees; the journal books {_wd(s['feeall'])}. "
               f"The gap sits in {flow} instead of COGS.")
        ctx.errors.append({"id": eid, "driver": "E1", "what": what, "records": rec, "amount": r2(amt),
                           "income_effect": 0.0, "cogs_effect": r2(amt), "entry": entry, "why": why,
                           "confidence": "measured"})
        _entry_pair(ctx, date_, fee_acct, flow, amt, f"Amazon fees, settlement {s['settl_id']}", eid)
    # E4: fee lines on non-COGS accounts in settlement journals dated in the month, one
    # error per account (journals listed), one entry pair per journal and account.
    fl_rows = []
    groups = defaultdict(float)
    meta = {}
    for t, j in journals.items():
        if not (ctx.S <= j["d"] <= min(ctx.E, ctx.A)):
            continue
        for l in j["lines"]:
            if (l.get("lmemo") or "").startswith(cfg["memo"]["fee_line_prefix"]) and not str(l["acct"]).startswith("5"):
                key = (l["acct"], t)
                groups[key] += num(l.get("dr")) - num(l.get("cr"))
                meta[key] = (j["d"], l.get("acctname") or "", l.get("atype") or "")
    by_acct: dict = {}
    for (acct, t), amt in sorted(groups.items(), key=lambda kv: (kv[0][0], meta[kv[0]][0], kv[0][1])):
        if abs(amt) >= 0.005:
            by_acct.setdefault(acct, []).append((t, amt))
    # Only expense-side accounts (config error_account_prefixes, "6") are E4 errors. Fee
    # lines on the accepted accounts (40105000 shipping chargebacks, by design per the
    # full-year review) and on any other non-COGS account are reported, not reclassed.
    fo = cfg["fee_lines_outside_cogs"]
    err_prefixes = tuple(fo["error_account_prefixes"])
    accepted = fo["accepted_accounts"]
    k = 0
    for acct, items in sorted(by_acct.items()):
        nm, atype = meta[(acct, items[0][0])][1], meta[(acct, items[0][0])][2]
        tot = sum(a for _, a in items)
        short = nm.split(" : ")[-1]
        if not acct.startswith(err_prefixes):
            treatment = "accepted, not reclassed" if acct in accepted else "not an error account; review"
            for t, amt in items:
                fl_rows.append({"journal": t, "date": meta[(acct, t)][0], "account": acct, "account_name": nm,
                                "account_type": atype, "amount": r2(amt), "treatment": treatment, "error_id": ""})
            if acct in accepted:
                ctx.limitations.append(f"Amazon fee lines on {acct} {short} total {_wd(tot)} ({accepted[acct]}).")
            else:
                ctx.limitations.append(f"Amazon fee lines on {acct} {short} total {_wd(tot)}; the account is neither "
                                       f"COGS, expense nor on the accepted list in the config. Not reclassed.")
            continue
        k += 1
        eid = f"E4-{k}"
        for t, amt in items:
            d = meta[(acct, t)][0]
            fl_rows.append({"journal": t, "date": d, "account": acct, "account_name": nm, "account_type": atype,
                            "amount": r2(amt), "treatment": "reclass to COGS", "error_id": eid})
            _entry_pair(ctx, d, fee_acct, acct, amt, f"Reclass Amazon fee lines from {acct}", eid)
        recs = ", ".join(f"{t} ({rules.fmt_md(meta[(acct, t)][0])})" for t, _ in items)
        ctx.errors.append({
            "id": eid, "driver": "E4", "what": f"Amazon fee lines posted outside COGS to {acct} {short}",
            "records": recs[:230], "amount": r2(tot), "income_effect": 0.0, "cogs_effect": r2(tot),
            "entry": f"Debit {fee_acct} {fee_name} {_wd(tot)}, credit {acct} {_wd(tot)}.",
            "why": "Amazon selling fees are cost of sales, not operating expense.",
            "confidence": "measured"})
    ctx.details["fee_lines_outside"] = fl_rows
    full_n = sum(1 for s in in_m if s["status"] == "full")
    if in_m and full_n == len(in_m):
        ctx.fine.append(f"All {full_n} settlement journals booked in {ctx.label} carry fee lines that match the settlement rows.")
    if k == 0:
        ctx.fine.append(f"No Amazon fee lines in {ctx.label} settlement journals post to expense accounts.")
    accepted_present = {r["account"] for r in fl_rows if r["treatment"] == "accepted, not reclassed"}
    for acct in accepted:
        if acct not in accepted_present:
            ctx.fine.append(f"No Amazon fee lines on {acct} in {ctx.label} ({accepted[acct]}).")
    return fee_acct


def _entry_pair(ctx, d, debit_acct, credit_acct, amt, memo, src):
    if abs(amt) < 0.005:
        return
    if amt < 0:
        debit_acct, credit_acct, amt = credit_acct, debit_acct, -amt
    ctx.entries.append({"date": d, "account": debit_acct, "debit": r2(amt), "credit": 0.0, "memo": memo, "source": src})
    ctx.entries.append({"date": d, "account": credit_acct, "debit": 0.0, "credit": r2(amt), "memo": memo, "source": src})


def _manual(ctx, raw):
    """Groups manual journal lines and classifies them. Returns (journals_by_tid,
    accrual journal rows)."""
    cfg = ctx.cfg
    J: dict = {}
    for l in raw["manual_journal_lines"]:
        j = J.setdefault(l["tid"], {"tid": l["tid"], "tranid": l["tranid"], "d": _pd(l["d"]), "cb": l.get("cb") or "",
                                    "hmemo": l.get("hmemo") or "", "rev_id": l.get("rev_id"),
                                    "isrev": l.get("isrev") == "T", "lines": []})
        j["lines"].append(l)
    names = {t: j["tranid"] for t, j in J.items()}
    for h in raw.get("journal_partners", []):
        names[h["tid"]] = h["tranid"]
    pointed_at = {j["rev_id"] for j in J.values() if j.get("rev_id")}
    for h in raw.get("journal_partners", []):
        if h.get("rev_id"):
            pointed_at.add(h["rev_id"])

    def pl_net(j):
        return rules.journal_net_by_side(j)[0]

    win = cfg["accrual_reversal_window_days"]
    rows = []
    acc_rows = []
    rv = 0
    accrued = cfg["accounts"]["accrued_misc"]
    for t, j in sorted(J.items(), key=lambda kv: (kv[1]["d"], kv[1]["tranid"])):
        has_pl = any(l.get("atype") in ("Income", "COGS") for l in j["lines"])
        link = bool(j.get("rev_id")) or t in pointed_at
        net = pl_net(j)
        opposite = any(o is not j and abs(pl_net(o) + net) < 0.005 and abs(net) >= 0.005
                       and abs((o["d"] - j["d"]).days) <= win for o in J.values())
        cls = rules.classify_manual_journal(j, link, opposite, cfg["manual_journal"])
        j["class"] = cls
        partner = names.get(j.get("rev_id"), "") if j.get("rev_id") else ""
        cogs_net = sum(num(l.get("dr")) - num(l.get("cr")) for l in j["lines"] if l.get("atype") == "COGS")
        has_accr = any(l.get("acct") == accrued for l in j["lines"])
        memo_all = (j["hmemo"] + " " + " ".join(l.get("lmemo") or "" for l in j["lines"])).lower()
        if has_accr and abs(cogs_net) >= 0.005 and any(w in memo_all for w in cfg["fee_accrual_memo_words"]):
            acc_rows.append({"journal": j["tranid"], "date": j["d"], "memo": j["hmemo"], "cogs_net": r2(cogs_net),
                             "reversal_partner": partner, "is_reversal": "Y" if j["isrev"] else "N",
                             "in_month": "Y" if ctx.S <= j["d"] <= min(ctx.E, ctx.A) else "N", "review_id": ""})
        if not (ctx.S <= j["d"] <= min(ctx.E, ctx.A)) or not has_pl:
            continue
        rid = ""
        if cls in rules.REVIEW_CLASSES:
            rv += 1
            rid = f"R1-{rv}"
            inc_cogs = sum(num(l.get("dr")) - num(l.get("cr")) for l in j["lines"] if l.get("atype") in ("Income", "COGS"))
            counter = sorted({l["acct"] for l in j["lines"] if l.get("atype") not in ("Income", "COGS")})
            ctx.review.append({
                "id": rid, "driver": "R1", "what": f"Manual journal on income or COGS, class {cls.replace('_', ' ')}",
                "records": f"{j['tranid']} ({rules.fmt_md(j['d'])}, {j['cb']})", "amount": r2(inc_cogs),
                "note": (rules.review_question(cls) + (f" Other side: {', '.join(counter)}." if counter else ""))[:230]})
        for l in j["lines"]:
            side = "P&L" if l.get("atype") in ("Income", "COGS") else "counterpart"
            rows.append({"journal": j["tranid"], "date": j["d"], "created_by": j["cb"], "memo": j["hmemo"],
                         "account": l.get("acct"), "account_name": l.get("acctname") or "", "type": l.get("atype") or "",
                         "debit": r2(num(l.get("dr"))), "credit": r2(num(l.get("cr"))), "side": side,
                         "class": cls, "reversal_partner": partner, "review_id": rid,
                         "pl_net": r2(-num(l.get("dr")) + num(l.get("cr"))) if side == "P&L" else 0.0})
    ctx.details["manual_journals"] = rows
    ctx.details["accrual_journals"] = acc_rows
    n_j = len({r["journal"] for r in rows})
    if n_j and rv == 0:
        ctx.fine.append(f"{n_j} manual journals touch income or COGS in {ctx.label}; none needs a controller ruling.")
    elif n_j == 0:
        ctx.fine.append(f"No manual journals touch income or COGS in {ctx.label}.")
    return J, acc_rows


def _late(ctx, raw):
    per: dict = {}
    for r in raw["late_created"]:
        p = per.setdefault(r["tid"], {"tranid": r["tranid"], "type": r["ttype"], "date": _pd(r["d"]),
                                      "created": _pd(r.get("cdate")), "created_by": r.get("cb") or "",
                                      "income": 0.0, "cogs": 0.0})
        amt = num(r.get("amt"))
        if r["atype"] == "Income":
            p["income"] += -amt
        else:
            p["cogs"] += amt
    rows = sorted(per.values(), key=lambda x: (-(abs(x["income"]) + abs(x["cogs"])), x["tranid"]))
    for r in rows:
        r["income"], r["cogs"] = r2(r["income"]), r2(r["cogs"])
    ctx.details["late_created"] = rows
    if rows:
        top = ", ".join(f"{r['tranid']} ({r['type']}, {_wd(abs(r['income']) + abs(r['cogs']))})" for r in rows[:3])
        inc = sum(r["income"] for r in rows)
        cg = sum(r["cogs"] for r in rows)
        ctx.lim_short = (f"{len(rows)} entries affecting {ctx.label} were created after month end through "
                         f"{rules.fmt_mdy(ctx.A)}, so a run on another date shows a different month.")
        ctx.limitations.append(
            f"{len(rows)} entries affecting {ctx.label} were created after month end through {rules.fmt_mdy(ctx.A)} "
            f"(income {_wd(inc)}, COGS {_wd(cg)}); the largest are {top}. A run on an earlier date shows a different month.")
    return rows


def _fee_timing(ctx, raw, setl, acc_rows, cache_orders):
    cfg = ctx.cfg
    fx = cfg["fx_to_usd"]
    by_sid = {s["sid"]: s for s in setl}
    legacy = {}
    for r in raw["legacy_invoices"]:
        d = _pd(r.get("d"))
        if r.get("oid") and d and (r["oid"] not in legacy or d < legacy[r["oid"]]):
            legacy[r["oid"]] = d
    cache_mt = {k: v["purchase_mt_date"] for k, v in (cache_orders or {}).items() if v.get("purchase_mt_date")}
    agg = defaultdict(float)
    settled_order_fee = defaultdict(float)
    settled_orders_mk: dict = {}
    fee_order_ids = set()
    src_count = Counter()
    for r in raw["settlement_rows"]:
        ty = str(r.get("ty"))
        s = by_sid.get(str(r.get("sm")))
        if ty not in ("1", "2") or s is None:
            continue
        fee = -num(r.get("fee")) * rules.fx_rate(s["currency"], fx)
        oid = r.get("o") or ""
        sm, src = rules.sales_month(oid, legacy, cache_mt, _pd(r.get("pd")))
        src_count[src] += 1
        agg[(s["sid"], sm, src)] += fee
        if ty == "1" and oid:
            fee_order_ids.add(oid)
            if sm == ctx.month:
                settled_order_fee[oid] += fee
                settled_orders_mk[oid] = s["marketplace"]
    rows = []
    for (sid, sm, src), fee in sorted(agg.items(), key=lambda kv: (by_sid[kv[0][0]]["deposit"], kv[0])):
        s = by_sid[sid]
        booked = s["booked_month"] == ctx.month
        belongs = sm == ctx.month
        if not booked and not belongs:
            bucket = "other months"
        elif booked and belongs:
            bucket = f"{ctx.label} sales settled in {ctx.label}"
        elif belongs:
            bucket = f"{ctx.label} sales settled after month end"
        elif sm < ctx.month:
            bucket = "earlier sales settled in the month"
        else:
            bucket = "later sales settled in the month"
        rows.append({"sid": sid, "settl_id": s["settl_id"], "marketplace": s["marketplace"], "deposit": s["deposit"],
                     "booked_month": s["booked_month"], "sales_month": sm, "source": src, "fee_usd": r2(fee),
                     "booked_in_month": "Y" if booked else "N", "belongs_to_month": "Y" if belongs else "N",
                     "bucket": bucket})
    ctx.details["fee_timing"] = rows
    booked_rows = sum(r["fee_usd"] for r in rows if r["booked_in_month"] == "Y")
    belongs_rows = sum(r["fee_usd"] for r in rows if r["belongs_to_month"] == "Y")
    after = sum(r["fee_usd"] for r in rows if r["belongs_to_month"] == "Y" and r["booked_in_month"] == "N")
    earlier = sum(r["fee_usd"] for r in rows if r["booked_in_month"] == "Y" and r["sales_month"] < ctx.month)
    acc_in_m = sum(a["cogs_net"] for a in acc_rows if a["in_month"] == "Y")
    # Fee lines on accepted non-COGS accounts (40105000 shipping chargebacks) are booked
    # on income by design, so they are not part of booked fee COGS.
    accepted_non_cogs = sum(r["amount"] for r in ctx.details.get("fee_lines_outside", [])
                            if r["treatment"] == "accepted, not reclassed")
    acc_at_e = sum(a["cogs_net"] for a in acc_rows if a["date"] == ctx.E)
    # estimate for the month's orders with no fee row yet
    est_rows = []
    estimate = 0.0
    est_n = 0
    if cache_orders is not None:
        excl = set(cfg["estimate"]["include_statuses_exclude"])
        for mk in cfg["estimate"]["marketplaces"]:
            fees = [v for o, v in settled_order_fee.items() if settled_orders_mk.get(o) == mk]
            med = rules.median_fee(fees)
            m_orders = [o for o, v in cache_orders.items()
                        if v.get("marketplace") == mk and v.get("purchase_mt_date")
                        and rules.month_key(v["purchase_mt_date"]) == ctx.month and v.get("status") not in excl]
            uns = [o for o in m_orders if o not in fee_order_ids]
            e = rules.unsettled_estimate(len(uns), med)
            est_rows.append({"marketplace": mk, "orders_in_month": len(m_orders), "orders_settled": len(fees),
                             "orders_unsettled": len(uns), "median_fee": r2(med), "estimate": e})
            estimate += e
            est_n += len(uns)
    ctx.details["fee_estimate"] = est_rows
    belongs = belongs_rows + estimate
    booked = booked_rows - accepted_non_cogs + acc_in_m
    net = r2(belongs - booked)
    measured_after = r2(after)
    accr = rules.accrual_amount(measured_after, r2(estimate), acc_at_e)
    fee_acct = ctx.fee_acct
    accrued = cfg["accounts"]["accrued_misc"]
    est_txt = (f"not yet settled: estimate {_wd(estimate)} over {est_n} orders" if cache_orders is not None
               else "fees not yet settled are not estimated (orders cache unavailable)")
    existing = f"; less {_wd(acc_at_e)} already accrued at {rules.fmt_md(ctx.E)}" if abs(acc_at_e) >= 0.005 else ""
    if abs(accr) >= 0.005:
        dr, cr = (fee_acct, accrued) if accr > 0 else (accrued, fee_acct)
        entry = (f"At {rules.fmt_md(ctx.E)}: debit {dr} {_wd(abs(accr))}, credit {cr} {_wd(abs(accr))} "
                 f"(fees on {ctx.label} sales settled {rules.fmt_md(ctx.next_first)} to {rules.fmt_md(ctx.A)}: measured "
                 f"{_wd(measured_after)}; {est_txt}{existing}). Reverse on {rules.fmt_md(ctx.next_first)}.")
    else:
        entry = "No accrual needed."
    prev_acc = [a for a in acc_rows if a["date"] == ctx.S - timedelta(days=1) and a["is_reversal"] == "N"]
    prev_rev = [a for a in acc_rows if a["date"] == ctx.S and a["is_reversal"] == "Y"]
    if prev_acc and prev_rev:
        ctx.fine.append(f"The {rules.month_label(ctx.prev_month)} fee accrual and its reversal on {rules.fmt_md(ctx.S)} are both booked.")
    elif prev_acc:
        for a in prev_acc:
            a["review_id"] = "R-T1"
        ctx.review.append({"id": "R-T1", "driver": "T1", "what": "Prior-month Amazon fee accrual has no reversal in the month",
                           "records": ", ".join(a["journal"] for a in prev_acc), "amount": r2(sum(a["cogs_net"] for a in prev_acc)),
                           "note": f"Reverse it on {rules.fmt_md(ctx.S)} or confirm it should stay."})
    elif earlier >= 0.005:
        ctx.limitations.append(f"No {rules.month_label(ctx.prev_month)} Amazon fee accrual was booked, so {_wd(earlier)} of "
                               f"fees on earlier sales landed in {ctx.label}; the matched view moves them out.")
    basis = "measured+estimate" if cache_orders is not None else "measured"
    detail = (f"Fees on {ctx.label} sales {_wd(belongs)} (settled in month {_wd(belongs_rows - after)}, after month end "
              f"{_wd(after)}, estimate {_wd(estimate)}); booked {_wd(booked)} (settlements {_wd(booked_rows)}, less "
              f"{_wd(accepted_non_cogs)} on accepted non-COGS accounts, accrual journals {_wd(acc_in_m)}).")
    ctx.timing.append({"id": "T1", "driver": "T1", "what": "Amazon fees by sales month and the month-end accrual",
                       "booked_in_month": r2(booked), "belongs_to_month": r2(belongs), "net": net,
                       "income_effect": 0.0, "cogs_effect": net, "entry": entry, "basis": basis, "detail": detail})
    if abs(accr) >= 0.005:
        _entry_pair(ctx, ctx.E, fee_acct, accrued, accr, f"Accrue Amazon fees on {ctx.label} sales", "T1")
        _entry_pair(ctx, ctx.next_first, accrued, fee_acct, accr, f"Reverse {ctx.label} Amazon fee accrual", "T1")
    ctx.t1 = {"booked_rows": r2(booked_rows), "accepted_non_cogs": r2(accepted_non_cogs),
              "accrual_journals": r2(acc_in_m), "belongs_rows": r2(belongs_rows),
              "after": measured_after, "earlier": r2(earlier), "estimate": r2(estimate), "estimate_orders": est_n,
              "accrual": accr, "existing_at_month_end": r2(acc_at_e), "sources": dict(src_count)}
    if src_count.get("posted_date"):
        ctx.limitations.append(f"{src_count['posted_date']} settlement rows had no invoice or cached order date; their "
                               f"sales month is the row's posted date.")


def _refunds(ctx, raw, setl):
    cfg = ctx.cfg
    fx = cfg["fx_to_usd"]
    by_sid = {s["sid"]: s for s in setl}
    cms = raw["credit_memos"]
    cm_income = sum(-num(c.get("inc")) for c in cms)
    legacy = [c for c in cms if not (c.get("ext") or "").startswith("MC-CM-")]
    refund_rows = defaultdict(list)
    posted_in_m = 0.0
    for r in raw["settlement_rows"]:
        if str(r.get("ty")) != "2":
            continue
        s = by_sid.get(str(r.get("sm")))
        if s is None:
            continue
        pdt = _pd(r.get("pd"))
        amt = num(r.get("pc")) * rules.fx_rate(s["currency"], fx)
        if pdt and ctx.S <= pdt <= ctx.E:
            posted_in_m += amt
        if r.get("o"):
            refund_rows[r["o"]].append(pdt)
    out = []
    moved = 0.0
    for c in legacy:
        d = _pd(c.get("d"))
        pds = [p for p in refund_rows.get(c.get("oid"), []) if p]
        near = min(pds, key=lambda p: abs((p - d).days)) if pds else None
        inc = -num(c.get("inc"))
        pm = rules.month_key(near) if near else "no refund row loaded"
        flag = "Y" if near and pm != ctx.month else "N"
        if flag == "Y":
            moved += inc
        out.append({"credit_memo": c.get("tranid"), "order_id": c.get("oid") or "", "date": d, "income": r2(inc),
                    "refund_posted": near, "posted_month": pm, "other_month": flag})
    ctx.details["refunds"] = out
    ctx.details["refund_totals"] = {"cm_income": r2(cm_income), "cm_count": len(cms), "legacy_count": len(legacy),
                                    "rows_posted_in_month": r2(posted_in_m), "difference": r2(cm_income - posted_in_m)}
    n_other = sum(1 for o in out if o["other_month"] == "Y")
    base = (f"Amazon credit memos dated in {ctx.label} reduce income by {_wd(-cm_income)}; refund rows Amazon posted in the "
            f"month total {_wd(-posted_in_m)} (difference {_wd(cm_income - posted_in_m)}). Consolidated credit memos "
            f"are dated on the posted day.")
    if n_other:
        ctx.review.append({"id": "R-T2", "driver": "T2", "what": "Legacy credit memos whose refund posted in another month",
                           "records": f"{n_other} credit memos", "amount": r2(moved),
                           "note": "Information only; the matched view keeps refunds as booked. Confirm no correction is wanted."})
    ctx.fine.append(base[:238])


def _wengo(ctx, raw):
    cfg = ctx.cfg
    w = cfg["wengo"]
    pref = w["fee_item_prefixes"]
    po_head: dict = {}
    units = defaultdict(lambda: {"Titan": 0.0, "Pong": 0.0})
    for l in list(raw.get("wengo_po_lines", [])) + list(raw.get("month_po_lines", [])):
        po_head[l["po"]] = _pd(l.get("d"))
        item = l.get("item") or ""
        for fam, p in pref.items():
            if item.startswith(p):
                units[l["po"]][fam] += num(l.get("qty"))
    bills = []
    for b in raw["wengo_bills"]:
        d = _pd(b.get("d"))
        refs = PO_RE.findall(b.get("memo") or "")
        bills.append({"tranid": b["tranid"], "ttype": b.get("ttype"), "date": d, "created": _pd(b.get("cdate")),
                      "status": b.get("status") or "", "memo": b.get("memo") or "", "refs": refs,
                      "amount": -num(b.get("tot")), "cogs": num(b.get("cogs")) if b.get("cogs") is not None else None,
                      "pmonth": b.get("pmonth")})
    # latest observed per-unit rate per family
    rate = dict(w["fallback_rates"])
    rate_basis = {k: "config fallback" for k in rate}
    seen = {}
    for b in sorted(bills, key=lambda x: (x["date"], x["tranid"])):
        if not b["refs"] or not all(r in po_head for r in b["refs"]) or b["amount"] <= 0:
            continue
        T = sum(units[r]["Titan"] for r in b["refs"])
        P = sum(units[r]["Pong"] for r in b["refs"])
        if T and not P:
            seen["Titan"] = (b["amount"] / T, b["tranid"])
        elif P and not T:
            seen["Pong"] = (b["amount"] / P, b["tranid"])
        elif T and P:
            for rt, rp in w["known_rate_pairs"]:
                if abs(T * rt + P * rp - b["amount"]) < 0.005:
                    seen["Titan"] = (rt, b["tranid"])
                    seen["Pong"] = (rp, b["tranid"])
                    break
    rate_src = {k: None for k in rate}
    for fam, (rv, t) in seen.items():
        rate[fam] = round(rv, 4)
        rate_basis[fam] = f"observed on {t}"
        rate_src[fam] = t

    def rate_text(fam):
        fb = w["fallback_rates"][fam]
        if rate_src[fam]:
            return f"at {rate[fam]:g} per {fam} unit, the rate observed on {rate_src[fam]}; config fallback is {fb:g}"
        return f"at {rate[fam]:g} per {fam} unit, the config fallback (no rate observed)"
    rows = []
    n = 0
    rv = 0
    m_bills = [b for b in bills if ctx.S <= b["date"] <= min(ctx.E, ctx.A)]
    for b in m_bills:
        posted = b["cogs"] or 0.0
        pms = sorted({rules.month_key(po_head[r]) for r in b["refs"] if r in po_head})
        tid = ""
        if b["cogs"] is None:
            belongs = "not posted"
        elif not b["refs"] or len(pms) != 1:
            belongs = "review"
            rv += 1
            tid = f"R-W{rv}"
            why = "no PO number in the memo" if not b["refs"] else (
                "the named POs fall in different months: " + ", ".join(pms) if pms else "the named PO was not found")
            ctx.review.append({"id": tid, "driver": "T3", "what": "Wengo bill whose PO month cannot be set",
                               "records": f"{b['tranid']} ({rules.fmt_md(b['date'])})", "amount": r2(posted),
                               "note": f"Controller assigns the period: {why}."[:230]})
        else:
            belongs = pms[0]
            if belongs != ctx.month:
                n += 1
                tid = f"T3-{n}"
                ctx.timing.append({
                    "id": tid, "driver": "T3", "what": f"Wengo sourcing fee for {', '.join(b['refs'])} belongs to {belongs}",
                    "booked_in_month": r2(posted), "belongs_to_month": 0.0, "net": r2(-posted),
                    "income_effect": 0.0, "cogs_effect": r2(-posted),
                    "entry": (f"Move {_wd(posted)} of {b['tranid']} to {rules.month_label(belongs)} (PO month). "
                              f"If that period is closed, record it there as an out-of-period item."),
                    "basis": "measured", "detail": f"{b['tranid']} dated {rules.fmt_md(b['date'])}, PO dated in {belongs}."})
        rows.append({"bill": b["tranid"], "type": b["ttype"], "date": b["date"], "created": b["created"],
                     "status": b["status"], "memo": b["memo"], "pos": ", ".join(b["refs"]),
                     "po_months": ", ".join(pms), "posted_month": b["pmonth"] or "", "posted_cogs": r2(posted),
                     "belongs_month": belongs, "row_id": tid})
    # POs dated in the month with fee-bearing units and no Wengo bill naming them
    billed = {r for b in bills for r in b["refs"]}
    unb = []
    month_pos = sorted({l["po"] for l in raw.get("month_po_lines", [])})
    for po in month_pos:
        T, P = units[po]["Titan"], units[po]["Pong"]
        if not (T or P) or po in billed:
            continue
        n += 1
        tid = f"T3-{n}"
        est = r2(T * rate["Titan"] + P * rate["Pong"])
        unb.append({"po": po, "date": po_head.get(po), "titan_units": T, "pong_units": P, "titan_rate": rate["Titan"],
                    "pong_rate": rate["Pong"], "rate_basis": f"Titan {rate_basis['Titan']}; Pong {rate_basis['Pong']}",
                    "estimate": est, "row_id": tid})
        acct = cfg["accounts"]["wengo_cogs"]
        ctx.timing.append({
            "id": tid, "driver": "T3", "what": f"Wengo fee on {po} ({rules.fmt_md(po_head[po])}) not yet billed",
            "booked_in_month": 0.0, "belongs_to_month": est, "net": est, "income_effect": 0.0, "cogs_effect": est,
            "entry": _wengo_entry(ctx, cfg, acct, est, T, P, rate_text),
            "basis": "estimate", "detail": f"Rates: Titan {rate['Titan']} ({rate_basis['Titan']}), Pong {rate['Pong']} ({rate_basis['Pong']})."})
        _entry_pair(ctx, ctx.E, acct, cfg["accounts"]["accrued_misc"], est, f"Accrue Wengo fee on {po}", tid)
    ctx.details["wengo"] = rows
    ctx.details["wengo_unbilled"] = unb
    if m_bills and not any(r["row_id"] for r in rows):
        n_b = len(m_bills)
        ctx.fine.append(f"{n_b} Wengo bill{'s' if n_b != 1 else ''} dated in {ctx.label} "
                        f"belong{'s' if n_b == 1 else ''} to {'its' if n_b == 1 else 'their'} PO month, {ctx.label}.")
    elif not m_bills:
        ctx.fine.append(f"No Wengo bills are dated in {ctx.label}.")
    if any(r["belongs_month"] == "not posted" for r in rows):
        ctx.limitations.append("A Wengo bill pending approval posts nothing yet; it is listed on the Wengo sheet only.")


def _wengo_entry(ctx, cfg, acct, est, T, P, rate_text) -> str:
    """Accrual text naming the per-unit rate used and its source, under 240 characters."""
    fams = [(f, u) for f, u in (("Titan", T), ("Pong", P)) if u]
    head = (f"At {rules.fmt_md(ctx.E)}: debit {acct} {_wd(est)}, credit {cfg['accounts']['accrued_misc']} {_wd(est)}: ")
    units = "; ".join(f"{int(u)} {f} units {rate_text(f)}" for f, u in fams)
    text = head + units + ". Reverse when the bill posts."
    if len(text) > 238:
        text = head + units + "."
    if len(text) > 238:
        text = text[:235].rstrip(" ,;") + "..."
    return text


def _retailer(ctx, raw):
    rows = []
    n = 0
    rv = 0
    per_bill: dict = {}
    for r in raw["retailer_lines"]:
        memo = " ".join(dict.fromkeys(x for x in ((r.get("lmemo") or "").strip(), (r.get("hmemo") or "").strip()) if x))
        bd = _pd(r.get("d"))
        periods = rules.infer_program_period(memo, bd, _pd(r.get("cdate")))
        amt = num(r.get("amt"))
        if periods is None:
            in_m, moved, target = None, None, "controller assigns"
        else:
            parts = rules.split_amount(amt, periods)
            in_m = sum(p for per, p in parts if per == ctx.month)
            moved = amt - in_m
            target = ", ".join(f"{per} {_wd(p)}" if len(parts) > 1 else per for per, p in parts)
        key = r["tid"]
        b = per_bill.setdefault(key, {"tranid": r["tranid"], "vendor": r.get("vendor") or "", "date": bd, "memo": memo,
                                      "amount": 0.0, "moved": 0.0, "targets": set(), "none": False})
        b["amount"] += amt
        if periods is None:
            b["none"] = True
        else:
            b["moved"] += moved
            b["targets"].update(per for per, _ in periods if per != ctx.month)
        rows.append({"bill": r["tranid"], "type": r.get("ttype"), "vendor": r.get("vendor") or "", "date": bd,
                     "created": _pd(r.get("cdate")), "account": r.get("acct"), "memo": memo, "deduction": r2(amt),
                     "periods": target, "belongs_in_month": r2(in_m) if in_m is not None else None,
                     "moved": r2(moved) if moved is not None else None, "row_id": ""})
    for tid, b in per_bill.items():
        rid = ""
        if b["none"]:
            rv += 1
            rid = f"R-T4-{rv}"
            ctx.review.append({"id": rid, "driver": "T4", "what": f"Retailer deduction with no period in the memo ({b['vendor']})",
                               "records": f"{b['tranid']} ({rules.fmt_md(b['date'])})", "amount": r2(b["amount"]),
                               "note": f"Controller assigns the period. Memo: {b['memo']}"[:230]})
        elif abs(b["moved"]) >= 0.005:
            n += 1
            rid = f"T4-{n}"
            tg = ", ".join(sorted(b["targets"]))
            ctx.timing.append({
                "id": rid, "driver": "T4", "what": f"Retailer deduction {b['vendor']} '{b['memo'][:60]}' belongs to {tg}",
                "booked_in_month": r2(b["amount"]), "belongs_to_month": r2(b["amount"] - b["moved"]),
                "net": r2(-b["moved"]), "income_effect": r2(b["moved"]), "cogs_effect": 0.0,
                "entry": (f"Move {_wd(b['moved'])} of {b['tranid']} to {tg}, or accrue it there going forward. "
                          f"If that period is closed, record it as an out-of-period item."),
                "basis": "measured", "detail": f"Period from the memo by the program-period rule; bill dated {rules.fmt_md(b['date'])}."})
        for r in rows:
            if r["bill"] == b["tranid"]:
                r["row_id"] = rid
    ctx.details["retailer"] = rows
    if rows and n == 0 and rv == 0:
        ctx.fine.append(f"Retailer deductions booked in {ctx.label} belong to {ctx.label} by their memos.")
    elif not rows:
        ctx.fine.append(f"No retailer deductions post to income in {ctx.label}.")


def _duplicates(ctx, raw):
    out = []
    k = 0

    def extras(docs, key):
        g = defaultdict(list)
        for d in docs:
            g[d.get(key)].append(d)
        res = []
        for kk, L in g.items():
            if len(L) < 2:
                continue
            L = sorted(L, key=lambda x: int(x["tid"]))
            for i, d in enumerate(L):
                res.append((kk, d, i > 0, L))
        return res

    groups = [("Legacy invoice", raw.get("dup_invoice_docs", []), "oid"),
              ("Legacy credit memo", [d for d in raw.get("dup_cm_docs", []) if not (d.get("ext") or "").startswith("MC-CM-")], "oid"),
              ("Consolidated invoice", [d for d in raw.get("consolidated_invoices", [])
                                        if ctx.S <= _pd(d["d"]) <= min(ctx.E, ctx.A)], "orn")]
    for kind, docs, key in groups:
        ex = extras(docs, key)
        if not ex:
            continue
        same_amount = all(len({round(num(d.get("inc")), 2) for d in L}) == 1 for _, _, _, L in ex)
        measured = kind != "Legacy credit memo" or same_amount
        k += 1
        eid = f"D1-{k}"
        inc_eff = cogs_eff = 0.0
        ids = []
        for kk, d, is_extra, _ in ex:
            inc_cp = -num(d.get("inc"))
            cg = num(d.get("cogs"))
            if is_extra:
                inc_eff += -inc_cp
                cogs_eff += -cg
                ids.append(d.get("tranid"))
            out.append({"kind": kind, "key": kk, "document": d.get("tranid"), "date": _pd(d.get("d")),
                        "income": r2(inc_cp), "cogs": r2(cg), "extra": "Y" if is_extra else "N",
                        "error_id": eid if measured else f"R-{eid}"})
        rec = ", ".join(ids[:6]) + (f" and {len(ids) - 6} more" if len(ids) > 6 else "")
        item = {"id": eid if measured else f"R-{eid}", "driver": "D1", "records": rec[:230],
                "amount": r2(abs(inc_eff) if abs(inc_eff) >= 0.005 else abs(cogs_eff))}
        if not measured:
            item["amount"] = r2(abs(inc_eff))
        if measured:
            ctx.errors.append({**item, "what": f"{kind}s duplicated for the same {'order' if key == 'oid' else 'reference'}",
                               "income_effect": r2(inc_eff), "cogs_effect": r2(cogs_eff),
                               "entry": f"Void or credit the duplicate documents ({len(ids)}); keep the earliest of each.",
                               "why": "Each order or daily reference should carry one document.", "confidence": "measured"})
        else:
            ctx.review.append({**item, "what": f"{kind}s: more than one for the same order",
                               "note": "Amounts differ, so these may be partial refunds. Confirm before reversing."})
    ctx.details["duplicates"] = out
    if not out:
        ctx.fine.append(f"No duplicate Amazon invoices or credit memos dated in {ctx.label}.")


def _completeness(ctx, raw, cache_orders, cache_info):
    cfg = ctx.cfg
    c = cfg["completeness"]
    fx_mk = {mk: cfg["fx_to_usd"][cur] for mk, cur in cfg["marketplace_currency"].items()}
    rows = []
    if cache_orders is None:
        ctx.details["completeness"] = rows
        ctx.limitations.append("Amazon revenue completeness was not measured: the orders cache was not loaded.")
        return None
    last_mature = ctx.A - timedelta(days=c["mature_days"])
    hi = min(ctx.E, last_mature)
    if hi < ctx.S:
        ctx.details["completeness"] = rows
        ctx.limitations.append(f"Amazon revenue completeness not measurable: no purchase day in {ctx.label} is "
                               f"{c['mature_days']} days old as of {rules.fmt_mdy(ctx.A)}.")
        return None
    if not cache_info.get("items_present"):
        ctx.details["completeness"] = rows
        ctx.limitations.append("Amazon revenue completeness not measurable: the orders cache holds no item prices "
                               "(order totals include tax and shipping).")
        return None
    ns = defaultdict(float)
    for d in raw.get("consolidated_invoices", []):
        dd = _pd(d["d"])
        if ctx.S <= dd <= hi:
            mk = (d.get("orn") or "").split("-")[2] if (d.get("orn") or "").count("-") >= 3 else "?"
            ns[mk] += num(d.get("rev40100"))
    cur_mk = {"1": "US", "2": "UK", "3": "CA"}
    for d in raw.get("legacy_invoice_revenue", []):
        dd = _pd(d["d"])
        if ctx.S <= dd <= hi:
            ns[cur_mk.get(str(d.get("cur")), "?")] += num(d.get("rev40100"))
    amz = defaultdict(float)
    missing_items = 0
    for o in cache_orders.values():
        ud = o.get("purchase_utc_date")
        if not ud or not (ctx.S <= ud <= hi) or o.get("status") not in ("Shipped", "Delivered", "PartiallyShipped"):
            continue
        if o.get("item_total") is None:
            missing_items += 1
            continue
        mk = o.get("marketplace")
        if mk in fx_mk:
            amz[mk] += o["item_total"] * fx_mk[mk]
    worst = None
    for mk in cfg["estimate"]["marketplaces"]:
        a, n_ = amz.get(mk, 0.0), ns.get(mk, 0.0)
        ratio = (n_ / a) if a else None
        fine = ratio is not None and c["fine_low"] <= ratio <= c["fine_high"]
        rows.append({"marketplace": mk, "days": f"{rules.fmt_md(ctx.S)} to {rules.fmt_md(hi)}", "netsuite_usd": r2(n_),
                     "amazon_usd": r2(a), "ratio": round(ratio, 4) if ratio is not None else None,
                     "verdict": "fine" if fine else ("not measurable" if ratio is None else "outside tolerance")})
        if ratio is not None and (worst is None or abs(ratio - 1) > abs(worst[1] - 1)):
            worst = (mk, ratio)
    ctx.details["completeness"] = rows
    if missing_items:
        ctx.limitations.append(f"{missing_items} shipped orders in the completeness window have no cached item prices and are left out.")
    if rows and all(r["verdict"] == "fine" for r in rows):
        ctx.fine.append(f"Amazon item revenue in NetSuite matches the orders cache within tolerance for "
                        f"{rules.fmt_md(ctx.S)} to {rules.fmt_md(hi)} (approximate).")
    else:
        ctx.limitations.append("Amazon revenue completeness (approximate) is outside 99.5 to 100.5 percent for at least "
                               "one marketplace; see the Notes sheet.")
    return worst


def _first_sentence(text: str, limit: int = 180) -> str:
    first = text.split(". ")[0].rstrip(".") + "."
    if len(first) <= limit:
        return first
    cut = first[:limit].rsplit(" ", 1)[0].rstrip(",;")
    return cut + "."


def _answer(ctx, pnl, margin) -> list[str]:
    def pct(x):
        return "n/a" if x is None else f"{x * 100:.1f} percent"
    lab = ctx.label
    out = [f"{lab} as booked shows {pct(margin['as_booked'])} gross margin on income of {_wd(pnl['income'])} "
           f"as of {rules.fmt_mdy(ctx.A)}."]
    if ctx.errors:
        tot = sum(abs(e["amount"]) for e in ctx.errors)
        out.append(f"{len(ctx.errors)} booking errors total {_wd(tot)}; corrected, margin is {pct(margin['corrected'])}.")
    else:
        out.append("No booking errors were found; corrected margin equals as booked.")
    out.append(f"Matched to the month the sales were earned, margin is {pct(margin['matched'])} "
               f"({margin['matched_basis'].replace('+', ' plus ')}).")
    t1 = ctx.t1
    if abs(t1["accrual"]) >= 0.005:
        out.append(f"The proposed Amazon fee accrual at {rules.fmt_md(ctx.E)} is {_wd(t1['accrual'])}, "
                   f"reversed on {rules.fmt_md(ctx.next_first)}.")
    if ctx.review:
        out.append(f"{len(ctx.review)} items need a controller ruling.")
    out.append(f"{len(ctx.fine)} checks came back fine. Nothing was changed in NetSuite.")
    short = getattr(ctx, "lim_short", None)
    if short:
        out.append("Largest limitation: " + short)
    elif ctx.limitations:
        out.append("Largest limitation: " + _first_sentence(ctx.limitations[0]))
    return out[:8]


def build(month: str, asof: date, raw: dict, cfg: dict, cache_orders: dict | None, cache_info: dict | None,
          meta: dict) -> tuple[dict, dict]:
    ctx = _Ctx(month, asof, cfg)
    pnl = _pnl(ctx, raw)
    journals = _journals(raw, cfg)
    setl = _settlements(ctx, raw, journals)
    _e1_e4(ctx, setl, journals)
    ctx.details["settlements"] = [{k: v for k, v in s.items() if k not in ("bank_accounts", "fee_accounts")} for s in setl]
    ctx.details["unmapped_journals"] = [
        {"journal": t, "date": journals[t]["d"], "bank": r2(journals[t]["bank"]), "feeall": r2(journals[t]["feeall"]),
         "review_id": "R-E1"} for t in ctx.unmapped_journals]
    if ctx.unmapped_journals:
        ctx.review.append({"id": "R-E1", "driver": "E1", "what": "Settlement journals dated in the month with no settlement match",
                           "records": ", ".join(ctx.unmapped_journals)[:230],
                           "amount": r2(sum(journals[t]["feeall"] for t in ctx.unmapped_journals)),
                           "note": "Fee lines shown. Confirm which settlement each journal books; they are left out of the fee checks."})
    _J, acc_rows = _manual(ctx, raw)
    _late(ctx, raw)
    _fee_timing(ctx, raw, setl, acc_rows, cache_orders)
    _refunds(ctx, raw, setl)
    _wengo(ctx, raw)
    _retailer(ctx, raw)
    _duplicates(ctx, raw)
    _completeness(ctx, raw, cache_orders, cache_info or {})

    inc0, cogs0 = pnl["income"], pnl["cogs"]
    inc1 = inc0 + sum(e["income_effect"] for e in ctx.errors)
    cogs1 = cogs0 + sum(e["cogs_effect"] for e in ctx.errors)
    inc2 = inc1 + sum(t["income_effect"] for t in ctx.timing)
    cogs2 = cogs1 + sum(t["cogs_effect"] for t in ctx.timing)
    if cache_orders is None:
        basis = "measured, excludes fees not yet settled"
    elif any(t["basis"] == "estimate" or "estimate" in t["basis"] for t in ctx.timing):
        basis = "measured+estimate"
    else:
        basis = "measured"
    margin = {"as_booked": rules.gm_pct(inc0, cogs0), "corrected": rules.gm_pct(inc1, cogs1),
              "matched": rules.gm_pct(inc2, cogs2), "matched_basis": basis,
              "income": {"as_booked": r2(inc0), "corrected": r2(inc1), "matched": r2(inc2)},
              "cogs": {"as_booked": r2(cogs0), "corrected": r2(cogs1), "matched": r2(cogs2)}}
    fx = cfg["fx_to_usd"]
    ctx.limitations.append(f"Fixed FX for settlement fees and credit memos: CAD {fx['CAD']}, GBP {fx['GBP']}. "
                           f"Transactions dated after {rules.fmt_mdy(asof)} are excluded.")
    if cache_info and cache_info.get("cutoff_utc"):
        ctx.limitations.append(f"Amazon orders cache runs through {cache_info['cutoff_utc'][:16].replace('T', ' ')} UTC.")
    if cache_orders is not None:
        ctx.limitations.append("Unsettled-fee estimate: orders in the cache purchased in the month (Mountain Time), not "
                               "canceled or pending, with no settlement fee row, times the median fee per settled order "
                               "of the same marketplace.")
    ctx.limitations.append("Wengo fee accruals are estimates; whether the per-unit fee belongs in period COGS or "
                           "inventory cost is an open controller question.")
    details = ctx.details
    details["entries"] = ctx.entries
    counts = {"settlements": sum(1 for s in setl if s["in_month"] == "Y"),
              "manual_journals": len({r["journal"] for r in details["manual_journals"]}),
              "late_created": len(details["late_created"]),
              "wengo_bills": len(details["wengo"]), "retailer_bills": len({r["bill"] for r in details["retailer"]}),
              "refund_cms": details["refund_totals"]["cm_count"]}
    model = {
        "month": month, "asof": asof.isoformat(), "run_at_mt": meta.get("run_at_mt", ""),
        "code_rev": meta.get("code_rev", "unknown"),
        "pnl": pnl, "errors": ctx.errors, "timing": ctx.timing, "review": ctx.review, "fine": ctx.fine,
        "margin": margin, "detail_counts": counts, "limitations": ctx.limitations,
        "fee_cogs_account": ctx.fee_acct, "t1": ctx.t1,
        "delivery": {"drive_file_id": "", "drive_link": "", "email": "skipped", "log_row": "skipped"},
    }
    model["answer"] = _answer(ctx, pnl, margin)
    model["limitation_headline"] = model["answer"][-1] if model["answer"][-1].startswith("Largest limitation") else ""
    _self_check(model, details)
    return model, details


def _self_check(model: dict, details: dict) -> None:
    """Python-side assertions that the figures the workbook formulas compute agree with
    the model (the same sums, computed from the detail rows)."""
    pnl_inc = sum(r["amount"] for r in details["pnl"] if r["type"] == "Income")
    pnl_cogs = sum(r["amount"] for r in details["pnl"] if r["type"] == "COGS")
    assert abs(pnl_inc - model["pnl"]["income"]) < 0.02, "P&L income does not tie to its rows"
    assert abs(pnl_cogs - model["pnl"]["cogs"]) < 0.02, "P&L COGS does not tie to its rows"
    for e in model["errors"]:
        if e["driver"] == "E1":
            s = sum(r["unbooked"] for r in details["settlements"] if r["error_id"] == e["id"])
            assert abs(s - e["amount"]) < 0.02, f"{e['id']} does not tie to the Settlements sheet"
    ft = details["fee_timing"]
    t1 = next(t for t in model["timing"] if t["id"] == "T1")
    booked = (sum(r["fee_usd"] for r in ft if r["booked_in_month"] == "Y") - model["t1"]["accepted_non_cogs"]
              + model["t1"]["accrual_journals"])
    accepted = sum(r["amount"] for r in details["fee_lines_outside"] if r["treatment"] == "accepted, not reclassed")
    assert abs(accepted - model["t1"]["accepted_non_cogs"]) < 0.005, "accepted fee lines do not tie"

    belongs = sum(r["fee_usd"] for r in ft if r["belongs_to_month"] == "Y") + model["t1"]["estimate"]
    assert abs(booked - t1["booked_in_month"]) < 0.05, "T1 booked does not tie to Fee timing"
    assert abs(belongs - t1["belongs_to_month"]) < 0.05, "T1 belongs does not tie to Fee timing"
    by_src = defaultdict(lambda: [0.0, 0.0])
    for en in details["entries"]:
        by_src[en["source"]][0] += en["debit"]
        by_src[en["source"]][1] += en["credit"]
    for src, (dr, cr) in by_src.items():
        assert abs(dr - cr) < 0.005, f"entries for {src} do not balance"


def email_tables(model: dict) -> str:
    """Plain-text rendering of the errors and timing tables (whole dollars)."""
    lines = ["Errors to correct:"]
    if not model["errors"]:
        lines.append("  None found.")
    for e in model["errors"]:
        lines.append(f"  {e['id']}: {e['what']}. {e['records']}. Amount {_wd(e['amount'])}. Entry: {e['entry']}")
    lines.append("")
    lines.append("Entries to book at month end:")
    if not model["timing"]:
        lines.append("  None.")
    for t in model["timing"]:
        lines.append(f"  {t['id']}: {t['what']}. Booked {_wd(t['booked_in_month'])}, belongs {_wd(t['belongs_to_month'])}, "
                     f"net {_wd(t['net'])} ({t['basis'].replace('+', ' plus ')}). Entry: {t['entry']}")
    if model["review"]:
        lines.append("")
        lines.append("For controller review:")
        for r in model["review"]:
            lines.append(f"  {r['id']}: {r['what']}. {r['records']}. Amount {_wd(r['amount'])}.")
    return "\n".join(lines)
