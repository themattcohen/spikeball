"""Month-range selector and refresh-control DOM harness (PRD-month-refresh.md
Section 7 T1, T1b-T1f, T2, T3, T4, T5, T6, T6b, T7, T8, T8b-T8d, T12).

Builds real design/mockup/template.html output (via design/mockup/build.py) with
spike/data/latest.json and drives it with a real headless Chromium through
Patchright, exactly as a browser would. Every "exact" comparison reads a
`data-raw` attribute (Section 4 test-hook interface) and compares floats per the
Section 7 preamble: within 0.005 for currency, within 0.01 for percent. T5's
comparison against the pre-change baseline's rendered (rounded) tile/table text
uses the wider tolerance the PRD specifies for that one comparison: within 0.5
for currency, within 0.05 for percent.

This file owns tests/test_dashboard_range.py and design/mockup/shots/month-refresh-t12-*.png
only. It does not modify design/mockup/template.html, build.py, spike/, scripts/,
tests/test_refresh_gate.py, or tests/conftest.py.

Run: python -m pytest tests/test_dashboard_range.py -q
"""
import collections
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

# Browser DOM tests need patchright (requirements-dev.txt) plus `python3 -m patchright
# install chromium`; without them this module skips cleanly instead of breaking collection.
sync_playwright = pytest.importorskip(
    "patchright.sync_api", reason="patchright not installed (see requirements-dev.txt)"
).sync_playwright

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
MOCKUP_DIR = ROOT / "design" / "mockup"
BUILD_PY = MOCKUP_DIR / "build.py"
DATA_PATH = ROOT / "spike" / "data" / "latest.json"
BASELINE_TEMPLATE = ROOT / "tests" / "fixtures" / "template_baseline_2026-09-08.html"
SHOTS_DIR = MOCKUP_DIR / "shots"

FIXTURE_REFRESH_URL = "https://script.google.com/macros/s/TEST/exec"

# Section 7 preamble: "'exact' means within 0.005 for currency and 0.01 for percent."
CURRENCY_TOL = 0.005
PERCENT_TOL = 0.01

# T5: the baseline's pre-existing tiles/table cells show rounded currency/percent
# text (money() rounds to whole dollars in a KPI tile; the channel table's Total
# row and pct1() round to a tenth of a percent). The PRD specifies a wider
# tolerance for this one comparison for exactly that reason.
BASELINE_CURRENCY_TOL = 0.5
BASELINE_PERCENT_TOL = 0.05

RANGE_CAPTION_TEXT_PERIOD = "Not range-aware: MTD and YTD only"
NO_DATA_LABEL = "No data for that range"
SKU_DISCLOSURE_TEXT = (
    "Custom-range SKU figures come from the full monthly SKU series and can differ from the MTD and YTD tables"
)
REFRESH_STATUS_RE = re.compile(r"^Refresh requested \d{1,2}:\d{2} MT; expected by \d{1,2}:\d{2} MT$")

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _range_month_label(ym: str) -> str:
    """Mirrors template.html's rangeMonthLabel(ym): 'YYYY-MM' -> 'Mon YYYY'."""
    year, month = ym.split("-")
    return f"{MONTH_ABBR[int(month) - 1]} {year}"


def _format_date_long(date_str: str) -> str:
    """Mirrors template.html's formatDateLong: 'YYYY-MM-DD' -> 'Mon D, YYYY'."""
    year, month, day = date_str.split("-")
    return f"{MONTH_ABBR[int(month) - 1]} {int(day)}, {year}"


# ---------------------------------------------------------------------------
# Number parsing / comparison helpers
# ---------------------------------------------------------------------------

def parse_number(text):
    """PRD-month-refresh.md Section 7 T5: 'parse digits, commas, minus, decimal'.
    Keeps exactly those four character classes (in order) out of a formatted
    currency/percent string and parses the remainder as a float. Handles the
    plain-minus-sign negative formatting Intl.NumberFormat({currencySign:
    'standard'}) produces (e.g. '-$1,234.56'), which is everything this
    template's money()/moneyCents()/pct1() helpers emit."""
    assert text is not None, "no text to parse a number from"
    kept = re.sub(r"[^0-9,.\-]", "", text)
    assert kept, f"no numeric characters found in {text!r}"
    return float(kept.replace(",", ""))


def assert_close(actual, expected, tol, label):
    diff = abs(actual - expected)
    assert diff <= tol, (
        f"{label}: actual={actual!r} expected={expected!r} "
        f"abs_diff={diff!r} tolerance={tol!r}"
    )


# ---------------------------------------------------------------------------
# Data fixtures (module scope: skip the whole module, with a message, when the
# local pull is absent -- same contract as conftest.py's function-scoped
# latest_data/latest_data_path, re-implemented here at module scope because a
# module-scoped fixture cannot depend on a function-scoped one).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def latest_json_path():
    if not DATA_PATH.is_file():
        pytest.skip(f"{DATA_PATH} not present -- run spike/extract.py locally first")
    return DATA_PATH


