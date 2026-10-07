"""Every NetSuite read the monthly gross-margin reconciliation makes.

READ-ONLY. Each function issues SuiteQL SELECT statements only (asserted in `_q`) and
returns list[dict] with the REST `links` key removed. Every query is date-scoped by its
arguments; nothing is hardcoded to a month. Dates are `datetime.date` values.

Shared traps (verified live):
- Amounts and the account live on `transactionaccountingline` (posting='T'), joined to
  `transactionline` on (transaction, transactionline). Journal lines carry
  mainline='T', so no mainline filter is applied to journal reads.
- Income is credit-positive, COGS debit-positive. `amount` here is the ledger sign
  (debit positive); callers flip Income.
- `createddate` renders on America/Chicago; date math is done client side.
- Paginated queries carry a unique ORDER BY (transaction id, line id).
- Null columns are absent from the returned dicts; callers use .get().

`suiteql` is imported by name so tests can monkeypatch `ns_queries.suiteql`.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPIKE = _HERE.parent
for _p in (_SPIKE, _SPIKE / "routine", _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _lib import suiteql  # noqa: E402

CHUNK = 500
_ORDER_ID_RE = re.compile(r"^[0-9]{3}-[0-9]{7}-[0-9]{7}$")
_TRANID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_INT_RE = re.compile(r"^[0-9]{1,15}$")

SUMMARY_FIELDS = {
    "settl_id": "custrecord_celigo_amzio_set_sum_settl_id",
    "acco": "custrecord_celigo_amzio_set_sum_amz_acco",
    "cur": "custrecord_celigo_amzio_set_sum_settl_cu",
    "sd": "custrecord_celigo_amzio_set_sum_settl_sd",
    "ed": "custrecord_celigo_amzio_set_sum_settl_ed",
    "dd": "custrecord_celigo_amzio_set_sum_settl_dd",
    "total_amt": "custrecord_celigo_amzio_set_sum_settl_ta",
    "o_fee": "custrecord_celigo_amzio_sett_sum_to_o_fe",
    "r_fee": "custrecord_celigo_amzio_sett_sum_to_r_fe",
}

ROW_FIELDS = {
    "o": "custrecord_celigo_amzio_set_mer_order_id",
    "ty": "custrecord_celigo_amzio_set_tran_type",
    "pd": "custrecord_celigo_amzio_set_posted_date",
    "sm": "custrecord_celigo_amzio_set_summary",
    "fee": "custrecord_celigo_amzio_set_total_fee",
    "pc": "custrecord_celigo_amzio_set_total_prod_c",
    "acct": "custrecord_celigo_amzio_set_amz_account",
}


class ReadOnlyViolation(AssertionError):
    pass


def _q(env, sql: str) -> list[dict]:
    if not sql.strip().lower().startswith("select"):
        raise ReadOnlyViolation("read-only: SELECT statements only")
    rows = suiteql(env, sql) or []
    for r in rows:
        r.pop("links", None)
    return rows


def _d(d: date) -> str:
    if not isinstance(d, date):
        raise TypeError(f"expected a date, got {type(d).__name__}")
    return f"TO_DATE('{d.isoformat()}','YYYY-MM-DD')"


def _chunks(seq, n=CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _ints(ids) -> list[str]:
    out = []
    for i in ids:
        s = str(i)
        if not _INT_RE.match(s):
            raise ValueError(f"not an internal id: {s!r}")
        out.append(s)
    return out


def _strs(vals, pattern) -> list[str]:
    out = []
    for v in vals:
        s = str(v)
        if not pattern.match(s):
            raise ValueError(f"value rejected by input validation: {s!r}")
        out.append("'" + s + "'")
    return out


_LINE_JOIN = """FROM transactionaccountingline ai
JOIN transactionline tl ON tl.transaction = ai.transaction AND tl.id = ai.transactionline
JOIN transaction t ON t.id = ai.transaction
JOIN account a ON a.id = ai.account"""


# ---------------------------------------------------------------------------
# P&L as booked
# ---------------------------------------------------------------------------

def pnl_by_account(env, period_start: date, asof: date) -> list[dict]:
    """Income and COGS posting totals by account for the accounting period starting on
    period_start, transactions dated on or before asof, all subsidiaries.
    Keys: acct, name, typ, amt (ledger sign, debit positive)."""
    sql = f"""SELECT a.acctnumber AS acct, a.fullname AS name, a.accttype AS typ, SUM(ai.amount) AS amt
