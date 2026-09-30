"""Excel export with openpyxl: one .xlsx per category plus 00_Master_Summary.
Files are written atomically (tmp + replace) so a download during a checkpoint
never sees a half-written file."""

from __future__ import annotations

import logging
import os
import re
import threading
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .categories import SUMMARY_FILE
from .normalize import xlsx_safe

log = logging.getLogger("exporter")

COLUMNS = [
    "Company Name", "Category", "Subcategory", "Official Website",
    "Business Email", "Business Phone", "Country", "State", "City",
    "Full Business Address", "Industry", "Services", "Source URL",
    "Source Page", "Search Query", "Collected At", "Confidence Score",
]

HEADER_FILL = PatternFill("solid", fgColor="1F4E5F")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _style_sheet(ws, n_cols: int):
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        width = 12
        for row in ws.iter_rows(min_col=c, max_col=c, max_row=min(ws.max_row, 300)):
            v = row[0].value
            if v is not None:
                width = max(width, min(55, len(str(v)) + 2))
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.freeze_panes = "A2"


_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _file_locks_guard:
        return _file_locks.setdefault(os.path.normcase(os.path.abspath(path)),
                                      threading.Lock())


def _save(wb: Workbook, path: str):
    """Atomic replace. Several jobs may export at once (the master summary
    is written by every one of them): writers of the same file are
    serialized and every temp file is unique, so they never interleave."""
    with _lock_for(path):
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.xlsx"
        wb.save(tmp)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: open in Excel (retrying won't help) or held for a
                # moment by an antivirus scan / a download (it will)
                if attempt == 2:
                    # keep ONE up-to-date copy next to it (not one per export)
                    fallback = path + ".tmp.xlsx"
                    try:
                        os.replace(tmp, fallback)
                    except OSError:
                        fallback = tmp
                    log.warning("%s is open in another program; left %s", path, fallback)
                    return
                time.sleep(0.3 * (attempt + 1))


def write_category_file(display: str, fname: str, records: list[dict],
                        out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\[\]:*?/\\]", "-", display)[:31]
    ws.append(COLUMNS)
    for r in records:
        ws.append([xlsx_safe(r.get(col, "")) for col in COLUMNS])
    _style_sheet(ws, len(COLUMNS))
    _save(wb, os.path.join(out_dir, fname))
    return fname


SUMMARY_COLS = [
    "Category", "Records Found", "Unique Records", "Records With Website",
    "Records With Email", "Records With Phone", "Records With Address",
    "Duplicates Removed", "Failed URLs",
]


def write_master_summary(categories: dict[str, dict],
                         records_by_cat: dict[str, list[dict]],
                         stats_by_cat: dict[str, dict], out_dir: str) -> str:
    """categories: ordered slug -> {"display", "file"} (fixed + custom)."""
    os.makedirs(out_dir, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(SUMMARY_COLS)
    totals = {c: 0 for c in SUMMARY_COLS[1:]}
    for cat, cdef in categories.items():
        rows = records_by_cat.get(cat, [])
        st = stats_by_cat.get(cat, {})
        vals = {
            "Records Found": st.get("discovered", len(rows)),
            "Unique Records": len(rows),
            "Records With Website": sum(1 for r in rows if r.get("Official Website")),
            "Records With Email": sum(1 for r in rows if r.get("Business Email")),
            "Records With Phone": sum(1 for r in rows if r.get("Business Phone")),
            "Records With Address": sum(1 for r in rows if r.get("Full Business Address")),
            "Duplicates Removed": st.get("duplicates", 0),
            "Failed URLs": st.get("failed_urls", 0),
        }
        ws.append([cdef["display"]] + [vals[c] for c in SUMMARY_COLS[1:]])
        for k, v in vals.items():
            totals[k] += v
    ws.append(["TOTAL"] + [totals[c] for c in SUMMARY_COLS[1:]])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    _style_sheet(ws, len(SUMMARY_COLS))
    _save(wb, os.path.join(out_dir, SUMMARY_FILE))
    return SUMMARY_FILE
