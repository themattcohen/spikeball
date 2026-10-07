"""compute.build() on a small hand-built set of raw rows with a known answer, plus the
read-only guard on every NetSuite query, the run_recon verdict contract, and the
delivery helpers' tab guard. Hermetic: `suiteql` is monkeypatched in ns_queries and
`authed_request` in google_auth; no network, no credentials.

Run: python -m pytest tests/test_gm_recon_compute.py -q
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GM_RECON = ROOT / "spike" / "gm_recon"
if str(GM_RECON) not in sys.path:
    sys.path.insert(0, str(GM_RECON))

import compute  # noqa: E402
import ns_queries  # noqa: E402

CFG = json.loads((ROOT / "spike" / "config" / "gm_recon.json").read_text(encoding="utf-8"))
MONTH = "2026-09"
ASOF = date(2026, 10, 7)


def _jl(tid, tranid, d, lineid, acct, atype, dr=None, cr=None, lmemo=""):
    r = {"tid": tid, "tranid": tranid, "d": d, "lineid": str(lineid), "acct": acct, "acctname": acct, "atype": atype,
         "lmemo": lmemo}
    if dr is not None:
        r["dr"] = str(dr)
    if cr is not None:
        r["cr"] = str(cr)
    return r


def _mj(tid, tranid, d, lineid, acct, atype, dr=None, cr=None, hmemo="", rev_id=None, isrev="F", cb="Controller"):
    r = _jl(tid, tranid, d, lineid, acct, atype, dr, cr, hmemo)
    r.update({"cb": cb, "hmemo": hmemo, "isrev": isrev, "cdate": d})
    if rev_id:
        r["rev_id"] = rev_id
    return r


FEE = "Fee charged by Amazon"
DEP = "Amount deposited by Amazon"
VAR = "Variance amount"


def make_raw():
    return {
        "pnl": [
            {"acct": "40100000", "name": "Revenue : Sales", "typ": "Income", "amt": "-100000"},
            {"acct": "50100000", "name": "Cost of Goods Sold", "typ": "COGS", "amt": "40000"},
            {"acct": "50300000", "name": "Merchant Account Fees", "typ": "COGS", "amt": "5000"},
        ],
        "summaries": [
            {"id": "1", "acco": "1", "cur": "USD", "dd": "9/5/2026", "total_amt": "1000", "settl_id": "S1"},
            {"id": "2", "acco": "3", "cur": "GBP", "dd": "9/18/2026", "total_amt": "500", "settl_id": "S2"},
            {"id": "3", "acco": "1", "cur": "USD", "dd": "10/3/2026", "total_amt": "800", "settl_id": "S3"},
            {"id": "4", "acco": "1", "cur": "USD", "dd": "8/22/2026", "total_amt": "300", "settl_id": "S4"},
        ],
        "settlement_journal_lines": [
            _jl("10", "JE0", "8/22/2026", 0, "10101750", "Bank", dr=300, lmemo=DEP),
            _jl("10", "JE0", "8/22/2026", 1, "50100000", "COGS", dr=30, lmemo=FEE),
            _jl("10", "JE0", "8/22/2026", 2, "10101700", "Bank", cr=330, lmemo=VAR),
            _jl("11", "JE1", "9/5/2026", 0, "10101750", "Bank", dr=1000, lmemo=DEP),
            _jl("11", "JE1", "9/5/2026", 1, "50300000", "COGS", dr=100, lmemo=FEE),
            _jl("11", "JE1", "9/5/2026", 2, "40105000", "Income", dr=2, lmemo=FEE),
            _jl("11", "JE1", "9/5/2026", 3, "60302200", "Expense", dr=3, lmemo=FEE),
            _jl("11", "JE1", "9/5/2026", 4, "10101700", "Bank", cr=1105, lmemo=VAR),
            _jl("12", "JE2", "9/18/2026", 0, "10101750", "Bank", dr=675, lmemo=DEP),
            _jl("12", "JE2", "9/18/2026", 1, "10101700", "Bank", cr=675, lmemo=VAR),
            _jl("13", "JE3", "10/3/2026", 0, "10101750", "Bank", dr=800, lmemo=DEP),
            _jl("13", "JE3", "10/3/2026", 1, "50300000", "COGS", dr=50, lmemo=FEE),
            _jl("13", "JE3", "10/3/2026", 2, "10101700", "Bank", cr=850, lmemo=VAR),
        ],
        "settlement_rows": [
            {"id": "101", "o": "111-0000001-0000001", "ty": "1", "pd": "9/4/2026", "sm": "1", "fee": "-60", "pc": "100"},
            {"id": "102", "o": "111-0000002-0000002", "ty": "1", "pd": "9/4/2026", "sm": "1", "fee": "-45", "pc": "70"},
            {"id": "103", "o": "111-0000002-0000002", "ty": "2", "pd": "8/30/2026", "sm": "1", "fee": "0", "pc": "-20"},
            {"id": "104", "o": "", "ty": "3", "pd": "9/4/2026", "sm": "1", "fee": "-999", "pc": "0"},
            {"id": "201", "o": "203-0000003-0000003", "ty": "1", "pd": "9/16/2026", "sm": "2", "fee": "-40", "pc": "50"},
            {"id": "301", "o": "111-0000004-0000004", "ty": "1", "pd": "10/1/2026", "sm": "3", "fee": "-50", "pc": "80"},
            {"id": "401", "o": "111-0000005-0000005", "ty": "1", "pd": "8/20/2026", "sm": "4", "fee": "-30", "pc": "60"},
        ],
        "legacy_invoices": [{"tid": "900", "oid": "111-0000001-0000001", "d": "8/19/2026"}],
        "manual_journal_lines": [
            _mj("20", "JEA", "8/31/2026", 0, "50100000", "COGS", dr=25, hmemo="Amazon fees expected on 9/5 payout", rev_id="21"),
            _mj("20", "JEA", "8/31/2026", 1, "20106400", "OthCurrLiab", cr=25, hmemo="Amazon fees expected on 9/5 payout", rev_id="21"),
            _mj("21", "JEB", "9/1/2026", 0, "50100000", "COGS", cr=25, hmemo="Amazon fees expected on 9/5 payout", rev_id="20", isrev="T"),
            _mj("21", "JEB", "9/1/2026", 1, "20106400", "OthCurrLiab", dr=25, hmemo="Amazon fees expected on 9/5 payout", rev_id="20", isrev="T"),
            _mj("22", "JET", "9/8/2026", 0, "50100000", "COGS", dr=10, hmemo="Amazon true up"),
            _mj("22", "JET", "9/8/2026", 1, "10101700", "Bank", cr=10, hmemo="Amazon true up"),
            _mj("23", "JEE", "9/9/2026", 0, "30406000", "Equity", dr=500, hmemo="Move estimated taxes"),
            _mj("23", "JEE", "9/9/2026", 1, "50100000", "COGS", cr=500, hmemo="Move estimated taxes"),
            _mj("24", "JEN", "9/10/2026", 0, "40201600", "Income", dr=20, hmemo="Reclass discount"),
            _mj("24", "JEN", "9/10/2026", 1, "40100000", "Income", cr=20, hmemo="Reclass discount"),
        ],
        "journal_partners": [],
        "late_created": [
            {"tid": "50", "tranid": "INV50", "ttype": "CustInvc", "d": "9/30/2026", "cdate": "2026-10-02", "cb": "x",
             "atype": "Income", "amt": "-300"},
            {"tid": "50", "tranid": "INV50", "ttype": "CustInvc", "d": "9/30/2026", "cdate": "2026-10-02", "cb": "x",
             "atype": "COGS", "amt": "100"},
        ],
        "credit_memos": [
            {"tid": "60", "tranid": "CM1", "oid": "111-0000002-0000002", "d": "9/10/2026", "inc": "20", "cogs": "-5"},
            {"tid": "61", "tranid": "CM2", "ext": "MC-CM-1", "d": "9/12/2026", "inc": "30", "cogs": "0"},
        ],
        "dup_invoices": [],
        "dup_credit_memos": [{"oid": "111-0000009-0000009", "n": "2"}],
        "dup_invoice_docs": [],
        "dup_cm_docs": [
            {"tid": "65", "tranid": "CM5", "oid": "111-0000009-0000009", "d": "9/11/2026", "inc": "15", "cogs": "-3"},
            {"tid": "66", "tranid": "CM6", "oid": "111-0000009-0000009", "d": "9/12/2026", "inc": "15", "cogs": "-3"},
        ],
        "consolidated_invoices": [
            {"tid": "70", "tranid": "INV70", "orn": "DAILY-FBA-US-2026-09-03", "d": "9/3/2026", "cur": "1",
             "rev40100": "200", "inc": "-200", "cogs": "80"},
            {"tid": "71", "tranid": "INV71", "orn": "DAILY-FBA-US-2026-09-03", "d": "9/3/2026", "cur": "1",
             "rev40100": "200", "inc": "-200", "cogs": "80"},
        ],
        "legacy_invoice_revenue": [],
        "wengo_bills": [
            {"tid": "80", "tranid": "WG-1", "ttype": "VendBill", "d": "9/11/2026", "cdate": "2026-09-11", "status": "Open",
             "memo": "PO100", "tot": "-1000", "cogs": "1000", "pmonth": "2026-09"},
            {"tid": "81", "tranid": "WG-2", "ttype": "VendBill", "d": "9/12/2026", "cdate": "2026-09-12", "status": "Open",
             "memo": "sourcing fee", "tot": "-300", "cogs": "300", "pmonth": "2026-09"},
        ],
        "wengo_po_lines": [{"tid": "90", "po": "PO100", "d": "8/20/2026", "item": "S-TTN-001", "qty": "50"}],
        "month_po_lines": [
            {"tid": "91", "po": "PO200", "d": "9/15/2026", "item": "S-TTN-001", "qty": "10"},
            {"tid": "91", "po": "PO200", "d": "9/15/2026", "item": "S-PONG-001", "qty": "100"},
            {"tid": "91", "po": "PO200", "d": "9/15/2026", "item": "A-BALL-001", "qty": "500"},
        ],
        "retailer_lines": [
            {"tid": "95", "ttype": "VendBill", "tranid": "IRA1", "d": "9/4/2026", "cdate": "2026-09-07", "vendor": "Retailer A",
             "acct": "40201600", "lmemo": "MAP Q2-Q3 2026", "hmemo": "MAP Q2-Q3 2026", "lineid": "1", "amt": "100"},
            {"tid": "96", "ttype": "VendBill", "tranid": "C-7", "d": "9/17/2026", "cdate": "2026-09-19", "vendor": "Retailer B",
             "acct": "40201600", "lmemo": "TFR043", "hmemo": "", "lineid": "1", "amt": "40"},
        ],
    }


def _o(day, mk, status="Shipped"):
    return {"purchase_mt_date": day, "purchase_utc_date": day, "marketplace": mk, "status": status,
            "order_total": 10.0, "currency": None, "item_total": None, "n_items": 0}


def make_cache():
    return {
        "111-0000002-0000002": _o(date(2026, 9, 2), "US"),
        "203-0000003-0000003": _o(date(2026, 9, 10), "UK"),
        "111-0000004-0000004": _o(date(2026, 9, 28), "US"),
        "111-0000005-0000005": _o(date(2026, 8, 18), "US"),
        "111-0000006-0000006": _o(date(2026, 9, 15), "US"),
        "111-0000007-0000007": _o(date(2026, 9, 16), "US", "Canceled"),
        "203-0000008-0000008": _o(date(2026, 9, 20), "UK"),
    }


CACHE_INFO = {"cutoff_utc": "2026-10-06T13:49:57Z", "orders": 7, "items_present": False, "orders_with_items": 0}


@pytest.fixture
def built():
    return compute.build(MONTH, ASOF, make_raw(), CFG, make_cache(), CACHE_INFO, {"run_at_mt": "t", "code_rev": "abc"})


def _by_id(rows):
    return {r["id"]: r for r in rows}


def test_pnl_as_booked(built):
    model, _ = built
    assert model["pnl"]["income"] == 100000.0
    assert model["pnl"]["cogs"] == 45000.0
    assert model["margin"]["as_booked"] == pytest.approx(0.55)


def test_e1_and_e4_errors(built):
    model, details = built
    errs = _by_id(model["errors"])
    assert errs["E1-1"]["amount"] == 54.0                 # GBP 40 x 1.35, journal with no fee lines
    assert errs["E1-1"]["cogs_effect"] == 54.0 and errs["E1-1"]["income_effect"] == 0.0
    assert "JE2" in errs["E1-1"]["records"]
    assert "50300000" in errs["E1-1"]["entry"] and "10101700" in errs["E1-1"]["entry"]
    assert errs["E4-1"]["amount"] == 3.0                  # fee line on expense account 60302200
    assert errs["E4-1"]["income_effect"] == 0.0 and errs["E4-1"]["cogs_effect"] == 3.0
    assert "40105000" not in errs["E4-1"]["what"] and "E4-2" not in errs
    # 40105000 shipping chargebacks are accepted by design: a data note, not an error
    assert any(l.startswith("Amazon fee lines on 40105000") and "not reclassed" in l for l in model["limitations"])
    treat = {r["account"]: (r["treatment"], r["error_id"]) for r in details["fee_lines_outside"]}
    assert treat == {"40105000": ("accepted, not reclassed", ""), "60302200": ("reclass to COGS", "E4-1")}
    status = {s["sid"]: s["status"] for s in details["settlements"]}
    assert status == {"4": "full", "1": "full", "2": "none", "3": "full"}
    assert model["fee_cogs_account"] == "50300000"


def test_duplicates(built):
    model, _ = built
    errs = _by_id(model["errors"])
    assert errs["D1-1"]["income_effect"] == 15.0 and errs["D1-1"]["cogs_effect"] == 3.0   # same-amount credit memos
    assert errs["D1-2"]["income_effect"] == -200.0 and errs["D1-2"]["cogs_effect"] == -80.0
    assert errs["D1-2"]["amount"] == 200.0


def test_t1_fee_timing_and_accrual(built):
    model, details = built
    t1 = _by_id(model["timing"])["T1"]
    assert model["t1"]["booked_rows"] == 159.0            # 60 + 45 + 54 settled in September
    assert model["t1"]["accrual_journals"] == -25.0       # the August accrual's reversal dated 9/1
    assert model["t1"]["belongs_rows"] == 149.0           # 45 + 54 + 50
    assert model["t1"]["after"] == 50.0
    assert model["t1"]["earlier"] == 60.0
    assert model["t1"]["estimate"] == 101.5               # US 1 x 47.5 median, UK 1 x 54
    assert model["t1"]["estimate_orders"] == 2
    assert model["t1"]["accepted_non_cogs"] == 2.0       # 40105000 line stays on income, not booked fee COGS
    assert t1["booked_in_month"] == 132.0 and t1["belongs_to_month"] == 250.5 and t1["net"] == 118.5
    assert model["t1"]["accrual"] == 151.5
    assert "debit 50300000 152, credit 20106400 152" in t1["entry"]
    assert any("fee accrual and its reversal" in f for f in model["fine"])
    est = {r["marketplace"]: r for r in details["fee_estimate"]}
    assert est["US"]["orders_unsettled"] == 1 and est["US"]["median_fee"] == 47.5
    assert est["CA"]["estimate"] == 0.0


def test_manual_journal_review(built):
    model, details = built
    rev = _by_id(model["review"])
    assert rev["R1-1"]["amount"] == 10.0 and "JET" in rev["R1-1"]["records"]
    assert rev["R1-2"]["amount"] == -500.0 and "JEE" in rev["R1-2"]["records"]
    classes = {r["journal"]: r["class"] for r in details["manual_journals"]}
    assert classes == {"JEB": "accrual_with_reversal", "JET": "true_up_no_reversal",
                       "JEE": "equity_or_sga_counterpart", "JEN": "netzero_reclass"}


def test_wengo_and_retailer_timing(built):
    model, details = built
    tm = _by_id(model["timing"])
    assert tm["T3-1"]["booked_in_month"] == 1000.0 and tm["T3-1"]["net"] == -1000.0   # PO100 is an August PO
    assert tm["T3-2"]["belongs_to_month"] == 476.0       # 10 Titan x 20 observed + 100 Pong x 2.76 fallback
    assert tm["T3-2"]["basis"] == "estimate"
    e = tm["T3-2"]["entry"]
    assert "at 20 per Titan unit, the rate observed on WG-1; config fallback is 14.37" in e
    assert "at 2.76 per Pong unit, the config fallback" in e
    assert len(e) <= 240
    assert tm["T4-1"]["income_effect"] == 50.0 and tm["T4-1"]["belongs_to_month"] == 50.0
    rev = _by_id(model["review"])
    assert rev["R-W1"]["amount"] == 300.0
    assert rev["R-T4-1"]["amount"] == 40.0
    assert rev["R-T2"]["amount"] == -20.0                 # legacy CM whose refund posted in August


def test_margin_and_entries(built):
    model, details = built
    m = model["margin"]
    assert m["income"] == {"as_booked": 100000.0, "corrected": 99815.0, "matched": 99865.0}
    assert m["cogs"] == {"as_booked": 45000.0, "corrected": 44980.0, "matched": 44574.5}
    assert m["matched_basis"] == "measured+estimate"
    dr = sum(e["debit"] for e in details["entries"])
    cr = sum(e["credit"] for e in details["entries"])
    assert dr == pytest.approx(cr)
    srcs = {e["source"] for e in details["entries"]}
    assert srcs == {"E1-1", "E4-1", "T1", "T3-2"}
    assert len(model["answer"]) <= 8
    assert model["detail_counts"]["late_created"] == 1


def test_build_without_cache_says_so():
    model, details = compute.build(MONTH, ASOF, make_raw(), CFG, None, None, {})
    assert model["t1"]["estimate"] == 0.0
    assert model["margin"]["matched_basis"] == "measured, excludes fees not yet settled"
    assert any("completeness was not measured" in l for l in model["limitations"])
    assert details["fee_estimate"] == []


def test_email_tables_whole_dollars(built):
    model, _ = built
    text = compute.email_tables(model)
    assert "E1-1" in text and "T1" in text
    assert ".00" not in text


# ---------------------------------------------------------------------------
# Read-only guard and query scoping
# ---------------------------------------------------------------------------

def test_every_query_is_a_select_and_date_scoped(monkeypatch):
    seen = []

    def fake(env, sql, **kw):
        seen.append(" ".join(sql.split()))
        if "customrecord_celigo_amzio_sett_summary" in sql:
            return [{"id": "1", "acco": "1", "cur": "USD", "dd": "9/5/2026", "total_amt": "1"}]
        if "customrecord_celigo_amzio_settle_trans" in sql:
            return [{"id": "2", "o": "111-0000001-0000001", "ty": "1", "pd": "9/4/2026", "sm": "1", "fee": "-1"}]
        if "BUILTIN.DF(t.entity) LIKE '%Wengo%'" in sql and "SUM(ai.amount)" not in sql:
            return [{"tid": "5", "tranid": "WG-1", "d": "9/11/2026", "memo": "PO1", "tot": "-1"}]
        return []

    monkeypatch.setattr(ns_queries, "suiteql", fake)
    raw = compute.fetch_raw({}, MONTH, ASOF, CFG, log=lambda *_: None)
    assert seen, "no queries issued"
    for q in seen:
        assert q.lower().startswith("select")
        assert "TO_DATE(" in q or "custrecord_celigo_amzio_set_summary IN" in q
    assert set(raw) >= {"pnl", "summaries", "settlement_rows", "manual_journal_lines", "retailer_lines"}


def test_non_select_is_refused(monkeypatch):
    monkeypatch.setattr(ns_queries, "suiteql", lambda env, sql, **kw: [])
    with pytest.raises(ns_queries.ReadOnlyViolation):
        ns_queries._q({}, "UPDATE transaction SET memo = 'x'")


def test_order_ids_are_validated_before_inlining(monkeypatch):
    seen = []
    monkeypatch.setattr(ns_queries, "suiteql", lambda env, sql, **kw: seen.append(sql) or [])
    ns_queries.legacy_invoice_dates({}, ["111-0000001-0000001", "x' OR 1=1 --"], date(2026, 1, 1), ASOF)
    assert len(seen) == 1 and "OR 1=1" not in seen[0]


# ---------------------------------------------------------------------------
# run_recon verdict contract and delivery guards
# ---------------------------------------------------------------------------

def test_run_recon_env_not_loaded_is_exit_4(monkeypatch, capsys):
    import run_recon
    monkeypatch.setattr(run_recon.doppler_env, "ensure_loaded", lambda: False)
    assert run_recon.main(["--dry-run"]) == 4
    assert capsys.readouterr().out.strip().splitlines()[-1] == "RECON_FAIL env not loaded"


def test_run_recon_netsuite_failure_is_exit_4(monkeypatch, capsys, tmp_path):
    import run_recon
    monkeypatch.setattr(run_recon.doppler_env, "ensure_loaded", lambda: True)
    for k in ("NETSUITE_ACCOUNT_ID", "NETSUITE_CONSUMER_KEY", "NETSUITE_CONSUMER_SECRET", "NETSUITE_TOKEN_ID",
              "NETSUITE_TOKEN_SECRET"):
        monkeypatch.setenv(k, "test")

    def boom(*a, **k):
        raise RuntimeError("offline in test")

    monkeypatch.setattr(compute, "fetch_raw", boom)
    code = run_recon.main(["--month", "2026-09", "--asof", "2026-10-07", "--dry-run", "--out", str(tmp_path)])
    assert code == 4
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("RECON_FAIL NetSuite read failed")


def test_run_recon_dry_run_end_to_end_offline(monkeypatch, capsys, tmp_path):
    pytest.importorskip("openpyxl")
    import run_recon
    monkeypatch.setattr(run_recon.doppler_env, "ensure_loaded", lambda: True)
    for k in ("NETSUITE_ACCOUNT_ID", "NETSUITE_CONSUMER_KEY", "NETSUITE_CONSUMER_SECRET", "NETSUITE_TOKEN_ID",
              "NETSUITE_TOKEN_SECRET"):
        monkeypatch.setenv(k, "test")
    monkeypatch.setattr(compute, "fetch_raw", lambda *a, **k: make_raw())
    code = run_recon.main(["--month", "2026-09", "--asof", "2026-10-07", "--dry-run", "--skip-amazon-cache",
                           "--out", str(tmp_path)])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == 0 and last.startswith("RECON_OK ")
    assert (tmp_path / "Spikeball_GM_Recon_2026-09_asof_20261007.xlsx").is_file()
    summary = json.loads((tmp_path / "summary_2026-09_asof_20261007.json").read_text(encoding="utf-8"))
    assert summary["month"] == "2026-09" and summary["delivery"]["email"] == "skipped"
    assert summary["limitations"][0].startswith("Orders cache skipped")


def test_log_row_refuses_nightly_tabs():
    import deliver
    with pytest.raises(ValueError):
        deliver.append_log_row("sheet", "run_log", ["a"], {"a": 1})


def test_recipients_parsing(monkeypatch):
    import deliver
    monkeypatch.setenv("SPIKEBALL_RECON_TO", "a@example.com, b@example.com,")
    assert deliver.recipients(CFG) == ["a@example.com", "b@example.com"]
    monkeypatch.delenv("SPIKEBALL_RECON_TO")
    monkeypatch.setenv("SPIKEBALL_ALERT_TO", "c@example.com")
    assert deliver.recipients(CFG) == ["c@example.com"]


def test_upload_patches_existing_file(monkeypatch, tmp_path):
    import deliver
    import google_auth
    calls = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET":
            return R(200, {"files": [{"id": "F1"}]})
        return R(200, {"id": "F1", "webViewLink": "https://drive.example/F1"})

    monkeypatch.setattr(google_auth, "authed_request", fake)
    p = tmp_path / "x.xlsx"
    p.write_bytes(b"data")
    fid, link = deliver.upload_xlsx(p, "FOLDER")
    assert (fid, link) == ("F1", "https://drive.example/F1")
    assert calls[1][0] == "PATCH" and "/upload/drive/v3/files/F1" in calls[1][1]
    assert "name='x'" in calls[0][2]["params"]["q"]
    assert "application/vnd.google-apps.spreadsheet" in calls[0][2]["params"]["q"]
    assert calls[1][2]["headers"]["Content-Type"] == deliver.XLSX_MIME
    assert p.read_bytes() == b"data"


def test_upload_creates_native_google_sheet(monkeypatch, tmp_path):
    import deliver
    import google_auth
    calls = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET":
            return R(200, {"files": []})
        return R(200, {"id": "G1", "webViewLink": "https://docs.example/G1"})

    monkeypatch.setattr(google_auth, "authed_request", fake)
    p = tmp_path / "Spikeball_GM_Recon_2026-09_asof_20261007.xlsx"
    p.write_bytes(b"xlsxbytes")
    fid, link = deliver.upload_xlsx(p, "FOLDER")
    assert (fid, link) == ("G1", "https://docs.example/G1")
    method, url, kw = calls[1]
    assert method == "POST" and kw["params"]["uploadType"] == "multipart"
    body = kw["data"]
    meta = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--b0undary", 1)[0])
    assert meta == {"name": "Spikeball_GM_Recon_2026-09_asof_20261007",
                    "mimeType": "application/vnd.google-apps.spreadsheet", "parents": ["FOLDER"]}
    assert b"Content-Type: " + deliver.XLSX_MIME.encode() in body and body.count(b"xlsxbytes") == 1
    assert p.read_bytes() == b"xlsxbytes"


# ---------------------------------------------------------------------------
# Orders cache reader (read-only; scratch folder removed after parsing)
# ---------------------------------------------------------------------------

def test_amazon_cache_load_parses_and_cleans_up(monkeypatch, tmp_path):
    import amazon_cache
    scratch = tmp_path / "_amazon_state"

    def fake_download(d):
        a = d / "amazon"
        a.mkdir(parents=True)
        (a / "orders_NA.jsonl").write_text(
            json.dumps({"AmazonOrderId": "111-0000001-0000001", "PurchaseDate": "2026-10-01T03:30:00Z",
                        "OrderStatus": "Shipped", "MarketplaceId": "ATVPDKIKX0DER",
                        "OrderTotal": {"CurrencyCode": "USD", "Amount": "69.40"}}) + "\n"
            + json.dumps({"AmazonOrderId": "111-0000002-0000002", "PurchaseDate": "2026-10-09T15:00:00Z",
                          "OrderStatus": "Shipped", "MarketplaceId": "ATVPDKIKX0DER", "OrderTotal": None}) + "\n",
            encoding="utf-8")
        (a / "order_items_EU.jsonl").write_text(
            json.dumps({"OrderItemId": "9", "AmazonOrderId": "203-0000003-0000003",
                        "ItemPrice": {"CurrencyCode": "GBP", "Amount": "24.00"}}) + "\n", encoding="utf-8")
        (a / "orders_EU.jsonl").write_text(
            json.dumps({"AmazonOrderId": "203-0000003-0000003", "PurchaseDate": "2026-09-20T10:00:00Z",
                        "OrderStatus": "Shipped", "MarketplaceId": "A1F83G8C2ARO7P",
                        "OrderTotal": {"CurrencyCode": "GBP", "Amount": "24.00"}}) + "\n", encoding="utf-8")
        return a

    monkeypatch.setattr(amazon_cache, "download_state", fake_download)
    orders, info = amazon_cache.load(ASOF, scratch, CFG["marketplace_ids"], 0.20)
    assert not scratch.exists()
    assert set(orders) == {"111-0000001-0000001", "203-0000003-0000003"}   # 10/9 purchase is after the as-of date
    o = orders["111-0000001-0000001"]
    assert o["purchase_mt_date"] == date(2026, 9, 30) and o["marketplace"] == "US" and o["order_total"] == 69.40
    assert orders["203-0000003-0000003"]["item_total"] == pytest.approx(20.0)   # UK item price net of VAT
    assert info["items_present"] is True and info["dropped_after_asof"] == 1
    assert info["cutoff_utc"] == "2026-10-09T15:00:00Z"


def test_amazon_cache_unavailable_raises_and_cleans_up(monkeypatch, tmp_path):
    import amazon_cache
    scratch = tmp_path / "_amazon_state"

    def fail(d):
        d.mkdir(parents=True)
        raise amazon_cache.CacheUnavailable("state file not available on Drive")

    monkeypatch.setattr(amazon_cache, "download_state", fail)
    with pytest.raises(amazon_cache.CacheUnavailable):
        amazon_cache.load(ASOF, scratch, CFG["marketplace_ids"], 0.20)
    assert not scratch.exists()


def test_run_recon_imports_every_module_before_the_netsuite_read():
    """A lazy import after the long NetSuite read could load a file edited mid-run and
    mix two code versions in one workbook; all recon modules load before fetch_raw."""
    src = (GM_RECON / "run_recon.py").read_text(encoding="utf-8")
    body = src[src.index("def run(args)"):]
    fetch = body.index("compute.fetch_raw(")
    for mod in ("amazon_cache", "compute", "deliver", "workbook"):
        assert body.index(f"import {mod}") < fetch, mod
        assert body.count(f"import {mod}") == 1, mod