FROM transactionaccountingline ai
JOIN transaction t ON t.id = ai.transaction
JOIN accountingperiod ap ON ap.id = t.postingperiod
JOIN account a ON a.id = ai.account
WHERE ai.posting = 'T' AND a.accttype IN ('Income','COGS')
AND ap.startdate = {_d(period_start)} AND t.trandate <= {_d(asof)}
GROUP BY a.acctnumber, a.fullname, a.accttype
ORDER BY a.acctnumber"""
    return _q(env, sql)


# ---------------------------------------------------------------------------
# Settlements (Celigo custom records read through NetSuite)
# ---------------------------------------------------------------------------

def settlement_summaries(env, dd_from: date, dd_to: date) -> list[dict]:
    cols = ", ".join(f"{v} AS {k}" for k, v in SUMMARY_FIELDS.items())
    sql = f"""SELECT id, {cols}, isinactive
FROM customrecord_celigo_amzio_sett_summary
WHERE custrecord_celigo_amzio_set_sum_settl_dd >= {_d(dd_from)}
AND custrecord_celigo_amzio_set_sum_settl_dd <= {_d(dd_to)}
ORDER BY id"""
    return _q(env, sql)


def settlement_rows(env, summary_ids) -> list[dict]:
    """Order (1) and refund (2) rows of the given settlement summaries."""
    ids = _ints(summary_ids)
    cols = ", ".join(f"{v} AS {k}" for k, v in ROW_FIELDS.items())
    out = []
    for ch in _chunks(ids, 20):
        sql = f"""SELECT id, {cols}
FROM customrecord_celigo_amzio_settle_trans
WHERE custrecord_celigo_amzio_set_summary IN ({",".join(ch)})
AND custrecord_celigo_amzio_set_tran_type IN (1,2)
ORDER BY id"""
        out += _q(env, sql)
    return out


def settlement_journal_lines(env, creator: str, d_from: date, d_to: date) -> list[dict]:
    """Posting lines of journals created by the settlement integration user."""
    if "'" in creator:
        raise ValueError("creator name rejected by input validation")
    sql = f"""SELECT t.id AS tid, t.tranid AS tranid, t.trandate AS d, tl.id AS lineid, a.acctnumber AS acct,
a.fullname AS acctname, a.accttype AS atype, ai.debit AS dr, ai.credit AS cr, tl.memo AS lmemo
{_LINE_JOIN}
WHERE ai.posting = 'T' AND t.type = 'Journal' AND BUILTIN.DF(t.createdby) = '{creator}'
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
ORDER BY t.id, tl.id"""
    return _q(env, sql)


# ---------------------------------------------------------------------------
# Manual journals
# ---------------------------------------------------------------------------

def manual_journal_lines(env, creator: str, d_from: date, d_to: date, extra_accounts=()) -> list[dict]:
    """Every posting line of journals NOT created by `creator`, dated in the window, that
    carry at least one Income-type or COGS-type line (or a line on one of
    `extra_accounts`, used to find accrual journals on the accrued-liability account)."""
    if "'" in creator:
        raise ValueError("creator name rejected by input validation")
    extra = _strs(extra_accounts, _TRANID_RE)
    extra_cond = f" OR a2.acctnumber IN ({','.join(extra)})" if extra else ""
    sql = f"""SELECT t.id AS tid, t.tranid AS tranid, t.trandate AS d, t.createddate AS cdate,
