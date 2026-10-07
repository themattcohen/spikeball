"""Pure rules for the monthly gross-margin reconciliation: dates and months, FX,
settlement fee status, settlement-to-journal mapping, sales-month precedence,
manual-journal classification, retailer program-period inference, and accrual math.

No I/O, no network, no clock reads except where a `now` argument defaults to the
current time. All amounts are floats in USD unless a name says otherwise.
"""
from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MT = ZoneInfo("America/Denver")
UTC = ZoneInfo("UTC")

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]


# ---------------------------------------------------------------------------
# Dates and months
# ---------------------------------------------------------------------------

def parse_ns_date(s) -> date | None:
    """NetSuite renders dates as M/D/YYYY; TO_CHAR columns as YYYY-MM-DD. Returns None
    for empty input; raises ValueError on anything else unparseable."""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    if "/" in s:
        m, d, y = s.split(" ")[0].split("/")
        return date(int(y), int(m), int(d))
    return date.fromisoformat(s[:10])


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def parse_month(ym: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", ym or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"month must be YYYY-MM, got {ym!r}")
    return int(m.group(1)), int(m.group(2))


def add_months(ym: str, n: int) -> str:
    y, m = parse_month(ym)
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_bounds(ym: str) -> tuple[date, date]:
    """(first day, last day) of the month."""
    y, m = parse_month(ym)
    first = date(y, m, 1)
    ny, nm = parse_month(add_months(ym, 1))
    return first, date(ny, nm, 1) - timedelta(days=1)


def month_label(ym: str) -> str:
    y, m = parse_month(ym)
    return f"{MONTH_NAMES[m - 1]} {y}"


def now_mt(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(MT)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(MT)


def default_month(now: datetime | None = None) -> str:
    """The previous calendar month in America/Denver as of `now`."""
    n = now_mt(now)
    return add_months(month_key(n.date()), -1)


def default_asof(now: datetime | None = None) -> date:
    return now_mt(now).date()


def fmt_mdy(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def fmt_md(d: date) -> str:
    return f"{d.month}/{d.day}"


def utc_iso_to_mt_date(s: str) -> date | None:
    """Amazon PurchaseDate (ISO 8601, Z) to the America/Denver calendar date."""
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(MT).date()


def utc_iso_to_utc_date(s: str) -> date | None:
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).date()


# ---------------------------------------------------------------------------
# Numbers and FX
# ---------------------------------------------------------------------------

def num(v) -> float:
    """Strict float parse for ledger values: None or "" is 0.0; anything else that is
    not a number raises (a reconciliation must not hide bad values as zero)."""
    if v is None or v == "":
        return 0.0
    return float(v)


def fx_rate(currency: str, rates: dict) -> float:
    if currency not in rates:
        raise KeyError(f"no fixed FX rate for currency {currency!r}")
    return float(rates[currency])


def to_usd(amount_local: float, currency: str, rates: dict) -> float:
    return amount_local * fx_rate(currency, rates)


def r2(x: float) -> float:
    return round(x + 0.0, 2)


# ---------------------------------------------------------------------------
# Settlement fees
# ---------------------------------------------------------------------------

def fee_status(feeall: float, rows_fee_usd: float, mapped: bool, tol: float = 0.01) -> str:
    """full / partial / none / no_journal, the review's rule."""
    if not mapped:
        return "no_journal"
    if rows_fee_usd and abs(feeall / rows_fee_usd - 1) < tol:
        return "full"
    if abs(feeall) < 0.005:
        return "none"
    return "partial"


def summarize_journal(lines: list[dict], memo_cfg: dict) -> dict:
    """Per-journal fee figures from its posting lines (dr/cr, acct, lmemo)."""
    fee_prefix = memo_cfg["fee_line_prefix"]

    def net(l):
        return num(l.get("dr")) - num(l.get("cr"))

    def is_fee(l):
        return (l.get("lmemo") or "").startswith(fee_prefix)

    fee_accts: dict[str, float] = {}
    for l in lines:
        if is_fee(l) and str(l.get("acct", "")).startswith("5"):
            fee_accts[l["acct"]] = fee_accts.get(l["acct"], 0.0) + net(l)
    return {
        "bank": sum(net(l) for l in lines if (l.get("lmemo") or "") == memo_cfg["deposit_line"]),
        "feeall": sum(net(l) for l in lines if is_fee(l)),
        "fee5": sum(net(l) for l in lines if is_fee(l) and str(l.get("acct", "")).startswith("5")),
        "fee_outside": sum(net(l) for l in lines if is_fee(l) and not str(l.get("acct", "")).startswith("5")),
        "var": sum(net(l) for l in lines if (l.get("lmemo") or "") == memo_cfg["variance_line"]),
        "fee_accounts": fee_accts,
        "bank_accounts": sorted({l["acct"] for l in lines
                                 if (l.get("lmemo") or "") == memo_cfg["deposit_line"] and net(l) > 0}),
    }


def map_settlements_to_journals(settlements: list[dict], journals: dict, tol: float = 1.0,
                                fx: dict | None = None, max_gap_days: int = 3) -> tuple[dict, dict]:
    """Maps settlement summary id -> journal tranid (or None). Returns (mapping, rule).

    Pass 1, the rule in force since 7/1: journal date equals deposit date. When more
    than one unused journal shares the date, the one whose bank deposit line matches
    the settlement total within `tol` wins.
    Pass 2, for settlements still unmapped (a journal re-dated after creation): an
    unused journal dated 1 to `max_gap_days` days after the deposit qualifies when its
    bank line equals the deposit (USD, within `tol`) or its fee lines equal the
    settlement-row fees at the fixed rate (within 0.3 percent). Exactly one candidate
    is required; anything else stays unmapped.

    settlements: [{"id", "dd": date, "total_amt", "currency", "rows_fee_local"}]
    journals: {tranid: {"d": date, "bank": float, "feeall": float}}
    """
    bydate: dict[date, list[str]] = {}
    for t, j in journals.items():
        bydate.setdefault(j["d"], []).append(t)
    used: set[str] = set()
    out: dict[str, str | None] = {}
    rule: dict[str, str] = {}
    ordered = sorted(settlements, key=lambda x: (x["dd"], str(x["id"])))
    for s in ordered:
        cands = [t for t in sorted(bydate.get(s["dd"], [])) if t not in used]
        pick = None
        if len(cands) == 1:
            pick = cands[0]
        elif len(cands) > 1:
            m = [t for t in cands if abs(journals[t]["bank"] - s["total_amt"]) <= tol]
            if len(m) == 1:
                pick = m[0]
        out[str(s["id"])] = pick
        rule[str(s["id"])] = "journal date equals deposit date" if pick else "no journal found"
        if pick:
            used.add(pick)
    for s in ordered:
        sid = str(s["id"])
        if out[sid]:
            continue
        rate = (fx or {}).get(s.get("currency") or "USD")
        fee_usd = (s.get("rows_fee_local") or 0.0) * rate if rate else None
        cands = []
        for t, j in sorted(journals.items()):
            if t in used:
                continue
            gap = (j["d"] - s["dd"]).days
            if not 0 < gap <= max_gap_days:
                continue
            bank_ok = ((s.get("currency") or "USD") == "USD" and bool(j["bank"])
                       and abs(j["bank"] - s["total_amt"]) <= tol)
            fee_ok = bool(fee_usd) and bool(j.get("feeall")) and abs(j["feeall"] / fee_usd - 1) < 0.003
            if bank_ok or fee_ok:
                why = "bank deposit equals settlement total" if bank_ok else "fee lines equal settlement fees"
                cands.append((t, gap, why))
        if len(cands) == 1:
            t, gap, why = cands[0]
            out[sid] = t
            rule[sid] = f"journal dated {gap} day(s) after deposit; {why}"
            used.add(t)
    return out, rule


def choose_fee_cogs_account(full_journal_fee_accounts: list[dict], default: str) -> str:
    """The COGS account the month's full settlement journals post fee lines to (the
    largest by amount), else the default."""
    tot: dict[str, float] = {}
    for fa in full_journal_fee_accounts:
        for acct, amt in fa.items():
            tot[acct] = tot.get(acct, 0.0) + abs(amt)
    if not tot:
        return default
    return max(sorted(tot), key=lambda a: tot[a])


# ---------------------------------------------------------------------------
# Sales month
# ---------------------------------------------------------------------------

def sales_month(order_id: str, legacy_dates: dict, cache_mt_dates: dict, posted: date | None) -> tuple[str, str]:
    """(YYYY-MM, source): legacy invoice trandate, else the Amazon PurchaseDate in
    America/Denver from the orders cache, else the settlement row's posted date."""
    d = legacy_dates.get(order_id)
    if d is not None:
        return month_key(d), "legacy_invoice"
    d = cache_mt_dates.get(order_id)
    if d is not None:
        return month_key(d), "amazon_cache"
    if posted is not None:
        return month_key(posted), "posted_date"
    return "unknown", "none"


# ---------------------------------------------------------------------------
# Manual journals
# ---------------------------------------------------------------------------

ACCRUAL_WITH_REVERSAL = "accrual_with_reversal"
TRUE_UP_NO_REVERSAL = "true_up_no_reversal"
EQUITY_OR_SGA = "equity_or_sga_counterpart"
NETZERO_RECLASS = "netzero_reclass"
OTHER = "other"
REVIEW_CLASSES = (TRUE_UP_NO_REVERSAL, EQUITY_OR_SGA)


def _memo_text(j: dict) -> str:
    parts = [j.get("hmemo") or ""] + [l.get("lmemo") or "" for l in j.get("lines", [])]
    return " ".join(parts).lower()


def _is_pl(l: dict) -> bool:
    return l.get("atype") in ("Income", "COGS")


def journal_net_by_side(j: dict) -> tuple[float, float]:
    """(income+COGS ledger net, counterpart ledger net), debit positive."""
    pl = sum(num(l.get("dr")) - num(l.get("cr")) for l in j["lines"] if _is_pl(l))
    other = sum(num(l.get("dr")) - num(l.get("cr")) for l in j["lines"] if not _is_pl(l))
    return pl, other


def classify_manual_journal(j: dict, has_reversal_link: bool, opposite_within_window: bool,
                            cfg: dict) -> str:
    """j: {"hmemo", "lines": [{"acct", "atype", "dr", "cr", "lmemo"}]}.
    Precedence follows the spec order: accrual with reversal, true-up without
    reversal, equity or SG&A counterpart, net-zero reclass, other."""
    memo = _memo_text(j)
    accrual_words = [w.lower() for w in cfg["accrual_words"]]
    true_up_words = [w.lower() for w in cfg["true_up_words"]]
    if has_reversal_link or (any(w in memo for w in accrual_words) and opposite_within_window):
        return ACCRUAL_WITH_REVERSAL
    if any(w in memo for w in true_up_words):
        return TRUE_UP_NO_REVERSAL
    counter = [l for l in j["lines"] if not _is_pl(l)]
    if any(str(l.get("acct", ""))[:1] in tuple(cfg["counterpart_prefixes_review"]) for l in counter):
        return EQUITY_OR_SGA
    pl, _ = journal_net_by_side(j)
    if not counter and abs(pl) < 0.005:
        return NETZERO_RECLASS
    return OTHER


def review_question(cls: str) -> str:
    if cls == TRUE_UP_NO_REVERSAL:
        return ("Is this a correction that should stay, or an accrual that needs a reversal next month?")
    if cls == EQUITY_OR_SGA:
        return ("The other side is an equity or SG&A account. Is that the intended account, "
                "or should it be a liability or a different P&L line?")
    return ""


# ---------------------------------------------------------------------------
# Retailer program period
# ---------------------------------------------------------------------------

_Q_END = {1: 3, 2: 6, 3: 9, 4: 12}
_MON_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b\.?\s*(20\d{2})?",
    re.IGNORECASE)
_MON_IDX = {n.lower(): i + 1 for i, n in enumerate(MONTH_NAMES)}
_MON_IDX.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
                 "sept": 9, "oct": 10, "nov": 11, "dec": 12})