@pytest.fixture(scope="module")
def latest_data(latest_json_path):
    return json.loads(latest_json_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def trailing_months(latest_data):
    return latest_data["meta"]["trailing_months"]


@pytest.fixture(scope="module")
def asof_date(latest_data):
    return latest_data["meta"]["asof_date"]


@pytest.fixture(scope="module")
def rollup_by_month(latest_data):
    return latest_data["rollup_by_month"]


@pytest.fixture(scope="module")
def rollup_keys(rollup_by_month):
    return sorted({r["key"] for r in rollup_by_month})


@pytest.fixture(scope="module")
def rollup_by_period_total(latest_data):
    for row in latest_data["rollup_by_period"]:
        if row["key"] == "total":
            return row
    raise AssertionError("rollup_by_period has no key='total' row")


def _sum_months(rows, months, fields, keyfn=None):
    """Plain Python sum of `fields` over `rows` whose 'ym' is in `months`,
    optionally grouped by keyfn(row). Mirrors template.html's sumRows()."""
    months = set(months)
    if keyfn is None:
        totals = {f: 0.0 for f in fields}
        for r in rows:
            if r["ym"] in months:
                for f in fields:
                    totals[f] += r.get(f) or 0.0
        return totals
    out = collections.defaultdict(lambda: {f: 0.0 for f in fields})
    for r in rows:
        if r["ym"] not in months:
            continue
        key = keyfn(r)
        for f in fields:
            out[key][f] += r.get(f) or 0.0
    return out


# ---------------------------------------------------------------------------
# Build fixtures: each distinct HTML this module needs is built exactly once,
# into one tmp_path_factory directory shared by the whole module.
# ---------------------------------------------------------------------------

def _run_build(out_path, *, template=None, features=None, refresh_url=None):
    """Invokes design/mockup/build.py exactly as documented (PRD-month-refresh.md
    Section 7 / the build agent's brief): `python build.py --data
    spike/data/latest.json --out <out> [--template ...] [--features ...]
    [--refresh-url ...]`. --features/--refresh-url are passed explicitly
    whenever this module wants a specific value (including the empty string)
    so the ambient SPIKEBALL_DASH_FEATURES / SPIKEBALL_REFRESH_REQUEST_URL env
    vars can never leak into a build; when template/features/refresh_url is
    None the corresponding flag is omitted entirely (used only for T5's
    baseline build, which must reproduce a build invocation with neither flag
    present). The env vars are always scrubbed from the subprocess regardless,
    so omitting a flag can never pick up an ambient value either."""
    cmd = [sys.executable, str(BUILD_PY), "--data", str(DATA_PATH), "--out", str(out_path)]
    if template is not None:
        cmd += ["--template", str(template)]
    if features is not None:
        cmd += ["--features", features]
    if refresh_url is not None:
        cmd += ["--refresh-url", refresh_url]
    env = dict(os.environ)
    env.pop("SPIKEBALL_DASH_FEATURES", None)
    env.pop("SPIKEBALL_REFRESH_REQUEST_URL", None)
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, env=env)
    assert result.returncode == 0, (
        f"build.py failed (exit {result.returncode}) building {out_path.name}\n"
        f"cmd: {cmd}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert out_path.is_file(), f"build.py exited 0 but did not write {out_path}"


@pytest.fixture(scope="module")
def build_dir(tmp_path_factory, latest_json_path):
    return tmp_path_factory.mktemp("dashboard_range_html")


@pytest.fixture(scope="module")
def html_on_path(build_dir):
    """Both features on, fixture refresh URL -- the workhorse build for T1-T4,
    T6-T8d, T12."""
    out = build_dir / "on.html"
    _run_build(out, features="range_selector,refresh_control", refresh_url=FIXTURE_REFRESH_URL)
    return out


@pytest.fixture(scope="module")
def html_flags_off_current_path(build_dir):
    """Current (edited) template.html, both flags explicitly off -- T5's
    'current build' half of the flags-off identity check."""
    out = build_dir / "off_current.html"
    _run_build(out, features="", refresh_url="")
    return out


@pytest.fixture(scope="module")
def html_flags_off_baseline_path(build_dir):
    """Verbatim pre-change template, no --features/--refresh-url flags at all
    (T5's literal build recipe for the baseline)."""
    out = build_dir / "off_baseline.html"
    _run_build(out, template=BASELINE_TEMPLATE)
    return out


@pytest.fixture(scope="module")
def html_no_refresh_url_path(build_dir):
    """range_selector + refresh_control on, but refresh_request_url empty --
    T8's '--refresh-url \"\" -> link and status absent' case."""
    out = build_dir / "no_refresh_url.html"
    _run_build(out, features="range_selector,refresh_control", refresh_url="")
    return out


@pytest.fixture(scope="module")
def html_no_refresh_feature_path(build_dir):
    """range_selector only (refresh_control absent) with the URL still set --
    T8's 'features lacking refresh_control -> link absent even with URL set'
    case."""
    out = build_dir / "no_refresh_feature.html"
    _run_build(out, features="range_selector", refresh_url=FIXTURE_REFRESH_URL)
    return out


# ---------------------------------------------------------------------------
# Browser / page helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:  # patchright installed, its chromium build not
            pytest.skip(
                f"chromium not available for patchright ({type(exc).__name__}); "
                "run: python3 -m patchright install chromium"
            )
        yield b
        b.close()


@contextmanager
def dash_page(browser, *, timezone_id=None, init_script=None):
    """Opens a fresh browser context + page, wired to collect console errors
    and page errors (task brief: 'Collect console errors via page.on("console")
    and page.on("pageerror") and assert none'). Yields (page, errors); the
    caller is responsible for asserting `not errors` at the point in the test
    that should be error-free (usually the end)."""
    kwargs = {}
    if timezone_id is not None:
        kwargs["timezone_id"] = timezone_id
    context = browser.new_context(**kwargs)
    if init_script is not None:
        context.add_init_script(init_script)
    page = context.new_page()
    errors = []
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    try:
        yield page, errors
    finally:
        context.close()


def goto(page, html_path: Path, hash_str: str = ""):
    url = html_path.resolve().as_uri()
    if hash_str:
        url += "#" + hash_str.lstrip("#")
    page.goto(url)


def data_raw(page, selector) -> float:
    val = page.get_attribute(selector, "data-raw")
    assert val is not None, f"{selector} has no data-raw attribute (or does not exist)"
    return float(val)


def range_caption_minus_text(page, section_id):
    """Returns the innerText of #<section_id> with any .range-caption
    descendants temporarily detached first (innerText is layout-dependent and
    returns '' for a fully-detached clone in Chromium, so this removes-then-
    restores on the live, still-rendered element instead of cloning)."""
    js = """
    (id) => {
      const el = document.getElementById(id);
      if (!el) return null;
      const captions = Array.from(el.querySelectorAll('.range-caption'));
      const removed = captions.map(c => ({node: c, next: c.nextSibling, parent: c.parentNode}));
      captions.forEach(c => c.remove());
      const text = el.innerText;
      removed.forEach(r => r.parent.insertBefore(r.node, r.next));
      return text;
    }
    """
    return page.evaluate(js, section_id)


# ===========================================================================
# T1 -- Range tie-out
# ===========================================================================

def test_t1_range_tie_out(browser, html_on_path, rollup_by_period_total, rollup_by_month):
    """PRD Section 7 T1: via the UI set #range-from/#range-to to the YTD
    months; KPI sales/gm_dollars/gm_pct equal rollup_by_period[key=total].ytd
    exactly. Repeat for the single current month against .mtd, and for
    2026-03..2026-05 against python sums of all five rollup_by_month keys."""
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=mtd")

        # -- Sub-case: YTD months, set via the UI selects --------------------
        page.select_option("#range-from", "2026-01")
        page.select_option("#range-to", "2026-09")
        ytd = rollup_by_period_total["ytd"]
        assert_close(data_raw(page, '[data-kpi="sales"]'), ytd["revenue"], CURRENCY_TOL, "T1 ytd sales")
        assert_close(data_raw(page, '[data-kpi="gm_dollars"]'), ytd["gp"], CURRENCY_TOL, "T1 ytd gm_dollars")
        assert_close(data_raw(page, '[data-kpi="gm_pct"]'), ytd["margin_pct"], PERCENT_TOL, "T1 ytd gm_pct")

        # -- Sub-case: single current month, set via the UI selects ----------
        page.select_option("#range-from", "2026-09")
        page.select_option("#range-to", "2026-09")
        mtd = rollup_by_period_total["mtd"]
        assert_close(data_raw(page, '[data-kpi="sales"]'), mtd["revenue"], CURRENCY_TOL, "T1 mtd sales")
        assert_close(data_raw(page, '[data-kpi="gm_dollars"]'), mtd["gp"], CURRENCY_TOL, "T1 mtd gm_dollars")
        assert_close(data_raw(page, '[data-kpi="gm_pct"]'), mtd["margin_pct"], PERCENT_TOL, "T1 mtd gm_pct")

        # -- Sub-case: 2026-03..2026-05, set via the UI selects ---------------
        page.select_option("#range-from", "2026-03")
        page.select_option("#range-to", "2026-05")
        months = ["2026-03", "2026-04", "2026-05"]
        totals = _sum_months(rollup_by_month, months, ["revenue", "gp"])
        expected_margin = totals["gp"] / totals["revenue"] * 100
        assert_close(data_raw(page, '[data-kpi="sales"]'), totals["revenue"], CURRENCY_TOL, "T1 Mar-May sales")
        assert_close(data_raw(page, '[data-kpi="gm_dollars"]'), totals["gp"], CURRENCY_TOL, "T1 Mar-May gm_dollars")
        assert_close(data_raw(page, '[data-kpi="gm_pct"]'), expected_margin, PERCENT_TOL, "T1 Mar-May gm_pct")

    assert not errors, f"console/page errors during T1: {errors}"


# ===========================================================================
# T1b -- 13m preset equals the custom full range
# ===========================================================================

def test_t1b_13m_equals_custom_full_range(browser, html_on_path, trailing_months):
    first, last = trailing_months[0], trailing_months[-1]
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=13m")
        preset_sales = data_raw(page, '[data-kpi="sales"]')
        preset_gp = data_raw(page, '[data-kpi="gm_dollars"]')
        preset_pct = data_raw(page, '[data-kpi="gm_pct"]')

        goto(page, html_on_path, f"period=custom&from={first}&to={last}")
        custom_sales = data_raw(page, '[data-kpi="sales"]')
        custom_gp = data_raw(page, '[data-kpi="gm_dollars"]')
        custom_pct = data_raw(page, '[data-kpi="gm_pct"]')

    assert not errors, f"console/page errors during T1b: {errors}"
    assert_close(custom_sales, preset_sales, CURRENCY_TOL, "T1b sales (13m vs custom full range)")
    assert_close(custom_gp, preset_gp, CURRENCY_TOL, "T1b gm_dollars (13m vs custom full range)")
    assert_close(custom_pct, preset_pct, PERCENT_TOL, "T1b gm_pct (13m vs custom full range)")


# ===========================================================================
# T1c -- from > to swaps
# ===========================================================================

def test_t1c_from_gt_to_swaps(browser, html_on_path):
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "from=2026-05&to=2026-03")
        swapped_sales = data_raw(page, '[data-kpi="sales"]')
        swapped_label = page.inner_text("#range-label")

        goto(page, html_on_path, "from=2026-03&to=2026-05")
        ordered_sales = data_raw(page, '[data-kpi="sales"]')
        ordered_label = page.inner_text("#range-label")

    assert not errors, f"console/page errors during T1c: {errors}"
    assert_close(swapped_sales, ordered_sales, CURRENCY_TOL, "T1c sales (from>to swap)")
    assert swapped_label == ordered_label, (
        f"T1c: #range-label differs between swapped and ordered hash: "
        f"{swapped_label!r} vs {ordered_label!r}"
    )