BUILTIN.DF(t.createdby) AS cb, t.memo AS hmemo, t.reversal AS rev_id, t.isreversal AS isrev,
t.reversaldate AS revdate, tl.id AS lineid, a.acctnumber AS acct, a.fullname AS acctname,
a.accttype AS atype, ai.debit AS dr, ai.credit AS cr, tl.memo AS lmemo
{_LINE_JOIN}
WHERE ai.posting = 'T' AND t.type = 'Journal' AND BUILTIN.DF(t.createdby) <> '{creator}'
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
AND t.id IN (SELECT ai2.transaction FROM transactionaccountingline ai2
  JOIN account a2 ON a2.id = ai2.account JOIN transaction t2 ON t2.id = ai2.transaction
  WHERE ai2.posting = 'T' AND t2.type = 'Journal'
  AND t2.trandate >= {_d(d_from)} AND t2.trandate <= {_d(d_to)}
  AND (a2.accttype IN ('Income','COGS'){extra_cond}))
ORDER BY t.id, tl.id"""
    return _q(env, sql)


def journal_headers(env, ids, d_from: date, d_to: date) -> list[dict]:
    """Headers of the given journal internal ids (reversal partners), date-scoped."""
    ids = _ints(ids)
    out = []
    for ch in _chunks(ids):
        sql = f"""SELECT t.id AS tid, t.tranid AS tranid, t.trandate AS d, BUILTIN.DF(t.createdby) AS cb,
t.reversal AS rev_id, t.isreversal AS isrev, t.memo AS hmemo
FROM transaction t
WHERE t.type = 'Journal' AND t.id IN ({",".join(ch)})
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
ORDER BY t.id"""
        out += _q(env, sql)
    return out


# ---------------------------------------------------------------------------
# Entries created after period end
# ---------------------------------------------------------------------------

def late_created_lines(env, period_start: date, asof: date) -> list[dict]:
    """Income and COGS posting totals per transaction in the period starting on
    period_start whose creation DAY is after the period end (day-level rule), dated on
    or before asof. Keys: tid, tranid, ttype, d, cdate, atype, amt (ledger sign)."""
    sql = f"""SELECT t.id AS tid, t.tranid AS tranid, t.type AS ttype, t.trandate AS d,
TO_CHAR(t.createddate,'YYYY-MM-DD') AS cdate, BUILTIN.DF(t.createdby) AS cb, a.accttype AS atype,
SUM(ai.amount) AS amt
FROM transactionaccountingline ai
JOIN transaction t ON t.id = ai.transaction
JOIN accountingperiod ap ON ap.id = t.postingperiod
JOIN account a ON a.id = ai.account
WHERE ai.posting = 'T' AND a.accttype IN ('Income','COGS')
AND ap.startdate = {_d(period_start)} AND TRUNC(t.createddate) > ap.enddate
AND t.trandate <= {_d(asof)}
GROUP BY t.id, t.tranid, t.type, t.trandate, TO_CHAR(t.createddate,'YYYY-MM-DD'), BUILTIN.DF(t.createdby), a.accttype
ORDER BY t.id, a.accttype"""
    return _q(env, sql)


# ---------------------------------------------------------------------------
# Invoices and credit memos (Amazon)
# ---------------------------------------------------------------------------

def legacy_invoice_dates(env, order_ids, d_from: date, d_to: date) -> list[dict]:
    """Legacy (per-order) Amazon invoices for the given order ids. Keys: oid, d, tid."""
    ids = sorted(set(order_ids))
    quoted = _strs([i for i in ids if _ORDER_ID_RE.match(str(i))], _ORDER_ID_RE)
    out = []
    for ch in _chunks(quoted):
        sql = f"""SELECT t.id AS tid, t.custbody_celigo_etail_order_id AS oid, t.trandate AS d
