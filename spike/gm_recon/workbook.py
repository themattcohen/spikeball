"""openpyxl writer for the monthly gross-margin reconciliation workbook.

openpyxl is imported inside `write_workbook()` only: the cloud sandbox did not ship it
until requirements.txt pinned it, and a module-level import crashed the nightly once
(see spike/routine/sandbox_import_check.py).

Layout mirrors the full-year review's Summary: Arial throughout; blue font for loaded
values, black for same-sheet formulas, green for cross-sheet formulas; light blue
section bands; grey total rows. Every Summary figure is a formula (SUMIFS or COUNTIFS
over detail rows, never SUMIF or COUNTIF) so the controller can audit it. A hidden
`_checks` sheet sets each Summary figure beside the Python value computed in
compute.py, with the difference.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import rules  # noqa: E402

SHEETS = ["Summary", "Entries", "Settlements", "Fee timing", "Manual journals", "Late created", "Wengo",
          "Retailer claims", "Refunds", "Duplicates", "PnL", "Notes"]
FONT = "Arial"
BLUE, GREEN, BLACK, WHITE = "0000FF", "008000", "000000", "FFFFFF"
MONEY = '#,##0.00;(#,##0.00);"-"'
WHOLE = '#,##0;(#,##0);"-"'
PCT1 = "0.0%"
PTS = '0.0" pts";-0.0" pts";"-"'
DATEF = "m/d/yyyy"
MAX_SUMMARY_TEXT = 240


def _trim(s, n=MAX_SUMMARY_TEXT):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def _q(v: str) -> str:
    return '"' + str(v).replace('"', '""') + '"'


class _Styles:
    def __init__(self, openpyxl):
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        self.Font, self.Alignment = Font, Alignment
        self.hdr = PatternFill("solid", fgColor="1F3864")
        self.hdr2 = PatternFill("solid", fgColor="2F5597")
        self.band = PatternFill("solid", fgColor="D9E2F3")
        self.sub = PatternFill("solid", fgColor="F2F2F2")
        thin = Side(style="thin", color="BFBFBF")
        self.border = Border(bottom=thin)
        self.wrap = Alignment(wrap_text=True, vertical="top")

    def font(self, color=BLACK, bold=False, italic=False, size=10):
        return self.Font(name=FONT, size=size, bold=bold, italic=italic, color=color)


class Table:
    """A rectangular block of rows written on a detail sheet. Remembers where each
    column sits so Summary formulas can reference bounded ranges."""

    def __init__(self, sheet, header_row, cols, n_rows):
        self.sheet = sheet
        self.header_row = header_row
        self.first = header_row + 1
        self.last = header_row + max(n_rows, 1)
        self.col = {}
        from openpyxl.utils import get_column_letter
        for i, (key, _h, _k) in enumerate(cols, start=1):
            self.col[key] = get_column_letter(i)

    def rng(self, key):
        c = self.col[key]
        return f"'{self.sheet}'!${c}${self.first}:${c}${self.last}"

    def sumifs(self, key, *crit):
        parts = [self.rng(key)]
        for ck, val in crit:
            parts += [self.rng(ck), _q(val)]
        return f"SUMIFS({','.join(parts)})"

    def countifs(self, *crit):
        parts = []
        for ck, val in crit:
            parts += [self.rng(ck), _q(val)]
        return f"COUNTIFS({','.join(parts)})"


def _write_table(ws, st, row, title, cols, rows):
    """cols: [(key, header, kind)] with kind in money, whole, date, text, int, num, pct.
    Writes a title band, a header row and the data rows. Returns (Table, next_row)."""
    ws.cell(row=row, column=1, value=title).font = st.font(bold=True, size=11)
    for c in range(1, len(cols) + 1):
        ws.cell(row=row, column=c).fill = st.band
    row += 1
    for i, (_k, h, _kind) in enumerate(cols, start=1):
        cell = ws.cell(row=row, column=i, value=h)
        cell.font = st.font(WHITE, bold=True)
        cell.fill = st.hdr2
        cell.alignment = st.Alignment(wrap_text=True, vertical="center")
    tbl = Table(ws.title, row, cols, len(rows))
    if not rows:
        ws.cell(row=row + 1, column=1, value="None").font = st.font(italic=True)
    for r_i, r in enumerate(rows, start=1):
        for c_i, (key, _h, kind) in enumerate(cols, start=1):
            v = r.get(key)
            if isinstance(v, (list, dict, set)):
                v = ", ".join(str(x) for x in v)
            cell = ws.cell(row=row + r_i, column=c_i, value=v)
            cell.font = st.font(BLUE)
            if kind == "money":
                cell.number_format = MONEY
            elif kind == "whole":
                cell.number_format = WHOLE
            elif kind == "date":
                cell.number_format = DATEF
            elif kind == "pct":
                cell.number_format = "0.00%"
            elif kind == "num":
                cell.number_format = "0.0000"
    return tbl, row + max(len(rows), 1) + 2


def _widths(ws, widths):
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def write_workbook(model: dict, details: dict, path: Path, cfg: dict) -> Path:
    import openpyxl  # lazy: see module docstring

    st = _Styles(openpyxl)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = {name: wb.create_sheet(name) for name in SHEETS}
    month = model["month"]
    asof = date.fromisoformat(model["asof"])
    S, E = rules.month_bounds(month)
    lab = rules.month_label(month)
    T: dict[str, Table] = {}

    # ---------------- detail sheets ----------------
    w = ws["Settlements"]
    T["setl"], r = _write_table(w, st, 1, f"Settlements deposited {rules.fmt_md(S - timedelta(days=cfg['fee_timing_window_days_before']))} to {rules.fmt_md(asof)} (fees in USD at fixed rates)", [
        ("sid", "Summary id", "text"), ("settl_id", "Settlement id", "text"), ("marketplace", "Marketplace", "text"),
        ("currency", "Currency", "text"), ("deposit", "Deposit date", "date"), ("deposit_amount", "Deposit amount (local)", "money"),
        ("rows_fee_local", "Settlement-row fees (local)", "money"), ("fx", "Fixed rate", "num"),
        ("rows_fee_usd", "Settlement-row fees USD", "money"), ("journal", "Journal", "text"), ("journal_date", "Journal date", "date"),
        ("booked_month", "Booked month", "text"), ("map_rule", "How matched", "text"), ("feeall", "Fee lines, all accounts", "money"),
        ("fee5", "Fee lines on COGS (5xxx)", "money"), ("fee_outside", "Fee lines outside COGS", "money"),
        ("variance", "Variance line", "money"), ("bank", "Bank deposit line", "money"), ("status", "Status", "text"),
        ("unbooked", "Unbooked (rows minus fee lines)", "money"), ("error_id", "Error id", "text"),
        ("in_month", f"Booked in {lab}", "text")], details["settlements"])
    T["feeout"], r = _write_table(w, st, r, "Amazon fee lines posted outside COGS (settlement journals dated in the month)", [
        ("journal", "Journal", "text"), ("date", "Date", "date"), ("account", "Account", "text"),
        ("account_name", "Account name", "text"), ("account_type", "Account type", "text"), ("amount", "Amount", "money"),
        ("treatment", "Treatment", "text"), ("error_id", "Error id", "text")], details["fee_lines_outside"])
    T["unmapped"], r = _write_table(w, st, r, "Settlement journals dated in the month with no settlement match", [
        ("journal", "Journal", "text"), ("date", "Date", "date"), ("bank", "Bank deposit line", "money"),
        ("feeall", "Fee lines, all accounts", "money"), ("review_id", "Review id", "text")], details.get("unmapped_journals", []))
    _widths(w, [11, 14, 10, 9, 11, 14, 14, 9, 14, 10, 11, 10, 34, 14, 14, 14, 14, 14, 11, 16, 9, 10])
    w.freeze_panes = "A3"

    w = ws["Fee timing"]
    T["ft"], r = _write_table(w, st, 1, "Settlement fees by settlement and sales month (order and refund rows, USD)", [
        ("sid", "Summary id", "text"), ("settl_id", "Settlement id", "text"), ("marketplace", "Marketplace", "text"),
        ("deposit", "Deposit date", "date"), ("booked_month", "Booked month", "text"), ("sales_month", "Sales month", "text"),
        ("source", "Sales-month source", "text"), ("fee_usd", "Fees USD", "money"),
        ("booked_in_month", f"Booked in {lab}", "text"), ("belongs_to_month", f"Belongs to {lab}", "text"),
        ("bucket", "Bucket", "text")], details["fee_timing"])
    T["est"], r = _write_table(w, st, r, f"Estimate for {lab} orders with no settlement fee row as of {rules.fmt_md(asof)}", [
        ("marketplace", "Marketplace", "text"), ("orders_in_month", "Orders in month (cache)", "whole"),
        ("orders_settled", "Orders with a fee row", "whole"), ("orders_unsettled", "Orders with no fee row", "whole"),
        ("median_fee", "Median fee per settled order", "money"), ("estimate", "Estimate", "money")], details["fee_estimate"])
    T["acc"], r = _write_table(w, st, r, "Amazon fee accrual journals (accrued-liability line, COGS line, Amazon memo)", [
        ("journal", "Journal", "text"), ("date", "Date", "date"), ("memo", "Memo", "text"), ("cogs_net", "COGS net (debit +)", "money"),
        ("reversal_partner", "Reversal partner", "text"), ("is_reversal", "Is reversal", "text"),
        ("in_month", f"Dated in {lab}", "text"), ("review_id", "Review id", "text")], details["accrual_journals"])
    _widths(w, [11, 14, 10, 11, 10, 10, 15, 13, 10, 10, 34])
    w.freeze_panes = "A3"

    w = ws["Manual journals"]
    T["mj"], r = _write_table(w, st, 1, f"Manual journals dated in {lab} with an income or COGS line (all posting lines)", [
        ("journal", "Journal", "text"), ("date", "Date", "date"), ("created_by", "Created by", "text"), ("memo", "Memo", "text"),
        ("account", "Account", "text"), ("account_name", "Account name", "text"), ("type", "Type", "text"),
        ("debit", "Debit", "money"), ("credit", "Credit", "money"), ("side", "Side", "text"), ("class", "Class", "text"),
        ("reversal_partner", "Reversal partner", "text"), ("review_id", "Review id", "text")], details["manual_journals"])
    _widths(w, [10, 11, 18, 40, 11, 34, 12, 13, 13, 11, 24, 12, 9])
    w.freeze_panes = "A3"

    w = ws["Late created"]
    T["late"], r = _write_table(w, st, 1, f"Entries posting to {lab} created after {rules.fmt_md(E)} (income credit +, COGS debit +)", [
        ("tranid", "Document", "text"), ("type", "Type", "text"), ("date", "Date", "date"), ("created", "Created", "date"),
        ("created_by", "Created by", "text"), ("income", "Income", "money"), ("cogs", "COGS", "money")], details["late_created"])
    _widths(w, [18, 10, 11, 11, 20, 14, 14])
    w.freeze_panes = "A3"

    w = ws["Wengo"]
    T["wg"], r = _write_table(w, st, 1, f"Wengo bills dated in {lab}", [
        ("bill", "Bill", "text"), ("type", "Type", "text"), ("date", "Date", "date"), ("created", "Created", "date"),
        ("status", "Status", "text"), ("memo", "Memo", "text"), ("pos", "POs named", "text"), ("po_months", "PO months", "text"),
        ("posted_month", "Posted month", "text"), ("posted_cogs", "Posted to COGS", "money"),
        ("belongs_month", "Belongs to", "text"), ("row_id", "Row id", "text")], details["wengo"])
    T["wgu"], r = _write_table(w, st, r, f"River Joint POs dated in {lab} with fee-bearing units and no Wengo bill as of {rules.fmt_md(asof)}", [
        ("po", "PO", "text"), ("date", "PO date", "date"), ("titan_units", "Titan units", "whole"), ("pong_units", "Pong units", "whole"),
        ("titan_rate", "Titan rate", "money"), ("pong_rate", "Pong rate", "money"), ("rate_basis", "Rate basis", "text"),
        ("estimate", "Estimated fee", "money"), ("row_id", "Row id", "text")], details["wengo_unbilled"])
    _widths(w, [12, 9, 11, 11, 14, 24, 16, 12, 11, 14, 12, 8])

    w = ws["Retailer claims"]
    T["rt"], r = _write_table(w, st, 1, f"Vendor bill and credit lines on income accounts, posted in {lab} (deduction debit +)", [
        ("bill", "Bill", "text"), ("type", "Type", "text"), ("vendor", "Vendor", "text"), ("date", "Date", "date"),
        ("created", "Created", "date"), ("account", "Account", "text"), ("memo", "Memo", "text"), ("deduction", "Deduction", "money"),
        ("periods", "Program period", "text"), ("belongs_in_month", f"Belongs to {lab}", "money"),
        ("moved", "Belongs elsewhere", "money"), ("row_id", "Row id", "text")], details["retailer"])
    _widths(w, [18, 9, 22, 11, 11, 11, 30, 13, 20, 13, 13, 9])

    w = ws["Refunds"]
    tot = details["refund_totals"]
    T["rfs"], r = _write_table(w, st, 1, "Refund totals (USD at fixed rates; income credit +)", [
        ("label", "Measure", "text"), ("value", "Amount", "money")], [
        {"label": f"Amazon credit memos dated in {lab}: income", "value": tot["cm_income"]},
        {"label": "Refund principal on settlement rows posted in the month", "value": tot["rows_posted_in_month"]},
        {"label": "Difference", "value": tot["difference"]}])
    T["rf"], r = _write_table(w, st, r, f"Legacy credit memos dated in {lab} and the posted date of their refund row", [
        ("credit_memo", "Credit memo", "text"), ("order_id", "Order id", "text"), ("date", "Date", "date"),
        ("income", "Income", "money"), ("refund_posted", "Refund posted", "date"), ("posted_month", "Posted month", "text"),
        ("other_month", "Other month", "text")], details["refunds"])
    _widths(w, [52, 22, 11, 13, 13, 20, 10])

    w = ws["Duplicates"]
    T["dup"], r = _write_table(w, st, 1, f"Amazon documents dated in {lab} that share an order id or daily reference", [
        ("kind", "Kind", "text"), ("key", "Order or reference", "text"), ("document", "Document", "text"), ("date", "Date", "date"),
        ("income", "Income", "money"), ("cogs", "COGS", "money"), ("extra", "Extra copy", "text"), ("error_id", "Row id", "text")],
        details["duplicates"])
    _widths(w, [20, 26, 14, 11, 13, 13, 10, 9])

    w = ws["PnL"]
    T["pnl"], r = _write_table(w, st, 1, f"Income and COGS by account, period {lab}, dated through {rules.fmt_mdy(asof)} (income credit +)", [
        ("acct", "Account", "text"), ("name", "Name", "text"), ("type", "Type", "text"), ("amount", "Amount", "money")],
        details["pnl"])
    _widths(w, [12, 70, 9, 16])

    w = ws["Entries"]
    ent = details["entries"]
    T["ent"], r = _write_table(w, st, 1, "Proposed journal lines (key these in; nothing was posted)", [
        ("date", "Date", "date"), ("account", "Account", "text"), ("debit", "Debit", "money"), ("credit", "Credit", "money"),
        ("memo", "Memo", "text"), ("source", "Source row", "text")], ent)
    for rr in range(T["ent"].first, T["ent"].first + len(ent)):
        w.cell(row=rr, column=1).number_format = "yyyy-mm-dd"
        w.cell(row=rr, column=3).number_format = "#,##0.00"
        w.cell(row=rr, column=4).number_format = "#,##0.00"
    tr = T["ent"].last + 1
    w.cell(row=tr, column=1, value="Total").font = st.font(bold=True)
    for col in ("C", "D"):
        c = w[f"{col}{tr}"]
        c.value = f"=SUM({col}{T['ent'].first}:{col}{T['ent'].last})"
        c.font = st.font(BLACK, bold=True)
        c.number_format = "#,##0.00"
    for col in "ABCDEF":
        w[f"{col}{tr}"].fill = st.sub
    _widths(w, [11, 11, 14, 14, 48, 10])

    # ---------------- Summary ----------------
    s = ws["Summary"]
    _widths(s, [8, 44, 26, 15, 13, 46, 46, 13, 13])
    s.sheet_view.showGridLines = False
    cols = "ABCDEFGHI"

    def cell(addr, value, color=None, bold=False, fmt=None, italic=False, size=10, wrap=True):
        c = s[addr]
        if isinstance(value, str) and not value.startswith("="):
            value = _trim(value)
        c.value = value
        if color is None:
            if isinstance(value, str) and value.startswith("="):
                color = GREEN if "!" in value else BLACK
            elif isinstance(value, (int, float)):
                color = BLUE
            else:
                color = BLACK
        c.font = st.font(color, bold=bold, italic=italic, size=size)
        c.alignment = st.wrap if wrap else st.Alignment(vertical="top")
        if fmt:
            c.number_format = fmt
        return c

    def heading(row, text):
        for col in cols:
            s[f"{col}{row}"].fill = st.hdr
        c = s[f"A{row}"]
        c.value = text
        c.font = st.font(WHITE, bold=True, size=12)
        s.row_dimensions[row].height = 22

    def header(row, labels):
        for col, h in zip(cols, labels):
            c = s[f"{col}{row}"]
            c.value = h
            c.font = st.font(WHITE, bold=True)
            c.fill = st.hdr2
            c.alignment = st.Alignment(horizontal="center", vertical="center", wrap_text=True)
        s.row_dimensions[row].height = 32

    def band_row(row):
        for col in cols:
            s[f"{col}{row}"].fill = st.band

    def merged(row, value, height=30, italic=False):
        s.merge_cells(f"B{row}:I{row}")
        cell(f"B{row}", value, italic=italic)
        s.row_dimensions[row].height = height

    cell("A1", f"Spikeball gross margin reconciliation, {lab}, as of {rules.fmt_mdy(asof)}", bold=True, size=16, wrap=False)
    s.row_dimensions[1].height = 26

    # Lay out the margin table first (row numbers are needed by the Answer formulas).
    n_err, n_tim, n_rev = len(model["errors"]), len(model["timing"]), len(model["review"])
    answer_rows = 8
    r_ans = 3
    r_mh = r_ans + answer_rows + 1          # margin heading
    r_mhdr = r_mh + 1
    r_inc, r_cogs, r_gm, r_basis = r_mhdr + 1, r_mhdr + 2, r_mhdr + 3, r_mhdr + 4
    r_eh = r_basis + 2
    r_ehdr = r_eh + 1
    e_first = r_ehdr + 1
    e_last = e_first + max(n_err, 1) - 1
    r_eh_tot = e_last + 1
    r_th = r_eh_tot + 2
    r_thdr = r_th + 1
    t_first = r_thdr + 1
    t_last = t_first + max(n_tim, 1) - 1
    r_th_tot = t_last + 1
    r_rh = r_th_tot + 2
    r_rhdr = r_rh + 1
    v_first = r_rhdr + 1
    v_last = v_first + max(n_rev, 1) - 1
    r_fh = v_last + 2
    f_first = r_fh + 1
    f_last = f_first + max(len(model["fine"]), 1) - 1
    r_lh = f_last + 2
    l_first = r_lh + 1

    # Margin table
    heading(r_mh, "Margin")
    header(r_mhdr, ["", "Line", "As booked", "Corrected for errors", "Matched to period", "Basis", "", "", ""])
    pnl = T["pnl"]
    cell(f"B{r_inc}", "Income")
    cell(f"C{r_inc}", "=" + pnl.sumifs("amount", ("type", "Income")), fmt=MONEY)
    cell(f"D{r_inc}", f"=C{r_inc}+SUM(H{e_first}:H{e_last})", fmt=MONEY)
    cell(f"E{r_inc}", f"=D{r_inc}+SUM(H{t_first}:H{t_last})", fmt=MONEY)
    cell(f"B{r_cogs}", "COGS")
    cell(f"C{r_cogs}", "=" + pnl.sumifs("amount", ("type", "COGS")), fmt=MONEY)
    cell(f"D{r_cogs}", f"=C{r_cogs}+SUM(I{e_first}:I{e_last})", fmt=MONEY)
    cell(f"E{r_cogs}", f"=D{r_cogs}+SUM(I{t_first}:I{t_last})", fmt=MONEY)
    cell(f"B{r_gm}", "Gross margin percent", bold=True)
    for col in "CDE":
        cell(f"{col}{r_gm}", f"=IF({col}{r_inc}=0,0,({col}{r_inc}-{col}{r_cogs})/{col}{r_inc})", fmt=PCT1, bold=True)
    for col in cols:
        s[f"{col}{r_gm}"].fill = st.sub
    basis_txt = {"measured+estimate": "Matched includes measured fees and an estimate for fees not yet settled.",
                 "measured": "Matched is measured only.",
                 "measured, excludes fees not yet settled": "Matched excludes fees not yet settled (orders cache unavailable)."}
    cell(f"F{r_inc}", "Ledger, posting period, all subsidiaries.")
    cell(f"F{r_cogs}", "Corrected adds the errors below; matched adds the month-end entries.")
    merged(r_basis, basis_txt.get(model["margin"]["matched_basis"], model["margin"]["matched_basis"]), 18, italic=True)

    # Errors table
    heading(r_eh, "Errors to correct")
    header(r_ehdr, ["#", "What", "Records", "Amount", "Change (points)", "Entry to make", "Why", "Income effect", "COGS effect"])
    inc0, cogs0 = f"$C${r_inc}", f"$C${r_cogs}"
    if not model["errors"]:
        cell(f"B{e_first}", "None found", italic=True)
        for col in "DEHI":
            cell(f"{col}{e_first}", "=0", fmt=MONEY)
    for i, e in enumerate(model["errors"]):
        rr = e_first + i
        cell(f"A{rr}", e["id"])
        cell(f"B{rr}", e["what"])
        cell(f"C{rr}", e["records"])
        cell(f"F{rr}", e["entry"])
        cell(f"G{rr}", e["why"])
        drv = e["driver"]
        if drv == "E1":
            amt = "=" + T["setl"].sumifs("unbooked", ("error_id", e["id"]))
            inc_f, cogs_f = "=0", f"=D{rr}"
        elif drv == "E4":
            amt = "=" + T["feeout"].sumifs("amount", ("error_id", e["id"]))
            inc_f = "=" + T["feeout"].sumifs("amount", ("error_id", e["id"]), ("account_type", "Income"))
            cogs_f = f"=D{rr}"
        else:  # D1
            inc_f = "=-" + T["dup"].sumifs("income", ("error_id", e["id"]), ("extra", "Y"))
            cogs_f = "=-" + T["dup"].sumifs("cogs", ("error_id", e["id"]), ("extra", "Y"))
            amt = f"=IF(ABS(H{rr})>=0.005,ABS(H{rr}),ABS(I{rr}))"
        cell(f"D{rr}", amt, fmt=MONEY)
        cell(f"H{rr}", inc_f, fmt=MONEY)
        cell(f"I{rr}", cogs_f, fmt=MONEY)
        cell(f"E{rr}", f"=IF({inc0}+H{rr}=0,0,((({inc0}+H{rr})-({cogs0}+I{rr}))/({inc0}+H{rr})-({inc0}-{cogs0})/{inc0})*100)",
             fmt=PTS)
        s.row_dimensions[rr].height = 64
    cell(f"B{r_eh_tot}", "Total", bold=True)
    for col in "HI":
        cell(f"{col}{r_eh_tot}", f"=SUM({col}{e_first}:{col}{e_last})", fmt=MONEY, bold=True)
    cell(f"E{r_eh_tot}", f"=(D{r_gm}-C{r_gm})*100", fmt=PTS, bold=True)
    for col in cols:
        s[f"{col}{r_eh_tot}"].fill = st.sub

    # Timing table
    heading(r_th, "Entries to book at month end (timing)")
    header(r_thdr, ["#", "What", "Booked in month", "Belongs to month", "Net", "Basis", "Entry to make", "Income effect", "COGS effect"])
    if not model["timing"]:
        cell(f"B{t_first}", "None found", italic=True)
        for col in "CDEHI":
            cell(f"{col}{t_first}", "=0", fmt=MONEY)
    for i, t in enumerate(model["timing"]):
        rr = t_first + i
        cell(f"A{rr}", t["id"])
        cell(f"B{rr}", t["what"])
        cell(f"F{rr}", t["basis"].replace("+", " plus "))
        cell(f"G{rr}", t["entry"])
        if t["driver"] == "T1":
            booked = ("=" + T["ft"].sumifs("fee_usd", ("booked_in_month", "Y")) + "-"
                      + T["feeout"].sumifs("amount", ("treatment", "accepted, not reclassed")) + "+"
                      + T["acc"].sumifs("cogs_net", ("in_month", "Y")))
            belongs = ("=" + T["ft"].sumifs("fee_usd", ("belongs_to_month", "Y")) + "+SUM(" + T["est"].rng("estimate") + ")")
            inc_f, cogs_f = "=0", f"=E{rr}"
        elif t["driver"] == "T3" and t["basis"] == "estimate":
            booked = "=0"
            belongs = "=" + T["wgu"].sumifs("estimate", ("row_id", t["id"]))
            inc_f, cogs_f = "=0", f"=E{rr}"
        elif t["driver"] == "T3":
            booked = "=" + T["wg"].sumifs("posted_cogs", ("row_id", t["id"]))
            belongs = "=0"
            inc_f, cogs_f = "=0", f"=E{rr}"
        else:  # T4
            booked = "=" + T["rt"].sumifs("deduction", ("row_id", t["id"]))
            belongs = "=" + T["rt"].sumifs("belongs_in_month", ("row_id", t["id"]))
            inc_f, cogs_f = f"=-E{rr}", "=0"
        cell(f"C{rr}", booked, fmt=MONEY)
        cell(f"D{rr}", belongs, fmt=MONEY)
        cell(f"E{rr}", f"=D{rr}-C{rr}", fmt=MONEY)
        cell(f"H{rr}", inc_f, fmt=MONEY)
        cell(f"I{rr}", cogs_f, fmt=MONEY)
        s.row_dimensions[rr].height = 76
    cell(f"B{r_th_tot}", "Total", bold=True)
    for col in "HI":
        cell(f"{col}{r_th_tot}", f"=SUM({col}{t_first}:{col}{t_last})", fmt=MONEY, bold=True)
    cell(f"E{r_th_tot}", f"=(E{r_gm}-D{r_gm})*100", fmt=PTS, bold=True)
    cell(f"F{r_th_tot}", "Change in margin points from the month-end entries", italic=True)
    for col in cols:
        s[f"{col}{r_th_tot}"].fill = st.sub

    # Review table
    heading(r_rh, "For controller review")
    header(r_rhdr, ["#", "What", "Records", "Amount", "", "Question", "", "", ""])
    if not model["review"]:
        cell(f"B{v_first}", "Nothing needs a ruling.", italic=True)
    for i, v in enumerate(model["review"]):
        rr = v_first + i
        cell(f"A{rr}", v["id"])
        cell(f"B{rr}", v["what"])
        cell(f"C{rr}", v["records"])
        s.merge_cells(f"F{rr}:I{rr}")
        cell(f"F{rr}", v["note"])
        cell(f"D{rr}", _review_amount_formula(v, T), fmt=MONEY)
        s.row_dimensions[rr].height = 48

    # Checked and fine
    heading(r_fh, "Checked and fine")
    if not model["fine"]:
        merged(f_first, "Nothing in this list for the month.")
    for i, f_ in enumerate(model["fine"]):
        merged(f_first + i, f_, 30)

    # Limitations
    heading(r_lh, "Limitations and data notes")
    lims = list(model["limitations"])
    for i, l_ in enumerate(lims):
        merged(l_first + i, l_, 30)
    r_hh = l_first + len(lims) + 1
    heading(r_hh, "How to read the rest")
    guide = [
        ("Entries", "Every proposed journal line: date, account, debit, credit, memo and the Summary row it comes from."),
        ("Settlements", "One row per Amazon settlement: fees on the settlement rows, the journal that booked it, its fee lines and status."),
        ("Fee timing", "Settlement fees by sales month, the estimate for fees not yet settled, and any fee accrual journals."),
        ("Manual journals", "Every line of manual journals in the month that touch income or COGS, with the class and review id."),
        ("Late created", "Entries posting to the month that were created after month end, through the as-of date."),
        ("Wengo", "Wengo bills dated in the month with their PO month, and POs in the month not yet billed."),
        ("Retailer claims", "Vendor bill lines on income accounts with the program period read from the memo."),
        ("Refunds", "Amazon credit memos in the month against refund rows Amazon posted in the month."),
        ("Duplicates", "Amazon invoices or credit memos that share an order id or daily reference."),
        ("PnL", "Income and COGS by account for the period as booked, the base of the margin table."),
        ("Notes", "Fixed rates, method notes, the completeness check and run details."),
    ]
    for i, (sh, txt) in enumerate(guide, start=1):
        cell(f"A{r_hh + i}", sh, bold=True, wrap=False)
        s.merge_cells(f"B{r_hh + i}:I{r_hh + i}")
        cell(f"B{r_hh + i}", txt)

    # Answer (formulas over the margin table and the section tables)
    heading(2, "Answer")
    ans = _answer_formulas(model, lab, asof, E, r_inc, r_cogs, r_gm, e_first, e_last, v_first, v_last, f_first, f_last,
                           n_err, n_rev)
    for i in range(answer_rows):
        rr = r_ans + i
        if i < len(ans):
            merged(rr, ans[i], 20)
        else:
            s.row_dimensions[rr].height = 4

    # ---------------- Notes ----------------
    n = ws["Notes"]
    _widths(n, [34, 90])
    rows = [("Month", lab), ("As of", rules.fmt_mdy(asof)), ("Run at (MT)", model.get("run_at_mt", "")),
            ("Code revision", model.get("code_rev", "")), ("Fee COGS account", model.get("fee_cogs_account", ""))]
    rows += [(f"Fixed rate {k}", v) for k, v in cfg["fx_to_usd"].items()]
    rows += [("Basis", "Ledger posting lines (transactionaccountingline, posting = T); income credit positive, COGS debit positive."),
             ("Settlement journals", f"Created by {cfg['settlement_journal_creator']}; matched to settlements by deposit date, "
                                     "then by amount within three days when a journal was re-dated."),
             ("Fee status", "full when fee lines are within 1 percent of the settlement-row fees; none when there are no fee "
                            "lines; partial otherwise; no_journal when no journal matches."),
             ("Sales month", "Legacy invoice date; else the Amazon purchase date in Mountain Time from the orders cache; "
                             "else the settlement row's posted date."),
             ("Retailer periods", "Quarter or month named in the memo; Q2-Q3 splits evenly; a bill created well before its "
                                  "date goes to the creation month; a bare earlier year goes to December of that year."),
             ("Read-only", "Nothing was changed in NetSuite. The Entries sheet lists proposed lines only.")]
    for i, (k, v) in enumerate(rows, start=1):
        a = n.cell(row=i, column=1, value=k)
        a.font = st.font(bold=True)
        b = n.cell(row=i, column=2, value=v)
        b.font = st.font(BLUE if isinstance(v, (int, float)) else BLACK)
        b.alignment = st.wrap
    rr = len(rows) + 2
    n.cell(row=rr, column=1, value="Limitations").font = st.font(bold=True, size=11)
    for i, l_ in enumerate(model["limitations"], start=1):
        c = n.cell(row=rr + i, column=2, value=l_)
        c.font = st.font()
        c.alignment = st.wrap
    rr += len(model["limitations"]) + 2
    _write_table(n, st, rr, "Amazon revenue completeness, mature purchase days (approximate)", [
        ("marketplace", "Marketplace", "text"), ("days", "Purchase days (UTC)", "text"), ("netsuite_usd", "NetSuite 40100000 USD", "money"),
        ("amazon_usd", "Amazon item price USD", "money"), ("ratio", "Ratio", "pct"), ("verdict", "Verdict", "text")],
        details.get("completeness", []))

    # ---------------- hidden checks ----------------
    ck = wb.create_sheet("_checks")
    ck.sheet_state = "hidden"
    for c_i, h in enumerate(["Check", "Python value", "Workbook formula", "Difference"], start=1):
        ck.cell(row=1, column=c_i, value=h).font = st.font(bold=True)
    checks = [("As booked income", model["margin"]["income"]["as_booked"], f"=Summary!C{r_inc}"),
              ("As booked COGS", model["margin"]["cogs"]["as_booked"], f"=Summary!C{r_cogs}"),
              ("Corrected income", model["margin"]["income"]["corrected"], f"=Summary!D{r_inc}"),
              ("Corrected COGS", model["margin"]["cogs"]["corrected"], f"=Summary!D{r_cogs}"),
              ("Matched income", model["margin"]["income"]["matched"], f"=Summary!E{r_inc}"),
              ("Matched COGS", model["margin"]["cogs"]["matched"], f"=Summary!E{r_cogs}")]
    # Gross margin compared in percentage points, so a 0.02 tolerance means 0.02 points.
    for label, key, col in (("As booked GM points", "as_booked", "C"), ("Corrected GM points", "corrected", "D"),
                            ("Matched GM points", "matched", "E")):
        gm = model["margin"][key]
        checks.append((label, round(gm * 100, 4) if gm is not None else 0.0, f"=Summary!{col}{r_gm}*100"))
    for i, e in enumerate(model["errors"]):
        checks.append((f"{e['id']} amount", e["amount"], f"=Summary!D{e_first + i}"))
    for i, t in enumerate(model["timing"]):
        checks.append((f"{t['id']} booked", t["booked_in_month"], f"=Summary!C{t_first + i}"))
        checks.append((f"{t['id']} belongs", t["belongs_to_month"], f"=Summary!D{t_first + i}"))
    for i, v in enumerate(model["review"]):
        checks.append((f"{v['id']} amount", v["amount"], f"=Summary!D{v_first + i}"))
    checks.append(("Entries debits minus credits", 0.0, f"=Entries!C{T['ent'].last + 1}-Entries!D{T['ent'].last + 1}"))
    for i, (label, pv, fml) in enumerate(checks, start=2):
        ck.cell(row=i, column=1, value=label).font = st.font()
        c = ck.cell(row=i, column=2, value=pv)
        c.font = st.font(BLUE)
        c.number_format = MONEY
        c = ck.cell(row=i, column=3, value=fml)
        c.font = st.font(GREEN)
        c.number_format = MONEY
        c = ck.cell(row=i, column=4, value=f"=ROUND(C{i}-B{i},2)")
        c.font = st.font()
        c.number_format = MONEY
    _widths(ck, [32, 16, 16, 12])

    # Fonts: every populated cell is Arial (cells created by fills only get the font too)
    from openpyxl.cell.cell import MergedCell
    for sh in wb.worksheets:
        for row in sh.iter_rows():
            for c in row:
                if isinstance(c, MergedCell):
                    continue
                if c.font is None or c.font.name != FONT:
                    c.font = st.font()
    wb.calculation.fullCalcOnLoad = True
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".xlsx.tmp")
    wb.save(tmp)
    tmp.replace(path)
    return path


def _review_amount_formula(v: dict, T: dict) -> str:
    rid, drv = v["id"], v["driver"]
    if drv == "R1":
        return ("=" + T["mj"].sumifs("debit", ("review_id", rid), ("side", "P&L")) + "-"
                + T["mj"].sumifs("credit", ("review_id", rid), ("side", "P&L")))
    if drv == "T2":
        return "=" + T["rf"].sumifs("income", ("other_month", "Y"))
    if drv == "D1":
        return "=ABS(" + T["dup"].sumifs("income", ("error_id", rid), ("extra", "Y")) + ")"
    if drv == "T3":
        return "=" + T["wg"].sumifs("posted_cogs", ("row_id", rid))
    if drv == "T4":
        return "=" + T["rt"].sumifs("deduction", ("row_id", rid))
    if drv == "T1":
        return "=" + T["acc"].sumifs("cogs_net", ("review_id", rid))
    if drv == "E1":
        return "=" + T["unmapped"].sumifs("feeall", ("review_id", rid))
    raise ValueError(f"no formula for review row {rid}")


def _answer_formulas(model, lab, asof, E, r_inc, r_cogs, r_gm, e_first, e_last, v_first, v_last, f_first, f_last,
                     n_err, n_rev) -> list[str]:
    """Answer sentences as formulas over the Summary tables, so every figure in the
    prose recomputes with the workbook."""
    out = [f'="{lab} as booked shows "&TEXT(C{r_gm},"0.0%")&" gross margin on income of "&TEXT(C{r_inc},"#,##0")'
           f'&" as of {rules.fmt_mdy(asof)}."']
    if n_err:
        out.append(f'=COUNTIFS(A{e_first}:A{e_last},"<>")&" booking errors total "&TEXT(SUMPRODUCT(ABS(D{e_first}:D{e_last})),"#,##0")'
                   f'&"; corrected, margin is "&TEXT(D{r_gm},"0.0%")&"."')
    else:
        out.append("No booking errors were found; corrected margin equals as booked.")
    basis = model["margin"]["matched_basis"].replace("+", " plus ")
    out.append(f'="Matched to the month the sales were earned, margin is "&TEXT(E{r_gm},"0.0%")&" ({basis})."')
    if abs(model["t1"]["accrual"]) >= 0.005:
        out.append(f'="The proposed Amazon fee accrual at {rules.fmt_md(E)} is "&TEXT(SUMIFS(Entries!$C:$C,Entries!$F:$F,"T1")/2,"#,##0")'
                   f'&", reversed on {rules.fmt_md(E + timedelta(days=1))}."')
    if n_rev:
        out.append(f'=COUNTIFS(A{v_first}:A{v_last},"<>")&" items need a controller ruling."')
    out.append(f'=COUNTIFS(B{f_first}:B{f_last},"<>")&" checks came back fine. Nothing was changed in NetSuite."')
    if model.get("limitation_headline"):
        out.append(_trim(model["limitation_headline"], 230))
    return out[:8]