# ===========================================================================
# T1d -- Partial hash
# ===========================================================================

def test_t1d_partial_hash(browser, html_on_path):
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=custom&from=2026-03")
        to_val = page.eval_on_selector("#range-to", "e => e.value")
        assert to_val == "2026-03", f"T1d: #range-to should mirror from= (2026-03), got {to_val!r}"

        goto(page, html_on_path, "period=custom&to=2026-05")
        from_val = page.eval_on_selector("#range-from", "e => e.value")
        assert from_val == "2026-05", f"T1d: #range-from should mirror to= (2026-05), got {from_val!r}"

    assert not errors, f"console/page errors during T1d: {errors}"


# ===========================================================================
# T1e -- Out-of-range
# ===========================================================================

def test_t1e_out_of_range(browser, html_on_path):
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=custom&from=2024-01&to=2024-06")
        label = page.inner_text("#range-label")

    assert label == NO_DATA_LABEL, f"T1e: #range-label expected {NO_DATA_LABEL!r}, got {label!r}"
    assert not errors, f"T1e: expected zero console errors, got {errors}"


# ===========================================================================
# T1f -- Open month label
# ===========================================================================

def test_t1f_open_month_label(browser, html_on_path, asof_date):
    expected_suffix = f"(through {_format_date_long(asof_date)} MT)"
    with dash_page(browser) as (page, errors):
        # Single current month via the mtd preset.
        goto(page, html_on_path, "period=mtd")
        mtd_label = page.inner_text("#range-label")
        expected_mtd = f"{_range_month_label('2026-09')} {expected_suffix}"

        # A multi-month custom range ending in the current month.
        goto(page, html_on_path, "period=custom&from=2026-07&to=2026-09")
        custom_label = page.inner_text("#range-label")
        expected_custom = f"{_range_month_label('2026-07')} to {_range_month_label('2026-09')} {expected_suffix}"

    assert not errors, f"console/page errors during T1f: {errors}"
    assert mtd_label == expected_mtd, f"T1f (mtd): expected {expected_mtd!r}, got {mtd_label!r}"
    assert custom_label == expected_custom, f"T1f (custom): expected {expected_custom!r}, got {custom_label!r}"