FROM transaction t
WHERE t.type = 'CustInvc' AND t.custbody_celigo_etail_order_id IN ({",".join(ch)})
AND (t.otherrefnum IS NULL OR t.otherrefnum NOT LIKE 'DAILY-FBA-%')
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
ORDER BY t.id"""
        out += _q(env, sql)
    return out


def amazon_credit_memos(env, d_from: date, d_to: date) -> list[dict]:
    """Amazon credit memos (order id set or consolidated MC-CM externalid) dated in the
    window, with Income and COGS posting sums. Keys: tid, tranid, ext, oid, d, memo,
    inc, cogs (ledger sign)."""
    sql = f"""SELECT t.id AS tid, t.tranid AS tranid, t.externalid AS ext, t.custbody_celigo_etail_order_id AS oid,
t.trandate AS d, t.memo AS memo,
SUM(CASE WHEN a.accttype = 'Income' THEN ai.amount ELSE 0 END) AS inc,
SUM(CASE WHEN a.accttype = 'COGS' THEN ai.amount ELSE 0 END) AS cogs
FROM transactionaccountingline ai
JOIN transaction t ON t.id = ai.transaction
JOIN account a ON a.id = ai.account
WHERE ai.posting = 'T' AND t.type = 'CustCred'
AND (t.custbody_celigo_etail_order_id IS NOT NULL OR t.externalid LIKE 'MC-CM-%')
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
GROUP BY t.id, t.tranid, t.externalid, t.custbody_celigo_etail_order_id, t.trandate, t.memo
ORDER BY t.id"""
    return _q(env, sql)


def duplicate_legacy_documents(env, ttype: str, d_from: date, d_to: date) -> list[dict]:
    """Amazon order ids with more than one legacy CustInvc or CustCred dated in the window
    (consolidated MC-CM credit memos and DAILY-FBA invoices excluded). Keys: oid, n."""
    if ttype not in ("CustInvc", "CustCred"):
        raise ValueError("ttype must be CustInvc or CustCred")
    excl = ("AND (t.otherrefnum IS NULL OR t.otherrefnum NOT LIKE 'DAILY-FBA-%')" if ttype == "CustInvc"
            else "AND (t.externalid IS NULL OR t.externalid NOT LIKE 'MC-CM-%')")
    sql = f"""SELECT t.custbody_celigo_etail_order_id AS oid, COUNT(*) AS n
FROM transaction t
WHERE t.type = '{ttype}' AND t.custbody_celigo_etail_order_id IS NOT NULL {excl}
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
GROUP BY t.custbody_celigo_etail_order_id
HAVING COUNT(*) > 1
ORDER BY t.custbody_celigo_etail_order_id"""
    return _q(env, sql)


def documents_for_orders(env, ttype: str, order_ids, d_from: date, d_to: date) -> list[dict]:
    """Every document of `ttype` for the given order ids in the window, with Income and
    COGS posting sums (ledger sign). Used to price duplicates."""
    if ttype not in ("CustInvc", "CustCred"):
        raise ValueError("ttype must be CustInvc or CustCred")
    quoted = _strs([i for i in sorted(set(order_ids)) if _ORDER_ID_RE.match(str(i))], _ORDER_ID_RE)
    out = []
    for ch in _chunks(quoted):
        sql = f"""SELECT t.id AS tid, t.tranid AS tranid, t.custbody_celigo_etail_order_id AS oid, t.trandate AS d,
t.externalid AS ext, t.otherrefnum AS orn,
SUM(CASE WHEN a.accttype = 'Income' THEN ai.amount ELSE 0 END) AS inc,
SUM(CASE WHEN a.accttype = 'COGS' THEN ai.amount ELSE 0 END) AS cogs
FROM transactionaccountingline ai
JOIN transaction t ON t.id = ai.transaction
JOIN account a ON a.id = ai.account
WHERE ai.posting = 'T' AND t.type = '{ttype}' AND t.custbody_celigo_etail_order_id IN ({",".join(ch)})
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
GROUP BY t.id, t.tranid, t.custbody_celigo_etail_order_id, t.trandate, t.externalid, t.otherrefnum
ORDER BY t.id"""
        out += _q(env, sql)
    return out


def consolidated_invoices(env, d_from: date, d_to: date) -> list[dict]:
    """DAILY-FBA consolidated invoices dated in the window with 40100000 revenue and
    Income and COGS sums. Keys: tid, tranid, orn, d, cur, rev40100 (credit positive),
    inc, cogs (ledger sign)."""
    sql = f"""SELECT t.id AS tid, t.tranid AS tranid, t.otherrefnum AS orn, t.trandate AS d, t.currency AS cur,
