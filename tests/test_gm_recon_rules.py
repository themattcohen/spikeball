"""Pure-rule tests for the monthly gross-margin reconciliation (spike/gm_recon/rules.py).
Hermetic: no network, no credentials, no clock reads except through explicit `now`.

Run: python -m pytest tests/test_gm_recon_rules.py -q
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

GM_RECON = Path(__file__).resolve().parents[1] / "spike" / "gm_recon"
if str(GM_RECON) not in sys.path:
    sys.path.insert(0, str(GM_RECON))

import rules  # noqa: E402

MJ_CFG = {"accrual_words": ["accrual", "expected on", "payout"],
          "true_up_words": ["true up", "true-up", "trueup", "amazon"],
          "counterpart_prefixes_review": ["3", "6"]}


# ---------------------------------------------------------------------------
# Fee status and FX
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("feeall,rows,mapped,expected", [
    (100.0, 100.0, True, "full"),
    (100.5, 100.0, True, "full"),        # within 1 percent
    (98.0, 100.0, True, "partial"),
    (0.0, 100.0, True, "none"),
    (0.0, 100.0, False, "no_journal"),
    (50.0, 100.0, False, "no_journal"),
])
def test_fee_status(feeall, rows, mapped, expected):
    assert rules.fee_status(feeall, rows, mapped) == expected


def test_fx_fixed_rates_and_unknown_currency():
    rates = {"USD": 1.0, "CAD": 0.71575, "GBP": 1.35}
    assert rules.to_usd(100.0, "CAD", rates) == pytest.approx(71.575)
    assert rules.to_usd(40.0, "GBP", rates) == pytest.approx(54.0)
    with pytest.raises(KeyError):
        rules.fx_rate("EUR", rates)


def test_num_is_strict():
    assert rules.num(None) == 0.0
    assert rules.num("") == 0.0
    assert rules.num("12.5") == 12.5
    with pytest.raises(ValueError):
        rules.num("abc")


def test_summarize_journal_splits_fee_lines():
    memo = {"fee_line_prefix": "Fee charged by Amazon", "deposit_line": "Amount deposited by Amazon",
            "variance_line": "Variance amount"}
    lines = [
        {"acct": "10101750", "dr": "1000", "lmemo": "Amount deposited by Amazon"},
        {"acct": "50300000", "dr": "100", "lmemo": "Fee charged by Amazon"},
        {"acct": "40105000", "dr": "2", "lmemo": "Fee charged by Amazon for x"},
        {"acct": "10101700", "cr": "1102", "lmemo": "Variance amount"},
    ]
    j = rules.summarize_journal(lines, memo)
    assert j["feeall"] == 102 and j["fee5"] == 100 and j["fee_outside"] == 2
    assert j["bank"] == 1000 and j["var"] == -1102
    assert j["fee_accounts"] == {"50300000": 100.0}
    assert j["bank_accounts"] == ["10101750"]


# ---------------------------------------------------------------------------
# Settlement to journal mapping
# ---------------------------------------------------------------------------

def test_mapping_by_deposit_date_and_bank_tiebreak():
    setl = [{"id": "1", "dd": date(2026, 9, 5), "total_amt": 1000.0},
            {"id": "2", "dd": date(2026, 9, 5), "total_amt": 400.0},
            {"id": "3", "dd": date(2026, 9, 9), "total_amt": 10.0}]
    js = {"JE1": {"d": date(2026, 9, 5), "bank": 400.2, "feeall": 0.0},
          "JE2": {"d": date(2026, 9, 5), "bank": 1000.0, "feeall": 0.0}}
    m, rule = rules.map_settlements_to_journals(setl, js, tol=1.0)
    assert m == {"1": "JE2", "2": "JE1", "3": None}
    assert rule["3"] == "no journal found"


def test_mapping_second_pass_finds_redated_journal():
    setl = [{"id": "9", "dd": date(2026, 8, 30), "total_amt": 31618.22, "currency": "CAD", "rows_fee_local": 15763.94}]
    js = {"JE5696": {"d": date(2026, 9, 1), "bank": 22630.65, "feeall": 11283.0}}
    m, rule = rules.map_settlements_to_journals(setl, js, tol=1.0, fx={"CAD": 0.71575})
    assert m == {"9": "JE5696"}
    assert "2 day(s) after deposit" in rule["9"]


def test_mapping_second_pass_needs_a_unique_candidate():
    setl = [{"id": "9", "dd": date(2026, 8, 30), "total_amt": 100.0, "currency": "USD", "rows_fee_local": 10.0}]
    js = {"A": {"d": date(2026, 8, 31), "bank": 100.0, "feeall": 0.0},
          "B": {"d": date(2026, 9, 1), "bank": 100.0, "feeall": 0.0}}
    m, _ = rules.map_settlements_to_journals(setl, js, tol=1.0, fx={"USD": 1.0})
    assert m == {"9": None}


def test_choose_fee_cogs_account():
    assert rules.choose_fee_cogs_account([{"50300000": 90.0}, {"50100000": 10.0}], "50100000") == "50300000"
    assert rules.choose_fee_cogs_account([], "50100000") == "50100000"


# ---------------------------------------------------------------------------
# Sales month
# ---------------------------------------------------------------------------

def test_sales_month_precedence_and_fallback():
    legacy = {"A": date(2026, 8, 19)}
    cache = {"A": date(2026, 9, 2), "B": date(2026, 9, 2)}
    assert rules.sales_month("A", legacy, cache, date(2026, 9, 4)) == ("2026-08", "legacy_invoice")
    assert rules.sales_month("B", legacy, cache, date(2026, 10, 1)) == ("2026-09", "amazon_cache")
    assert rules.sales_month("C", legacy, cache, date(2026, 10, 1)) == ("2026-10", "posted_date")
    assert rules.sales_month("C", legacy, cache, None) == ("unknown", "none")


def test_purchase_date_converts_to_mountain_time():
    # 03:30 UTC on 10/1 is 9/30 in Denver (MDT, UTC-6)
    assert rules.utc_iso_to_mt_date("2026-10-01T03:30:00Z") == date(2026, 9, 30)
    assert rules.utc_iso_to_utc_date("2026-10-01T03:30:00Z") == date(2026, 10, 1)
    # winter: UTC-7
    assert rules.utc_iso_to_mt_date("2026-12-01T06:30:00Z") == date(2026, 11, 30)


# ---------------------------------------------------------------------------
# Retailer program period
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("memo,bill,created,expected", [
    ("Target MAP Q2 2026", date(2026, 9, 4), date(2026, 9, 7), [("2026-06", 1.0)]),
    ("MAP Q2-Q3", date(2026, 9, 4), date(2026, 9, 7), [("2026-06", 0.5), ("2026-09", 0.5)]),
    ("Walmart Claim 125019776", date(2026, 8, 1), date(2026, 8, 1), None),
    ("2025 Co-op", date(2026, 3, 2), date(2026, 3, 2), [("2025-12", 1.0)]),
    ("June 2026 circular", date(2026, 7, 10), date(2026, 7, 10), [("2026-06", 1.0)]),
    ("TFR043", date(2026, 8, 17), date(2026, 8, 19), None),
    ("Claim IRA2025Q4101935", date(2026, 1, 5), date(2025, 12, 30), [("2025-12", 1.0)]),
    ("2024 BDF deduction for October", date(2026, 2, 1), date(2026, 2, 1), [("2024-10", 1.0)]),
])
def test_infer_program_period(memo, bill, created, expected):
    assert rules.infer_program_period(memo, bill, created) == expected


def test_infer_program_period_early_created_bill_goes_to_creation_month():
    # Walmart claim created 6/10, dated 8/1, no period in the memo
    assert rules.infer_program_period("Sales Discounts - Claim 125019776", date(2026, 8, 1),
                                      date(2026, 6, 10)) == [("2026-06", 1.0)]


def test_infer_program_period_same_year_bare_year_is_not_inferable():
    assert rules.infer_program_period("2026 Co-op", date(2026, 7, 1), date(2026, 7, 1)) is None


def test_split_amount_half_up_first_share():
    parts = rules.split_amount(617.45, [("2026-06", 0.5), ("2026-09", 0.5)])
    assert parts == [("2026-06", 308.73), ("2026-09", 308.72)]
    assert sum(p for _, p in parts) == pytest.approx(617.45)


# ---------------------------------------------------------------------------
# Manual journal classification
# ---------------------------------------------------------------------------

def _j(memo, lines):
    return {"hmemo": memo, "lines": lines}


def test_classify_accrual_with_reversal_link():
    j = _j("May COGS expected on 6/1 Amazon payout",
           [{"acct": "50100000", "atype": "COGS", "dr": "100"}, {"acct": "20106400", "atype": "OthCurrLiab", "cr": "100"}])
    assert rules.classify_manual_journal(j, True, False, MJ_CFG) == rules.ACCRUAL_WITH_REVERSAL


def test_classify_accrual_by_memo_and_opposite_journal():
    j = _j("Fee accrual", [{"acct": "50100000", "atype": "COGS", "dr": "100"},
                           {"acct": "20106400", "atype": "OthCurrLiab", "cr": "100"}])
    assert rules.classify_manual_journal(j, False, True, MJ_CFG) == rules.ACCRUAL_WITH_REVERSAL
    # without the opposite journal the accrual words alone are not enough
    assert rules.classify_manual_journal(j, False, False, MJ_CFG) == rules.OTHER


def test_classify_true_up_without_reversal():
    j = _j("Amazon true up August", [{"acct": "50100000", "atype": "COGS", "dr": "10"},
                                     {"acct": "10101700", "atype": "Bank", "cr": "10"}])
    assert rules.classify_manual_journal(j, False, False, MJ_CFG) == rules.TRUE_UP_NO_REVERSAL


def test_classify_equity_or_sga_counterpart():
    j = _j("To move estimate corporate taxes", [{"acct": "30406000", "atype": "Equity", "dr": "38000"},
                                                {"acct": "50100000", "atype": "COGS", "cr": "38000"}])
    assert rules.classify_manual_journal(j, False, False, MJ_CFG) == rules.EQUITY_OR_SGA
    j2 = _j("Total amount of other transactions", [{"acct": "60101100", "atype": "Expense", "cr": "500"},
                                                   {"acct": "50100000", "atype": "COGS", "dr": "500"}])
    assert rules.classify_manual_journal(j2, False, False, MJ_CFG) == rules.EQUITY_OR_SGA


def test_classify_netzero_reclass_and_other():
    j = _j("reclass discount", [{"acct": "40201600", "atype": "Income", "dr": "20"},
                                {"acct": "40100000", "atype": "Income", "cr": "20"}])
    assert rules.classify_manual_journal(j, False, False, MJ_CFG) == rules.NETZERO_RECLASS
    j2 = _j("Stripe fees", [{"acct": "50300000", "atype": "COGS", "dr": "20"},
                            {"acct": "10101000", "atype": "Bank", "cr": "20"}])
    assert rules.classify_manual_journal(j2, False, False, MJ_CFG) == rules.OTHER


# ---------------------------------------------------------------------------
# Accrual math and formatting
# ---------------------------------------------------------------------------

def test_accrual_math():
    assert rules.median_fee([10.0, 30.0, 20.0, 40.0]) == 25.0
    assert rules.median_fee([]) == 0.0
    assert rules.unsettled_estimate(3, 20.38) == 61.14
    assert rules.accrual_amount(43374.39, 28011.77, 0.0) == 71386.16
    assert rules.accrual_amount(100.0, 50.0, 120.0) == 30.0


def test_gm_pct_and_whole_dollars():
    assert rules.gm_pct(100.0, 45.0) == pytest.approx(0.55)
    assert rules.gm_pct(0.0, 5.0) is None
    assert rules.whole_dollars(71386.16) == "71,386"
    assert rules.whole_dollars(-1234.5) == "-1,234"


# ---------------------------------------------------------------------------
# Months, dates, DST
# ---------------------------------------------------------------------------

def test_default_month_is_previous_month_in_mountain_time():
    # 10/1 05:00 UTC is still 9/30 23:00 in Denver: the previous month is August
    assert rules.default_month(datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)) == "2026-08"
    # 10/1 07:00 UTC is 10/1 01:00 MDT: the previous month is September
    assert rules.default_month(datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)) == "2026-09"
    # January rolls back to December of the prior year
    assert rules.default_month(datetime(2027, 1, 15, 18, 0, tzinfo=timezone.utc)) == "2026-12"


def test_default_asof_across_dst_changes():
    # 2026-03-08 is the spring DST change: 08:30 UTC is 01:30 MST, 09:30 UTC is 03:30 MDT
    assert rules.default_asof(datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc)) == date(2026, 3, 8)
    assert rules.default_asof(datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)) == date(2026, 3, 7)
    # 2026-11-01 is the fall change: 06:30 UTC is 00:30 MDT, 08:30 UTC is 01:30 MST
    assert rules.default_asof(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)) == date(2026, 11, 1)
    assert rules.default_asof(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)) == date(2026, 10, 31)


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        rules.now_mt(datetime(2026, 10, 1, 12, 0))


def test_month_arithmetic():
    assert rules.add_months("2026-01", -1) == "2025-12"
    assert rules.add_months("2026-12", 1) == "2027-01"
    assert rules.month_bounds("2028-02") == (date(2028, 2, 1), date(2028, 2, 29))
    assert rules.month_bounds("2026-09") == (date(2026, 9, 1), date(2026, 9, 30))
    assert rules.month_label("2026-09") == "September 2026"
    with pytest.raises(ValueError):
        rules.parse_month("2026-13")
    with pytest.raises(ValueError):
        rules.parse_month("Sept")


def test_parse_ns_date_formats():
    assert rules.parse_ns_date("9/3/2026") == date(2026, 9, 3)
    assert rules.parse_ns_date("2026-10-01") == date(2026, 10, 1)
    assert rules.parse_ns_date("") is None
    assert rules.parse_ns_date(None) is None