# ===========================================================================
# T2 -- Channel table
# ===========================================================================

def test_t2_channel_table_tie_out(browser, html_on_path, rollup_by_month, rollup_keys):
    months = ["2026-03", "2026-04", "2026-05"]
    expected = _sum_months(rollup_by_month, months, ["revenue", "cogs", "gp"], keyfn=lambda r: r["key"])

    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=custom&from=2026-03&to=2026-05")
        dom_keys = page.eval_on_selector_all(
            "#channel-table tbody tr[data-key]", "els => els.map(e => e.getAttribute('data-key'))"
        )
        assert sorted(dom_keys) == sorted(rollup_keys), (
            f"T2: #channel-table tr[data-key] set {sorted(dom_keys)} != rollup_by_month keys {sorted(rollup_keys)}"
        )
        for key in rollup_keys:
            row_sel = f'#channel-table tbody tr[data-key="{key}"]'
            dom_rev = data_raw(page, f'{row_sel} td[data-col="revenue"]')
            dom_cogs = data_raw(page, f'{row_sel} td[data-col="cogs"]')
            dom_gp = data_raw(page, f'{row_sel} td[data-col="gp"]')

            exp = expected[key]
            assert_close(dom_rev, exp["revenue"], CURRENCY_TOL, f"T2 {key} revenue")
            assert_close(dom_cogs, exp["cogs"], CURRENCY_TOL, f"T2 {key} cogs")
            assert_close(dom_gp, exp["gp"], CURRENCY_TOL, f"T2 {key} gp")

            margin_cell = f'{row_sel} td[data-col="margin_pct"]'
            if exp["revenue"] == 0:
                # setRangeHook (template.html) omits data-raw when the ratio's
                # denominator is zero (margin_pct is null) -- true for
                # 'unassigned' over 2026-03..2026-05 (zero revenue every
                # month), not a bug: assert the attribute is correctly absent
                # rather than asserting a ratio that cannot exist.
                raw = page.get_attribute(margin_cell, "data-raw")
                assert raw is None, (
                    f"T2 {key}: expected data-raw absent on margin_pct (zero revenue -> null ratio), got {raw!r}"
                )
            else:
                exp_margin = exp["gp"] / exp["revenue"] * 100
                dom_margin = data_raw(page, margin_cell)
                assert_close(dom_margin, exp_margin, PERCENT_TOL, f"T2 {key} margin_pct")

    assert not errors, f"console/page errors during T2: {errors}"


# ===========================================================================
# T3 -- Ratio rule
# ===========================================================================