SUM(CASE WHEN a.acctnumber = '40100000' THEN -ai.amount ELSE 0 END) AS rev40100,
SUM(CASE WHEN a.accttype = 'Income' THEN ai.amount ELSE 0 END) AS inc,
SUM(CASE WHEN a.accttype = 'COGS' THEN ai.amount ELSE 0 END) AS cogs
FROM transactionaccountingline ai
JOIN transaction t ON t.id = ai.transaction
JOIN account a ON a.id = ai.account
WHERE ai.posting = 'T' AND t.type = 'CustInvc' AND t.otherrefnum LIKE 'DAILY-FBA-%'
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
GROUP BY t.id, t.tranid, t.otherrefnum, t.trandate, t.currency
ORDER BY t.id"""
    return _q(env, sql)


def legacy_invoice_revenue(env, d_from: date, d_to: date) -> list[dict]:
    """Legacy Amazon invoices dated in the window with 40100000 revenue. Keys: tid, oid,
    d, cur, rev40100 (credit positive)."""
    sql = f"""SELECT t.id AS tid, t.custbody_celigo_etail_order_id AS oid, t.trandate AS d, t.currency AS cur,
SUM(CASE WHEN a.acctnumber = '40100000' THEN -ai.amount ELSE 0 END) AS rev40100
FROM transactionaccountingline ai
JOIN transaction t ON t.id = ai.transaction
JOIN account a ON a.id = ai.account
WHERE ai.posting = 'T' AND t.type = 'CustInvc' AND t.custbody_celigo_etail_order_id IS NOT NULL
AND (t.otherrefnum IS NULL OR t.otherrefnum NOT LIKE 'DAILY-FBA-%')
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
GROUP BY t.id, t.custbody_celigo_etail_order_id, t.trandate, t.currency
ORDER BY t.id"""
    return _q(env, sql)


# ---------------------------------------------------------------------------
# Wengo sourcing fees and River Joint purchase orders
# ---------------------------------------------------------------------------

def wengo_bills(env, vendor_pattern: str, d_from: date, d_to: date) -> list[dict]:
    """Wengo VendBill/VendCred headers dated in the window with their COGS posting sum and
    posting month. Keys: tid, tranid, ttype, d, cdate, status, memo, tot, cogs, pmonth."""
    if "'" in vendor_pattern:
        raise ValueError("vendor pattern rejected by input validation")
    sql = f"""SELECT t.id AS tid, t.tranid AS tranid, t.type AS ttype, t.trandate AS d,
TO_CHAR(t.createddate,'YYYY-MM-DD') AS cdate, BUILTIN.DF(t.status) AS status, t.memo AS memo,
t.foreigntotal AS tot
FROM transaction t
WHERE t.type IN ('VendBill','VendCred') AND BUILTIN.DF(t.entity) LIKE '{vendor_pattern}'
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
ORDER BY t.id"""
    heads = _q(env, sql)
    if not heads:
        return []
    ids = [h["tid"] for h in heads]
    gl = {}
    for ch in _chunks(_ints(ids)):
        g = _q(env, f"""SELECT t.id AS tid, TO_CHAR(ap.startdate,'YYYY-MM') AS pmonth, SUM(ai.amount) AS cogs
