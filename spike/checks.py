"""checks.py -- FD1a E2 self-checks as code (CONTRACT.md `meta.checks`).

`run_checks(D, prev_state)` takes `D`, the FULL assembled output dict (what `extract.py`
writes as the JSON body -- i.e. `latest.json`'s content, or a section thereof read back via
`--data`), and `prev_state`, the small prior-run state dict written by
`extract.py --write-state PATH` (see `spike/CONTRACT.md` "Prior-run state"). Returns the
`meta.checks` object: eight named checks (`a_channel_foot` .. `g_closed_months_stable`,
`t5_bom_rule`) each `{"pass": bool, "detail": str}`, plus `all_pass`.

CLI:
  doppler run --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG -- python spike/checks.py \
      --data spike/data/latest.json [--prev-state spike/data/state.json]
Exits 0 iff `all_pass`; always prints the full `meta.checks` JSON to stdout first, so a
non-zero exit is diagnosable without re-running. Read-only: never touches NetSuite or Amazon
directly (it only reads local JSON files already produced by `extract.py`).

State-shape note: CONTRACT.md's "Prior-run state" shape is
`{"pulled_at_mt", "picklist_snapshot", "closed_months": {...}}` -- the minimum needed for
checks (f) and (g). This module's `extract.py --write-state` ADDS one more top-level key,
`"sections": {<name>: {"rows": int}}`, purely so check (c)'s "still has rows" comparison has
something to compare against; a state file written before this addition (or without it) is
handled the same as "no prior state" for that one sub-check, never a hard failure.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

MT = ZoneInfo("America/Denver")

# T5 (PRD Section 7), redefined 2026-08-26 (team-lead review): originally named two SKUs
# (P-RIM-003-BLA, A-STICKER-001) that were expected to NEVER appear in sku_sales, based on
# research/01a's narrower sample (the Amazon DAILY-FBA population and one Major Retail EDI
# invoice, both examined WITHOUT the Income-line join). Live verification during this build
# found P-RIM-003-BLA ("Replacement Rim - 3.0 - Black") genuinely, independently sold with
# real Income postings across Spikeball.com/Major Retail/PE/Rec (a real spare-parts SKU, not
# a BOM artifact -- item class "Parts Replacement") -- "SKU X is absent" was a false-positive
# invariant, not evidence the Income-join rule was broken. What T5 actually needs to prove is
# the METHOD, per SKU: (1) sku_sales units (Income-joined) are DOMINATED by component
# consumption relative to every line touching the item with no Income filter (naive_qty,
# built live by extract.py's build_t5_sentinels against meta.t5_sentinels); (2) no sku_sales
# row anywhere has revenue==0 (a true BOM-consumption line has no GL posting and cannot
# produce a matching row at all, so this can only fail if the join itself broke); (3) when a
# real list price exists, realized revenue-per-unit falls in a plausible band of it (informal
# by design -- channel/discount/bundle pricing varies, this is a sanity band, not equality).
T5_SENTINEL_SKUS = ["P-RIM-003-BLA", "A-STICKER-001"]
T5_DOMINANCE_RATIO = 0.25  # Income-joined units must be <= this fraction of naive_qty
T5_PRICE_BAND = (0.5, 3.0)  # realized avg price must fall within [0.5x, 3x] list price

TOLERANCE_DOLLARS = 0.01
CLOSED_MONTH_TOLERANCE_PCT = 0.5
FRESHNESS_MAX_HOURS = 26


def _ok(detail: str) -> dict:
    return {"pass": True, "detail": detail}


def _fail(detail: str) -> dict:
    return {"pass": False, "detail": detail}


# ---------------------------------------------------------------------------
# (a) channel foot
# ---------------------------------------------------------------------------

def check_a_channel_foot(D: dict) -> dict:
    """Channel-summed revenue (incl. Unassigned) equals an independently-queried
    unsegmented Income total, for MTD, YTD, and each of the 13 trailing months, within
    $0.01 -- this IS `self_check` (built by an independent second query in extract.py, per
    PRD E2(a)); this check just reads its verdict and surfaces the worst diff. `self_check`
    itself already retries once, live, on a live-postings race (a window whose two sides were
    queried minutes apart catching a real new posting in between); `retry_detail`, when
    present, is always surfaced here too so a retry is visible in meta.checks, not just
    meta.sections.self_check."""
    sc = D.get("self_check")
    if not sc or "pass" not in sc:
        return _fail("self_check section missing or empty (build error) -- cannot verify channel foot")

    retry_suffix = f" [{sc['retry_detail']}]" if sc.get("retried") and sc.get("retry_detail") else ""

    diffs = [abs(sc.get("diff", 0.0)), abs(sc.get("ytd_diff", 0.0))]
    month_diffs = [abs(m.get("diff", 0.0)) for m in sc.get("months", [])]
    diffs.extend(month_diffs)
    worst = max(diffs) if diffs else None

    if worst is None:
        return _fail("self_check present but has no diff fields to check" + retry_suffix)
    if worst > TOLERANCE_DOLLARS or not sc.get("pass", False):
        offenders = []
        if abs(sc.get("diff", 0.0)) > TOLERANCE_DOLLARS:
            offenders.append(f"MTD diff={sc['diff']}")
        if abs(sc.get("ytd_diff", 0.0)) > TOLERANCE_DOLLARS:
            offenders.append(f"YTD diff={sc['ytd_diff']}")
        for m in sc.get("months", []):
            if abs(m.get("diff", 0.0)) > TOLERANCE_DOLLARS:
                offenders.append(f"{m['ym']} diff={m['diff']}")
        return _fail(f"channel foot exceeds $0.01: {'; '.join(offenders)}" + retry_suffix)
    return _ok(f"worst |diff| across MTD/YTD/13 months = {worst}" + retry_suffix)


# ---------------------------------------------------------------------------
# (b) inventory tie
# ---------------------------------------------------------------------------

def check_b_inventory_tie(D: dict) -> dict:
    inv = D.get("inventory")
    if not inv or "tie_out_diff" not in inv:
        return _fail("inventory section missing or empty (build error) -- cannot verify tie-out")
    diff = inv.get("tie_out_diff")
    if diff is None:
        return _fail("inventory.tie_out_diff is null")
    if abs(diff) > TOLERANCE_DOLLARS:
        return _fail(
            f"tie_out_diff={diff} exceeds $0.01 "
            f"(locations={inv.get('total_value_locations')}, item_header={inv.get('total_value_item_header')})"
        )
    return _ok(f"tie_out_diff={diff}")


# ---------------------------------------------------------------------------
# (c) rows present + every NetSuite section ok
# ---------------------------------------------------------------------------

def check_c_rows_present(D: dict, prev_state: dict) -> dict:
    sections = D.get("meta", {}).get("sections", {})
    if not sections:
        return _fail("meta.sections missing or empty -- cannot verify")

    not_ok = [name for name, s in sections.items() if name != "amazon_orders" and s.get("status") != "ok"]
    if not_ok:
        return _fail(f"NetSuite section(s) not status=ok: {', '.join(sorted(not_ok))}")

    prev_sections = prev_state.get("sections") if prev_state else None
    if not prev_sections:
        return _ok("all NetSuite sections status=ok; no prior state to compare row counts against")

    silent_empty = []
    for name, prev in prev_sections.items():
        prev_rows = prev.get("rows", 0)
        cur = sections.get(name)
        if cur is not None and cur.get("status") == "skipped":
            continue  # deliberately skipped this run (e.g. --skip-amazon): not a silent-empty failure
        if prev_rows > 0 and cur is not None and cur.get("rows", 0) == 0:
            silent_empty.append(name)
    if silent_empty:
        return _fail(f"section(s) had rows previously but 0 rows now (silent-empty failure): {', '.join(sorted(silent_empty))}")
    return _ok("all NetSuite sections status=ok; no section that had rows previously is now empty")


# ---------------------------------------------------------------------------
# (d) freshness
# ---------------------------------------------------------------------------

def check_d_fresh(D: dict) -> dict:
    pulled_at = D.get("meta", {}).get("pulled_at_mt")
    if not pulled_at:
        return _fail("meta.pulled_at_mt missing")
    try:
        dt = datetime.datetime.fromisoformat(pulled_at)
    except ValueError:
        return _fail(f"meta.pulled_at_mt not valid ISO-8601: {pulled_at!r}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        return _fail(f"meta.pulled_at_mt has no explicit UTC offset: {pulled_at!r}")
    now = datetime.datetime.now(MT)
    age_hours = (now - dt).total_seconds() / 3600.0
    if age_hours > FRESHNESS_MAX_HOURS:
        return _fail(f"pulled_at_mt is {age_hours:.1f}h old, exceeds {FRESHNESS_MAX_HOURS}h")
    if age_hours < -0.1:
        return _fail(f"pulled_at_mt is {abs(age_hours):.1f}h in the future")
    return _ok(f"age {age_hours:.2f}h, offset {dt.utcoffset()}")


# ---------------------------------------------------------------------------
# (e) asof / window recomputation
# ---------------------------------------------------------------------------

def check_e_asof(D: dict) -> dict:
    meta = D.get("meta", {})
    asof_str = meta.get("asof_date")
    mtd_start = meta.get("mtd_start")
    ytd_start = meta.get("ytd_start")
    if not asof_str or not mtd_start or not ytd_start:
        return _fail("meta.asof_date / mtd_start / ytd_start missing")
    try:
        asof = datetime.date.fromisoformat(asof_str)
    except ValueError:
        return _fail(f"meta.asof_date not a valid date: {asof_str!r}")

    override = bool(meta.get("asof_override"))
    notes = []
    if not override:
        yesterday = datetime.datetime.now(MT).date() - datetime.timedelta(days=1)
        if asof != yesterday:
            return _fail(f"meta.asof_date={asof_str} != yesterday MT ({yesterday.isoformat()}) and asof_override is not set")
        notes.append(f"asof_date={asof_str} matches yesterday MT")
    else:
        notes.append(f"asof_override=true, asof_date={asof_str} not checked against yesterday (test run)")

    recomputed_mtd = asof.replace(day=1).isoformat()
    recomputed_ytd = datetime.date(asof.year, 1, 1).isoformat()
    if recomputed_mtd != mtd_start:
        return _fail(f"mtd_start={mtd_start} != recomputed {recomputed_mtd} from asof_date={asof_str}")
    if recomputed_ytd != ytd_start:
        return _fail(f"ytd_start={ytd_start} != recomputed {recomputed_ytd} from asof_date={asof_str}")
    notes.append("mtd_start/ytd_start recomputation matches meta")
    return _ok("; ".join(notes))


# ---------------------------------------------------------------------------
# (f) picklist stability
# ---------------------------------------------------------------------------

def _diff_picklist(prev_map: dict, cur_map: dict):
    prev_ids, cur_ids = set(prev_map), set(cur_map)
    added = sorted(cur_ids - prev_ids)
    removed = sorted(prev_ids - cur_ids)
    renamed = sorted(i for i in (prev_ids & cur_ids) if prev_map[i] != cur_map[i])
    return added, removed, renamed


def check_f_picklist_stable(D: dict, prev_state: dict) -> dict:
    snap = D.get("picklist_snapshot")
    if not snap:
        return _fail("picklist_snapshot missing from this run's output")

    prev_snap = (prev_state or {}).get("picklist_snapshot")
    if not prev_snap:
        return _ok("no prior state -- picklist_snapshot recorded for future comparison")

    problems = []
    for category in ("channels", "regions"):
        cur_map = snap.get(category, {})
        prev_map = prev_snap.get(category, {})
        added, removed, renamed = _diff_picklist(prev_map, cur_map)
        if added:
            problems.append(f"{category} added: {added}")
        if removed:
            problems.append(f"{category} removed: {removed}")
        if renamed:
            details = [f"{i} ({prev_map[i]!r}->{cur_map[i]!r})" for i in renamed]
            problems.append(f"{category} renamed: {details}")

    if problems:
        return _fail("; ".join(problems) + " -- acknowledge in run_log before publish")
    return _ok("channel and region id->name maps unchanged from prior state")


# ---------------------------------------------------------------------------
# (g) closed months stable
# ---------------------------------------------------------------------------

def _closed_month_totals(D: dict) -> dict:
    """{ym: {revenue, ntxn}} summed across every channel row (incl. Unassigned) in
    pnl_by_channel_month -- the same channel-summed figure self_check already validates
    footed to the unsegmented total, so it is a trustworthy month total."""
    out: dict = {}
    for row in D.get("pnl_by_channel_month", []) or []:
        ym = row.get("ym")
        e = out.setdefault(ym, {"revenue": 0.0, "cogs": 0.0, "ntxn": 0})
        e["revenue"] += row.get("revenue", 0.0) or 0.0
        e["cogs"] += row.get("cogs", 0.0) or 0.0
        e["ntxn"] += row.get("ntxn", 0) or 0
    return out


def _known_artifact_months(D: dict) -> set:
    months = set()
    for art in D.get("meta", {}).get("known_artifacts", []) or []:
        ship_day = art.get("ship_day")
        if ship_day and len(ship_day) >= 7:
            months.add(ship_day[:7])
    return months


def check_g_closed_months_stable(D: dict, prev_state: dict) -> dict:
    meta = D.get("meta", {})
    trailing = meta.get("trailing_months", []) or []
    if len(trailing) < 2:
        return _ok("fewer than 2 trailing months present; no closed months to compare")
    closed_months = trailing[:-1]  # every month before asof's month

    prev_closed = (prev_state or {}).get("closed_months")
    if not prev_closed:
        return _ok("no prior state -- closed-month totals recorded for future comparison")

    cur_totals = _closed_month_totals(D)
    known_artifact_months = _known_artifact_months(D)

    problems = []
    checked = []
    for ym in closed_months:
        prev = prev_closed.get(ym)
        if not prev:
            checked.append(f"{ym}: no prior data")
            continue
        cur = cur_totals.get(ym, {"revenue": 0.0, "ntxn": 0})
        prev_rev = prev.get("revenue", 0.0) or 0.0
        cur_rev = cur.get("revenue", 0.0) or 0.0
        rev_base = abs(prev_rev) if prev_rev else abs(cur_rev)
        rev_swing_pct = (abs(cur_rev - prev_rev) / rev_base * 100.0) if rev_base else 0.0

        prev_ntxn = prev.get("ntxn", 0) or 0
        cur_ntxn = cur.get("ntxn", 0) or 0
        ntxn_base = abs(prev_ntxn) if prev_ntxn else abs(cur_ntxn)
        ntxn_swing_pct = (abs(cur_ntxn - prev_ntxn) / ntxn_base * 100.0) if ntxn_base else 0.0

        exceeds = rev_swing_pct > CLOSED_MONTH_TOLERANCE_PCT or ntxn_swing_pct > CLOSED_MONTH_TOLERANCE_PCT
        detail = (f"{ym}: revenue swing {rev_swing_pct:.2f}% (prev {prev_rev} -> {cur_rev}), "
                  f"ntxn swing {ntxn_swing_pct:.2f}% (prev {prev_ntxn} -> {cur_ntxn})")
        if exceeds:
            if ym in known_artifact_months:
                checked.append(f"{detail} -- covered by meta.known_artifacts")
            else:
                problems.append(f"{detail} exceeds {CLOSED_MONTH_TOLERANCE_PCT}% and is not in meta.known_artifacts")
        else:
            checked.append(detail)

    if problems:
        return _fail("; ".join(problems))
    return _ok("; ".join(checked) if checked else "no closed months with prior data to compare")


# ---------------------------------------------------------------------------
# t5: BOM rule
# ---------------------------------------------------------------------------

def check_t5_bom_rule(D: dict) -> dict:
    """T5, redefined -- proves the Income-line join METHOD rather than banning SKUs by name
    (see the module-level comment above T5_SENTINEL_SKUS for why the original formulation
    was a false positive). Two parts:
    (1) structural, over every sku_sales row: no row has revenue==0 (a true BOM-consumption
        line has no GL posting and cannot produce a matching row at all, so this can only
        fail if the Income-join rule itself broke).
    (2) per T5_SENTINEL_SKUS, reading extract.py's meta.t5_sentinels (built live against
        NetSuite -- this check itself never touches NetSuite, only the JSON already
        produced): the Income-joined YTD units must be <= T5_DOMINANCE_RATIO of naive_qty
        (every line touching the item, no Income filter) -- proving component consumption
        dominates the naive figure and the join is doing real filtering, not passing
        everything through. When a real list price is on file, realized avg price must also
        fall within T5_PRICE_BAND of it (skipped, not failed, when no list price exists or
        units are 0). Detail always names both numbers per SKU."""
    sku_sales = D.get("sku_sales") or {}
    rows = (sku_sales.get("mtd") or []) + (sku_sales.get("ytd") or [])
    if not rows and (sku_sales.get("mtd") is None or sku_sales.get("ytd") is None):
        return _fail("sku_sales section missing or empty (build error) -- cannot verify T5")

    zero_revenue_rows = [r for r in rows if r.get("revenue") == 0]
    problems = []
    details = []
    if zero_revenue_rows:
        sample = [(r.get("channel"), r.get("sku")) for r in zero_revenue_rows[:5]]
        problems.append(f"{len(zero_revenue_rows)} sku_sales row(s) with revenue==0 (Income-join rule broke), e.g. {sample}")
    else:
        details.append(f"{len(rows)} sku_sales rows checked, no zero-revenue rows (Income-join rule intact)")

    sentinels = D.get("meta", {}).get("t5_sentinels")
    if not sentinels:
        problems.append("meta.t5_sentinels missing -- cannot prove the Income-join method against the sentinel SKUs")
    else:
        for sku in T5_SENTINEL_SKUS:
            s = sentinels.get(sku)
            if not s:
                problems.append(f"{sku}: missing from meta.t5_sentinels")
                continue
            naive = s.get("naive_qty_ytd_all_lines")
            units = abs(s.get("sku_sales_units_ytd") or 0.0)
            if naive is None:
                problems.append(f"{sku}: item id not found in NetSuite -- cannot compute naive_qty")
                continue
            naive_abs = abs(naive)
            ratio = round(units / naive_abs, 4) if naive_abs else None
            base = (f"{sku}: sku_sales_units(Income-join)={units}, naive_qty(all lines, no Income filter)={naive_abs}"
                    f"{f', ratio={ratio}' if ratio is not None else ''}")
            dominance_ok = (units <= T5_DOMINANCE_RATIO * naive_abs) if naive_abs else (units == 0)
            if not dominance_ok:
                problems.append(f"{base} -- Income-join units exceed {T5_DOMINANCE_RATIO * 100:.0f}% of naive_qty "
                                 f"(component consumption should dominate; the join may be under-filtering)")
                continue

            list_price = s.get("list_price_reference")
            revenue = s.get("sku_sales_revenue_ytd") or 0.0
            if list_price and units:
                avg_price = revenue / units
                lo, hi = T5_PRICE_BAND[0] * list_price, T5_PRICE_BAND[1] * list_price
                price_note = f"avg_realized_price={round(avg_price, 2)} vs list_price={list_price} (band [{round(lo, 2)},{round(hi, 2)}])"
                if lo <= avg_price <= hi:
                    details.append(f"{base}; {price_note}, within band")
                else:
                    problems.append(f"{base}; {price_note} -- outside plausible price band")
            else:
                details.append(f"{base}; price-consistency sub-check skipped (no list price on file or zero units)")

    if problems:
        return _fail("; ".join(problems))
    return _ok("; ".join(details))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_checks(D: dict, prev_state: dict | None = None) -> dict:
    prev_state = prev_state or {}
    checks = {
        "a_channel_foot": check_a_channel_foot(D),
        "b_inventory_tie": check_b_inventory_tie(D),
        "c_rows_present": check_c_rows_present(D, prev_state),
        "d_fresh": check_d_fresh(D),
        "e_asof": check_e_asof(D),
        "f_picklist_stable": check_f_picklist_stable(D, prev_state),
        "g_closed_months_stable": check_g_closed_months_stable(D, prev_state),
        "t5_bom_rule": check_t5_bom_rule(D),
    }
    checks["all_pass"] = all(v["pass"] for v in checks.values())
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Run FD1a E2 self-checks against an already-written latest.json.")
    parser.add_argument("--data", required=True, help="Path to a latest.json-shaped file")
    parser.add_argument("--prev-state", default=None, help="Path to a prior --write-state file")
    args = parser.parse_args()

    D = json.loads(Path(args.data).read_text(encoding="utf-8"))
    prev_state = {}
    if args.prev_state:
        p = Path(args.prev_state)
        if p.exists():
            prev_state = json.loads(p.read_text(encoding="utf-8"))

    result = run_checks(D, prev_state)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["all_pass"] else 1)


if __name__ == "__main__":
    main()