def test_t3_ratio_rule(browser, html_on_path, rollup_by_month, trailing_months, asof_date):
    year = asof_date[:4]
    ytd_months = sorted(m for m in trailing_months if m.startswith(year))

    monthly_margins = []
    tot_rev = 0.0
    tot_gp = 0.0
    for m in ytd_months:
        one = _sum_months(rollup_by_month, [m], ["revenue", "gp"])
        tot_rev += one["revenue"]
        tot_gp += one["gp"]
        assert one["revenue"] != 0, f"T3: month {m} has zero total revenue, cannot form a monthly margin"
        monthly_margins.append(one["gp"] / one["revenue"] * 100)

    mean_of_monthly = sum(monthly_margins) / len(monthly_margins)
    ratio_of_sums = tot_gp / tot_rev * 100

    # The data fact this test exists to prove: on this data, averaging the
    # monthly ratios gives a materially different (and wrong) number than the
    # ratio of the summed dollars.
    assert abs(mean_of_monthly - ratio_of_sums) > 0.01, (
        f"T3: mean-of-monthly-margins ({mean_of_monthly!r}) and ratio-of-sums ({ratio_of_sums!r}) "
        f"do not differ by more than 0.01 on this data -- the test's premise no longer holds"
    )

    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=ytd")
        dom_pct = data_raw(page, '[data-kpi="gm_pct"]')

    assert not errors, f"console/page errors during T3: {errors}"
    assert_close(dom_pct, ratio_of_sums, PERCENT_TOL, "T3 gm_pct (must be the ratio of sums)")
    assert abs(dom_pct - mean_of_monthly) > 0.01, (
        f"T3: the page's gm_pct ({dom_pct!r}) matches the mean-of-monthly-margins "
        f"({mean_of_monthly!r}) instead of the ratio of sums ({ratio_of_sums!r})"
    )


# ===========================================================================
# T4 -- SKU
# ===========================================================================

def test_t4_sku_tie_out(browser, html_on_path, latest_data, trailing_months, asof_date):
    year = asof_date[:4]
    ytd_months = sorted(m for m in trailing_months if m.startswith(year))
    ytd_month_set = set(ytd_months)

    sku_sales_ytd_revenue = collections.defaultdict(float)
    for row in latest_data["sku_sales"]["ytd"]:
        sku_sales_ytd_revenue[row["sku"]] += row["revenue"]

    month_units = collections.defaultdict(float)
    month_revenue = collections.defaultdict(float)
    for row in latest_data["sku_sales_month"]:
        if row["ym"] in ytd_month_set:
            month_units[row["sku"]] += row["units"] or 0.0
            month_revenue[row["sku"]] += row["revenue"] or 0.0

    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, f"period=custom&from={ytd_months[0]}&to={ytd_months[-1]}")
        rows = page.eval_on_selector_all(
            "#moving-grid [data-sku]",
            "els => els.map(e => ({sku: e.getAttribute('data-sku'), "
            "rev: e.getAttribute('data-raw-revenue'), units: e.getAttribute('data-raw-units')}))",
        )
        caption = page.inner_text("#moving-grid .concentration-note")

    assert not errors, f"console/page errors during T4: {errors}"
    assert rows, "T4: #moving-grid held no [data-sku] rows for the YTD custom range"
    assert caption == SKU_DISCLOSURE_TEXT, f"T4: disclosure caption expected {SKU_DISCLOSURE_TEXT!r}, got {caption!r}"

    checked_against_ytd = 0
    for row in rows:
        sku = row["sku"]
        dom_rev = float(row["rev"])
        dom_units = float(row["units"])

        # Units: internal correctness only, against the unconditional sum of
        # sku_sales_month -- never against sku_sales.ytd (Section 12 item 3).
        exp_units = month_units.get(sku)
        assert exp_units is not None, f"T4: {sku} in #moving-grid has no sku_sales_month rows in the YTD range"
        assert_close(dom_units, exp_units, 0.005, f"T4 {sku} units (internal, vs sku_sales_month sum)")

        exp_month_rev = month_revenue.get(sku, 0.0)
        assert_close(dom_rev, exp_month_rev, CURRENCY_TOL, f"T4 {sku} revenue (vs sku_sales_month sum)")

        # Revenue: for every SKU present in both the DOM and sku_sales.ytd,
        # the precomputed table is a display subset of (channel, sku) rows, so its per-SKU aggregate is a lower bound, never higher (Section 12 item 3, status update 2026-09-08).
        if sku in sku_sales_ytd_revenue:
            checked_against_ytd += 1
            assert sku_sales_ytd_revenue[sku] <= dom_rev + CURRENCY_TOL, (
                f"T4 {sku} revenue: sku_sales.ytd aggregate {sku_sales_ytd_revenue[sku]!r} exceeds the page value {dom_rev!r}; "
                f"sku_sales.ytd is a display subset of (channel, sku) rows (extract.py top-25 logic) so it may be lower, never higher")

    assert checked_against_ytd > 0, "T4: none of the #moving-grid SKUs were found in sku_sales.ytd -- tie-out was vacuous"


# ===========================================================================
# T5 -- Preset identity
# ===========================================================================