_Q_RANGE_RE = re.compile(r"(?<![A-Za-z0-9])Q([1-4])\s*(?:-|to|/|&)\s*Q([1-4])(?![0-9])(?:\s*(20\d{2}))?",
                         re.IGNORECASE)
_Q_YEAR_PREFIX_RE = re.compile(r"(20\d{2})\s*Q([1-4])", re.IGNORECASE)
_Q_RE = re.compile(r"(?<![A-Za-z0-9])Q([1-4])(?![0-9])(?:\s*(?:FY)?\s*(20\d{2}))?", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<![0-9])(20\d{2})(?![0-9])")


def infer_program_period(memo: str, bill_date: date, created_date: date | None,
                         early_create_days: int = 28) -> list[tuple[str, float]] | None:
    """Returns [(YYYY-MM, share), ...] with shares summing to 1, or None when no period
    is inferable (controller assigns it).

    Rules, in order:
    1. A quarter range ("Q2-Q3") splits evenly across the quarter-end months.
    2. A quarter ("Q2 2026", "2025Q4") gives its quarter-end month (stated year, else
       the bill year).
    3. A month name ("June 2026") gives that month (stated year, else a year named
       elsewhere in the memo, else the bill year).
    4. A bill created at least `early_create_days` days before its transaction date, in
       an earlier month, belongs to the creation month.
    5. A bare year that ended before the bill year gives December of that year.
    """
    text = memo or ""
    by = bill_date.year
    m = _Q_RANGE_RE.search(text)
    if m:
        q1, q2 = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else by
        if q2 < q1:
            q1, q2 = q2, q1
        qs = list(range(q1, q2 + 1))
        share = 1.0 / len(qs)
        return [(f"{y:04d}-{_Q_END[q]:02d}", share) for q in qs]
    m = _Q_YEAR_PREFIX_RE.search(text)
    if m:
        return [(f"{int(m.group(1)):04d}-{_Q_END[int(m.group(2))]:02d}", 1.0)]
    years = [int(y) for y in _YEAR_RE.findall(text)]
    m = _Q_RE.search(text)
    if m:
        y = int(m.group(2)) if m.group(2) else (years[0] if years else by)
        return [(f"{y:04d}-{_Q_END[int(m.group(1))]:02d}", 1.0)]
    m = _MON_RE.search(text)
    if m:
        mon = _MON_IDX[m.group(1).lower().rstrip(".")]
        y = int(m.group(2)) if m.group(2) else (years[0] if years else by)
        return [(f"{y:04d}-{mon:02d}", 1.0)]
    if created_date is not None and (bill_date - created_date).days >= early_create_days \
            and month_key(created_date) < month_key(bill_date):
        return [(month_key(created_date), 1.0)]
    if years and max(years) < by:
        return [(f"{max(years):04d}-12", 1.0)]
    return None


