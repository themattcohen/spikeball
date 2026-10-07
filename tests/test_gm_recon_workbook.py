"""Workbook writer tests for the monthly gross-margin reconciliation
(spike/gm_recon/workbook.py). Builds the workbook from the compute fixture into
tmp_path, reopens it with openpyxl and checks the contract: sheet order, Summary
figures are formulas, SUMIFS/COUNTIFS only, fullCalcOnLoad, Arial everywhere, no
banned strings, Summary text at most 240 characters, no em dashes.

Run: python -m pytest tests/test_gm_recon_workbook.py -q
"""
import ast
import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

ROOT = Path(__file__).resolve().parents[1]
GM_RECON = ROOT / "spike" / "gm_recon"
for _p in (GM_RECON, Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import compute  # noqa: E402
import workbook  # noqa: E402
from test_gm_recon_compute import ASOF, CACHE_INFO, CFG, MONTH, make_cache, make_raw  # noqa: E402

EXPECTED_SHEETS = ["Summary", "Entries", "Settlements", "Fee timing", "Manual journals", "Late created", "Wengo",
                   "Retailer claims", "Refunds", "Duplicates", "PnL", "Notes", "_checks"]


@pytest.fixture
def wb_path(tmp_path):
    model, details = compute.build(MONTH, ASOF, make_raw(), CFG, make_cache(), CACHE_INFO,
                                   {"run_at_mt": "2026-10-07T08:00:00-06:00", "code_rev": "abc1234"})
    return workbook.write_workbook(model, details, tmp_path / "recon.xlsx", CFG), model


def _cells(wb):
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    yield ws, c


def test_sheet_order_and_hidden_checks(wb_path):
    path, _ = wb_path
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == EXPECTED_SHEETS
    assert wb["_checks"].sheet_state == "hidden"


def test_full_calc_on_load(wb_path):
    path, _ = wb_path
    wb = openpyxl.load_workbook(path)
    assert wb.calculation.fullCalcOnLoad is True


def test_summary_numbers_are_formulas(wb_path):
    path, _ = wb_path
    ws = openpyxl.load_workbook(path)["Summary"]
    numeric = [c for row in ws.iter_rows() for c in row if isinstance(c.value, (int, float))]
    assert numeric == [], f"loaded numbers on Summary: {[(c.coordinate, c.value) for c in numeric]}"
    formulas = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert len(formulas) > 20
    assert any("SUMIFS(" in c.value for c in formulas)


def test_no_sumif_or_countif(wb_path):
    path, _ = wb_path
    wb = openpyxl.load_workbook(path)
    for ws, c in _cells(wb):
        if isinstance(c.value, str):
            up = c.value.upper()
            assert "SUMIF(" not in up and "COUNTIF(" not in up, f"{ws.title}!{c.coordinate}"


def test_every_cell_is_arial(wb_path):
    path, _ = wb_path
    wb = openpyxl.load_workbook(path)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.value is None or type(c).__name__ == "MergedCell":
                    continue
                assert c.font.name == "Arial", f"{ws.title}!{c.coordinate} font {c.font.name}"


def test_summary_text_length_and_punctuation(wb_path):
    path, _ = wb_path
    wb = openpyxl.load_workbook(path)
    for ws, c in _cells(wb):
        if not isinstance(c.value, str):
            continue
        assert "\u2014" not in c.value, f"em dash at {ws.title}!{c.coordinate}"
        if ws.title == "Summary" and not c.value.startswith("="):
            assert len(c.value) <= 240, f"Summary!{c.coordinate} is {len(c.value)} chars"


def test_no_banned_strings_in_any_cell(wb_path):
    publish_bundle = pytest.importorskip("publish_bundle",
                                         reason="the banned-string list ships with the maintainer tooling only")
    path, _ = wb_path
    wb = openpyxl.load_workbook(path)
    for ws, c in _cells(wb):
        low = str(c.value).lower()
        for term in publish_bundle.BANNED_STRINGS:
            assert term.lower() not in low, f"{ws.title}!{c.coordinate} contains a banned term"


def test_summary_layout_mirrors_the_review(wb_path):
    path, model = wb_path
    ws = openpyxl.load_workbook(path)["Summary"]
    assert ws["A1"].value == "Spikeball gross margin reconciliation, September 2026, as of 10/7/2026"
    col_a = [ws.cell(row=r, column=1).value for r in range(1, ws.max_row + 1)]
    for heading in ("Answer", "Margin", "Errors to correct", "Entries to book at month end (timing)",
                    "For controller review", "Checked and fine", "Limitations and data notes", "How to read the rest"):
        assert heading in col_a, heading
    ids = [v for v in col_a if isinstance(v, str)]
    for e in model["errors"]:
        assert e["id"] in ids
    for t in model["timing"]:
        assert t["id"] in ids


def test_checks_sheet_pairs_python_values_with_formulas(wb_path):
    path, model = wb_path
    ck = openpyxl.load_workbook(path)["_checks"]
    labels = {ck.cell(row=r, column=1).value: (ck.cell(row=r, column=2).value, ck.cell(row=r, column=3).value)
              for r in range(2, ck.max_row + 1)}
    assert labels["As booked income"][0] == 100000.0
    assert labels["Matched COGS"][0] == model["margin"]["cogs"]["matched"]
    assert labels["Matched GM points"][0] == round(model["margin"]["matched"] * 100, 4)
    assert labels["T1 booked"][0] == next(t for t in model["timing"] if t["id"] == "T1")["booked_in_month"]
    assert all(str(v[1]).startswith("=") for v in labels.values())


def test_entries_sheet_rows(wb_path):
    path, _ = wb_path
    ws = openpyxl.load_workbook(path)["Entries"]
    header = [ws.cell(row=2, column=c).value for c in range(1, 7)]
    assert header == ["Date", "Account", "Debit", "Credit", "Memo", "Source row"]
    sources = {ws.cell(row=r, column=6).value for r in range(3, ws.max_row + 1)}
    assert {"E1-1", "E4-1", "T1", "T3-2"} <= sources
    last = max(r for r in range(3, ws.max_row + 1) if ws.cell(row=r, column=6).value)
    for r in range(3, last + 1):
        assert ws.cell(row=r, column=1).number_format == "yyyy-mm-dd"
        assert ws.cell(row=r, column=3).number_format == "#,##0.00"
        assert ws.cell(row=r, column=4).number_format == "#,##0.00"


def test_openpyxl_is_imported_lazily():
    tree = ast.parse((GM_RECON / "workbook.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name for n in top for a in n.names] + [n.module or "" for n in top if isinstance(n, ast.ImportFrom)]
    assert not any(x.startswith("openpyxl") for x in names)