def test_t5_preset_identity(browser, html_flags_off_current_path, html_flags_off_baseline_path, html_on_path):
    # -- Part 1: flags-off byte identity (the hard requirement) --------------
    with dash_page(browser) as (page, errors):
        main_texts = {}
        for period in ("mtd", "ytd"):
            goto(page, html_flags_off_baseline_path, f"period={period}")
            baseline_text = page.inner_text("#main")
            goto(page, html_flags_off_current_path, f"period={period}")
            current_text = page.inner_text("#main")
            main_texts[period] = (baseline_text, current_text)

        # DOM absence checks on the current (edited) flags-off build.
        goto(page, html_flags_off_current_path, "period=mtd")
        for selector in ("#range-from", "#refresh-request-link", ".range-caption"):
            count = page.eval_on_selector_all(selector, "els => els.length")
            assert count == 0, f"T5: flags-off current build unexpectedly contains {count} of {selector!r}"

    assert not errors, f"console/page errors during T5 part 1: {errors}"

    for period, (baseline_text, current_text) in main_texts.items():
        assert baseline_text == current_text, (
            f"T5: #main innerText differs between the baseline and current flags-off builds "
            f"for period={period} (baseline len={len(baseline_text)}, current len={len(current_text)})"
        )

    # -- Part 2: with features on, preset KPI data-raw equals the baseline's --
    # displayed period totals. The pre-existing KPI row has no MTD/YTD
    # gross-profit-dollar tile (only "MTD sales", "MTD/YTD gross margin", and
    # the YTD-sales hero), so this anchors on the pre-existing channel table's
    # Total row instead, which the baseline template already renders
    # unconditionally for both MTD and YTD (Revenue/COGS/GP/Margin,
    # moneyCents()/pct1() formatted) -- the same three quantities (sales,
    # gross profit dollars, gross margin percent) the new range tiles show.
    with dash_page(browser) as (page, errors):
        baseline_totals = {}
        for period in ("mtd", "ytd"):
            goto(page, html_flags_off_baseline_path, f"period={period}")
            total_row = "#channel-table tbody tr.total-row"
            rev_text = page.inner_text(f"{total_row} td:nth-child(2)")
            gp_text = page.inner_text(f"{total_row} td:nth-child(4)")
            margin_text = page.inner_text(f"{total_row} td:nth-child(5)")
            baseline_totals[period] = {
                "sales": parse_number(rev_text),
                "gm_dollars": parse_number(gp_text),
                "gm_pct": parse_number(margin_text),
            }

        current_totals = {}
        for period in ("mtd", "ytd"):
            goto(page, html_on_path, f"period={period}")
            current_totals[period] = {
                "sales": data_raw(page, '[data-kpi="sales"]'),
                "gm_dollars": data_raw(page, '[data-kpi="gm_dollars"]'),
                "gm_pct": data_raw(page, '[data-kpi="gm_pct"]'),
            }

    assert not errors, f"console/page errors during T5 part 2: {errors}"

    for period in ("mtd", "ytd"):
        b = baseline_totals[period]
        c = current_totals[period]
        assert_close(
            c["sales"], b["sales"], BASELINE_CURRENCY_TOL,
            f"T5 {period} sales (current data-raw vs baseline channel-table Total, "
            f"tolerance widened to {BASELINE_CURRENCY_TOL} because the baseline text is rounded currency)",
        )
        assert_close(
            c["gm_dollars"], b["gm_dollars"], BASELINE_CURRENCY_TOL,
            f"T5 {period} gm_dollars (current data-raw vs baseline channel-table Total GP, "
            f"tolerance widened to {BASELINE_CURRENCY_TOL} because the baseline text is rounded currency)",
        )
        assert_close(
            c["gm_pct"], b["gm_pct"], BASELINE_PERCENT_TOL,
            f"T5 {period} gm_pct (current data-raw vs baseline channel-table Total margin, "
            f"tolerance widened to {BASELINE_PERCENT_TOL} because the baseline text is rounded to one decimal)",
        )


# ===========================================================================
# T6 -- Non-range sections
# ===========================================================================

def test_t6_non_range_sections(browser, html_on_path, asof_date):
    section_ids = [
        "inventory-section",
        "working-capital-section",
        "amazon-section",
        "returns-section",
        "orders-aov-card",
    ]
    expected_caption = {
        "inventory-section": f"Not range-aware: as of {asof_date} MT",
        "working-capital-section": f"Not range-aware: as of {asof_date} MT",
        "amazon-section": RANGE_CAPTION_TEXT_PERIOD,
        "returns-section": RANGE_CAPTION_TEXT_PERIOD,
        "orders-aov-card": RANGE_CAPTION_TEXT_PERIOD,
    }

    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=mtd")
        for sid in section_ids:
            count = page.eval_on_selector_all(f"#{sid} .range-caption", "els => els.length")
            assert count == 0, f"T6: {sid} unexpectedly has a .range-caption under the mtd preset"
        before = {sid: range_caption_minus_text(page, sid) for sid in section_ids}

        page.select_option("#range-from", "2026-03")
        page.select_option("#range-to", "2026-05")

        after = {sid: range_caption_minus_text(page, sid) for sid in section_ids}
        captions = {}
        for sid in section_ids:
            captions[sid] = page.inner_text(f"#{sid} .range-caption")

    assert not errors, f"console/page errors during T6: {errors}"

    for sid in section_ids:
        assert before[sid] == after[sid], (
            f"T6: {sid} content (minus .range-caption) changed when switching from the mtd preset "
            f"to the 2026-03..2026-05 custom range"
        )
        assert captions[sid] == expected_caption[sid], (
            f"T6: {sid} .range-caption expected {expected_caption[sid]!r}, got {captions[sid]!r}"
        )


# ===========================================================================
# T6b -- Sparse series
# ===========================================================================