def split_amount(amount: float, shares: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Splits to the cent; the first share is rounded half up, the last takes the
    remainder (the review's Q2-Q3 rule)."""
    from decimal import Decimal, ROUND_HALF_UP
    amt = Decimal(str(round(amount, 2)))
    out = []
    left = amt
    for i, (period, share) in enumerate(shares):
        if i == len(shares) - 1:
            part = left
        else:
            part = (amt * Decimal(str(share))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            left -= part
        out.append((period, float(part)))
    return out


# ---------------------------------------------------------------------------
# Accrual math
# ---------------------------------------------------------------------------

def median_fee(fees: list[float]) -> float:
    return float(statistics.median(fees)) if fees else 0.0


def unsettled_estimate(count_unsettled: int, median: float) -> float:
    return r2(count_unsettled * median)


def accrual_amount(measured_after: float, estimate: float, existing_accrual: float) -> float:
    """Fees on the month's sales still to be recognized at month end: measured fees
    settled after month end, plus the estimate for fees not yet settled, less any
    accrual already booked at month end."""
    return r2(measured_after + estimate - existing_accrual)


def gm_pct(income: float, cogs: float) -> float | None:
    if abs(income) < 0.005:
        return None
    return (income - cogs) / income


def whole_dollars(x: float) -> str:
    """Whole dollars with thousands separators, no cents (prose and email rule)."""
    v = int(round(x))
    return f"-{abs(v):,}" if v < 0 else f"{v:,}"