FROM transactionaccountingline ai
JOIN transaction t ON t.id = ai.transaction
JOIN accountingperiod ap ON ap.id = t.postingperiod
JOIN account a ON a.id = ai.account
WHERE ai.posting = 'T' AND a.accttype = 'COGS' AND t.id IN ({",".join(ch)})
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
GROUP BY t.id, TO_CHAR(ap.startdate,'YYYY-MM')
ORDER BY t.id""")
        for r in g:
            gl[r["tid"]] = r
    for h in heads:
        g = gl.get(h["tid"])
        h["cogs"] = g["cogs"] if g else None
        h["pmonth"] = g["pmonth"] if g else None
    return heads


def purchase_orders_by_tranid(env, tranids, d_from: date, d_to: date) -> list[dict]:
    """PO headers and fee-relevant lines for the given PO numbers. Keys: tid, po, d,
    vendor, lineid, item, qty."""
    quoted = _strs(sorted(set(tranids)), _TRANID_RE)
    out = []
    for ch in _chunks(quoted):
        sql = f"""SELECT t.id AS tid, t.tranid AS po, t.trandate AS d, BUILTIN.DF(t.entity) AS vendor,
tl.id AS lineid, BUILTIN.DF(tl.item) AS item, tl.quantity AS qty
FROM transactionline tl JOIN transaction t ON t.id = tl.transaction
WHERE t.type = 'PurchOrd' AND t.tranid IN ({",".join(ch)}) AND tl.mainline = 'F' AND tl.taxline = 'F'
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
ORDER BY t.id, tl.id"""
        out += _q(env, sql)
    return out


def purchase_orders_by_vendor(env, vendor_pattern: str, d_from: date, d_to: date) -> list[dict]:
    """PO lines for vendor POs dated in the window. Keys: tid, po, d, vendor, lineid,
    item, qty."""
    if "'" in vendor_pattern:
        raise ValueError("vendor pattern rejected by input validation")
    sql = f"""SELECT t.id AS tid, t.tranid AS po, t.trandate AS d, BUILTIN.DF(t.entity) AS vendor,
tl.id AS lineid, BUILTIN.DF(tl.item) AS item, tl.quantity AS qty
FROM transactionline tl JOIN transaction t ON t.id = tl.transaction
WHERE t.type = 'PurchOrd' AND BUILTIN.DF(t.entity) LIKE '{vendor_pattern}'
AND tl.mainline = 'F' AND tl.taxline = 'F'
AND t.trandate >= {_d(d_from)} AND t.trandate <= {_d(d_to)}
ORDER BY t.id, tl.id"""
    return _q(env, sql)


# ---------------------------------------------------------------------------
# Retailer deductions
# ---------------------------------------------------------------------------

def retailer_income_lines(env, period_start: date, asof: date) -> list[dict]:
    """VendBill/VendCred lines posting to Income-type accounts in the accounting period
    starting on period_start, dated on or before asof. Keys: tid, ttype, tranid, d,
    cdate, vendor, acct, acctname, lmemo, hmemo, lineid, amt (ledger sign)."""
    sql = f"""SELECT t.id AS tid, t.type AS ttype, t.tranid AS tranid, t.trandate AS d,
TO_CHAR(t.createddate,'YYYY-MM-DD') AS cdate, BUILTIN.DF(t.entity) AS vendor, a.acctnumber AS acct,
a.fullname AS acctname, tl.memo AS lmemo, t.memo AS hmemo, tl.id AS lineid, SUM(ai.amount) AS amt
{_LINE_JOIN}
JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE ai.posting = 'T' AND a.accttype = 'Income' AND t.type IN ('VendBill','VendCred')
AND ap.startdate = {_d(period_start)} AND t.trandate <= {_d(asof)}
GROUP BY t.id, t.type, t.tranid, t.trandate, TO_CHAR(t.createddate,'YYYY-MM-DD'), BUILTIN.DF(t.entity),
a.acctnumber, a.fullname, tl.memo, t.memo, tl.id
ORDER BY t.id, tl.id"""
    return _q(env, sql)