def test_t6b_sparse_series(browser, html_on_path, latest_data):
    cf_rows = latest_data["cf_month"]
    expected_net_income = sum(
        r["amount"] for r in cf_rows if r["ym"] in ("2026-07", "2026-08") and r["cf_line"] == "Net Income"
    )

    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=custom&from=2026-01&to=2026-08")
        cf_note = page.inner_text("#cashflow-section .range-note")
        dom_net_income = data_raw(page, '#cashflow-table tr[data-cf-line="Net Income"] td[data-col="amount"]')
        bs_note = page.inner_text("#balance-sheet-section .range-note")

    assert not errors, f"console/page errors during T6b: {errors}"
    assert cf_note == "Cash flow available from 2026-07", (
        f"T6b: #cashflow-section .range-note expected 'Cash flow available from 2026-07', got {cf_note!r}"
    )
    assert_close(dom_net_income, expected_net_income, CURRENCY_TOL, "T6b cash flow Net Income (2026-07+2026-08 only)")
    assert bs_note == "Balance sheet as of 2026-08", (
        f"T6b: #balance-sheet-section .range-note expected 'Balance sheet as of 2026-08', got {bs_note!r}"
    )


# ===========================================================================
# T7 -- Hash round trip
# ===========================================================================

def test_t7_hash_round_trip(browser, html_on_path):
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=custom&from=2026-03&to=2026-05")
        from_val = page.eval_on_selector("#range-from", "e => e.value")
        to_val = page.eval_on_selector("#range-to", "e => e.value")
        label = page.inner_text("#range-label")
        assert from_val == "2026-03" and to_val == "2026-05", (
            f"T7: initial selects expected (2026-03, 2026-05), got ({from_val!r}, {to_val!r})"
        )
        assert label == f"{_range_month_label('2026-03')} to {_range_month_label('2026-05')}", (
            f"T7: initial #range-label unexpected: {label!r}"
        )

        page.select_option("#range-to", "2026-06")
        current_hash = page.eval_on_selector("html", "e => location.hash")
        assert "to=2026-06" in current_hash, f"T7: expected 'to=2026-06' in location.hash, got {current_hash!r}"

        page.click('.period-btn[data-period="ytd"]')
        final_hash = page.eval_on_selector("html", "e => location.hash")
        assert "from=" not in final_hash and "to=" not in final_hash, (
            f"T7: expected no from=/to= after clicking the YTD preset, got {final_hash!r}"
        )
        final_from = page.eval_on_selector("#range-from", "e => e.value")
        final_to = page.eval_on_selector("#range-to", "e => e.value")
        assert final_from == "2026-01" and final_to == "2026-09", (
            f"T7: after the YTD preset, expected selects (2026-01, 2026-09), got ({final_from!r}, {final_to!r})"
        )

    assert not errors, f"console/page errors during T7: {errors}"


# ===========================================================================
# T8 -- Refresh control
# ===========================================================================

def test_t8_refresh_control(browser, html_on_path, html_no_refresh_url_path, html_no_refresh_feature_path):
    # -- Link attributes, real click -> popup, localStorage, status text -----
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path)
        link = page.locator("#refresh-request-link")
        assert link.get_attribute("href") == FIXTURE_REFRESH_URL, "T8: #refresh-request-link href mismatch"
        assert link.get_attribute("target") == "_blank", "T8: #refresh-request-link target mismatch"
        assert link.get_attribute("rel") == "noopener", "T8: #refresh-request-link rel mismatch"

        with page.expect_popup() as popup_info:
            link.click()
        popup = popup_info.value
        popup_origin = popup.evaluate("() => location.origin")
        fixture_origin = "https://script.google.com"
        assert popup_origin == fixture_origin, f"T8: popup origin {popup_origin!r} != {fixture_origin!r}"
        popup.close()

        stored = page.evaluate("() => localStorage.getItem('sb-fin-refresh-requested')")
        assert stored, "T8: localStorage['sb-fin-refresh-requested'] was not set after the click"
        from datetime import datetime
        parsed_ok = True
        try:
            datetime.fromisoformat(stored.replace("Z", "+00:00"))
        except ValueError:
            parsed_ok = False
        assert parsed_ok, f"T8: stored refresh-requested value is not a parseable ISO timestamp: {stored!r}"

        status = page.inner_text("#refresh-status")
        assert REFRESH_STATUS_RE.match(status), f"T8: #refresh-status {status!r} did not match {REFRESH_STATUS_RE.pattern!r}"

    assert not errors, f"console/page errors during T8 (main flow): {errors}"

    # -- refresh_request_url empty -> link and status absent -----------------
    with dash_page(browser) as (page, errors):
        goto(page, html_no_refresh_url_path)
        assert page.eval_on_selector_all("#refresh-request-link", "els => els.length") == 0, (
            "T8: #refresh-request-link present with refresh_request_url=''"
        )
        assert page.eval_on_selector_all("#refresh-status", "els => els.length") == 0, (
            "T8: #refresh-status present with refresh_request_url=''"
        )
    assert not errors, f"console/page errors during T8 (empty url): {errors}"

    # -- refresh_control feature absent (URL still set) -> link absent -------
    with dash_page(browser) as (page, errors):
        goto(page, html_no_refresh_feature_path)
        assert page.eval_on_selector_all("#refresh-request-link", "els => els.length") == 0, (
            "T8: #refresh-request-link present with refresh_control absent from meta.features"
        )
    assert not errors, f"console/page errors during T8 (feature absent): {errors}"

    # -- pulled_at_mt later than a pre-seeded stored request -> cleared on load
    # (seeded via an init script so it is set before the page's own scripts run,
    # to a timestamp well before this data pull's meta.pulled_at_mt).
    seed_script = (
        "try { window.localStorage.setItem('sb-fin-refresh-requested', '2020-01-01T00:00:00.000Z'); } catch(e) {}"
    )
    with dash_page(browser, init_script=seed_script) as (page, errors):
        goto(page, html_on_path)
        status = page.inner_text("#refresh-status")
        assert status == "", (
            f"T8: #refresh-status expected empty (stored request predates meta.pulled_at_mt), got {status!r}"
        )
        stored_after_load = page.evaluate("() => localStorage.getItem('sb-fin-refresh-requested')")
        assert not stored_after_load, (
            f"T8: stale stored refresh request was not cleared on load: {stored_after_load!r}"
        )
    assert not errors, f"console/page errors during T8 (stale stored request): {errors}"


# ===========================================================================
# T8b -- Cadence copy
# ===========================================================================

def _expected_nightly_label():
    """The '~4am MT' label build.py/template.html derive from SPIKEBALL_NIGHTLY_SLOT_UTC
    (default 9) for today's date in America/Denver, so the test follows the env and
    daylight saving exactly as the page does."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    raw = os.environ.get("SPIKEBALL_NIGHTLY_SLOT_UTC", "").strip()
    try:
        slot = int(raw) if raw else 9
    except ValueError:
        slot = 9
    if not 0 <= slot <= 23:
        slot = 9
    h = datetime.now(timezone.utc).replace(hour=slot, minute=0, second=0, microsecond=0)
    h = h.astimezone(ZoneInfo("America/Denver")).hour
    h12 = h % 12 or 12
    return f"~{h12}{'am' if h < 12 else 'pm'} MT"

def test_t8b_cadence_copy(browser, html_on_path, html_no_refresh_feature_path):
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path)
        rendered_text = page.inner_text("#asof-cadence")

        goto(page, html_no_refresh_feature_path)
        baseline_text = page.inner_text("#asof-cadence")

    assert not errors, f"console/page errors during T8b: {errors}"
    assert rendered_text == f"refreshes nightly {_expected_nightly_label()}, or within the hour on request", (
        f"T8b: #asof-cadence (control rendered) unexpected text: {rendered_text!r}"
    )
    assert baseline_text == "refreshes nightly ~3am MT", (
        f"T8b: #asof-cadence (refresh_control off) unexpected text: {baseline_text!r}"
    )


# ===========================================================================
# T8c -- Timezone independence
# ===========================================================================

def test_t8c_timezone_independence(browser, html_on_path):
    expected_by_re = re.compile(r"expected by (\d{1,2}:\d{2} MT)$")

    results = {}
    for tz in ("America/Denver", "Asia/Kolkata"):
        with dash_page(browser, timezone_id=tz) as (page, errors):
            goto(page, html_on_path)
            page.click("#refresh-request-link")
            status = page.inner_text("#refresh-status")
        assert not errors, f"console/page errors during T8c ({tz}): {errors}"
        match = expected_by_re.search(status)
        assert match, f"T8c: #refresh-status under timezone {tz} did not match: {status!r}"
        results[tz] = match.group(1)

    assert results["America/Denver"] == results["Asia/Kolkata"], (
        f"T8c: 'expected by' time differs across browser context timezones: {results!r}"
    )


# ===========================================================================
# T8d -- Storage disabled
# ===========================================================================

def test_t8d_storage_disabled(browser, html_on_path):
    init_script = """
    Object.defineProperty(window, 'localStorage', {
      get() { throw new Error('localStorage disabled for T8d'); }
    });
    """
    with dash_page(browser, init_script=init_script) as (page, errors):
        goto(page, html_on_path)
        with page.expect_popup() as popup_info:
            page.click("#refresh-request-link")
        popup = popup_info.value
        popup_origin = popup.evaluate("() => location.origin")
        assert popup_origin == "https://script.google.com", (
            f"T8d: popup still opened at the wrong origin: {popup_origin!r}"
        )
        popup.close()

        status = page.inner_text("#refresh-status")
        assert REFRESH_STATUS_RE.match(status), (
            f"T8d: #refresh-status did not render with localStorage disabled: {status!r}"
        )

    assert not errors, f"console/page errors during T8d: {errors}"


# ===========================================================================
# T12 -- Both themes, two widths
# ===========================================================================

def test_t12_screenshots(browser, html_on_path):
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    viewports = [(1440, 900, "1440x900"), (390, 844, "390x844")]
    themes = ["light", "dark"]

    written = []
    with dash_page(browser) as (page, errors):
        goto(page, html_on_path, "period=custom&from=2026-03&to=2026-05")
        page.wait_for_selector('[data-kpi="sales"][data-raw]')

        for width, height, label in viewports:
            page.set_viewport_size({"width": width, "height": height})
            for theme in themes:
                page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)
                page.wait_for_timeout(50)  # let the theme's CSS custom properties repaint
                out_path = SHOTS_DIR / f"month-refresh-t12-{label}-{theme}.png"
                page.screenshot(path=str(out_path))
                assert out_path.is_file() and out_path.stat().st_size > 0, f"T12: screenshot not written: {out_path}"
                written.append(out_path)

    assert not errors, f"T12: expected zero console errors across all screenshot states, got {errors}"
    assert len(written) == 4, f"T12: expected 4 screenshots, wrote {len(written)}"
