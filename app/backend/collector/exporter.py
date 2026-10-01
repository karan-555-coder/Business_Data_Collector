"""Excel export with openpyxl: one .xlsx per category plus 00_Master_Summary.
Files are written atomically (tmp + replace) so a download during a checkpoint
never sees a half-written file, and only when their content changed: openpyxl
needs ~0.5 s of CPU for a 2,000-row sheet (~5 s of wall time on a 0.1-CPU
host), and every rewrite makes each open browser download the file again."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .. import fastjson
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


def _save_locked(wb: Workbook, path: str) -> bool:
    """Atomic replace; True once `path` holds the new workbook. Several jobs
    may export at once (the master summary is written by every one of
    them): the caller holds the file's lock, so writers of the same file
    are serialized, and every temp file is unique, so they never interleave."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.xlsx"
    wb.save(tmp)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return True
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
                return False
            time.sleep(0.3 * (attempt + 1))
    return False


# path -> (content digest, mtime_ns, size) of the file this process last
# wrote there. A file changed or removed by anything else is rewritten.
_written: dict[str, tuple[bytes, int, int]] = {}


def _digest(*parts) -> bytes:
    return hashlib.blake2b(fastjson.dumps(parts), digest_size=16).digest()


def _write_if_changed(path: str, digest: bytes, build, force: bool = False) -> bool:
    """Save build()'s workbook to `path` unless the file there was written
    from identical content. Returns True if the file was (re)written."""
    with _lock_for(path):
        prev = _written.get(path)
        if not force and prev is not None and prev[0] == digest:
            try:
                st = os.stat(path)
                if (st.st_mtime_ns, st.st_size) == prev[1:]:
                    return False
            except OSError:
                pass
        _written.pop(path, None)
        if _save_locked(build(), path):
            try:
                st = os.stat(path)
                _written[path] = (digest, st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        return True


def write_category_file(display: str, fname: str, records: list[dict],
                        out_dir: str, force: bool = False) -> str:
    """One category's workbook. Skipped when its rows are unchanged since
    this process last wrote the file (unless force)."""
    os.makedirs(out_dir, exist_ok=True)
    title = re.sub(r"[\[\]:*?/\\]", "-", display)[:31]
    rows = [[xlsx_safe(r.get(col, "")) for col in COLUMNS] for r in records]

    def build() -> Workbook:
        wb = Workbook()
        ws = wb.active
        ws.title = title
        ws.append(COLUMNS)
        for row in rows:
            ws.append(row)
        _style_sheet(ws, len(COLUMNS))
        return wb
    _write_if_changed(os.path.join(out_dir, fname), _digest(title, rows), build, force)
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
    table = []
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
        table.append([cdef["display"]] + [vals[c] for c in SUMMARY_COLS[1:]])
        for k, v in vals.items():
            totals[k] += v
    table.append(["TOTAL"] + [totals[c] for c in SUMMARY_COLS[1:]])

    def build() -> Workbook:
        wb = Workbook()
        ws = wb.active
        ws.title = "Summary"
        ws.append(SUMMARY_COLS)
        for row in table:
            ws.append(row)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
        _style_sheet(ws, len(SUMMARY_COLS))
        return wb
    _write_if_changed(os.path.join(out_dir, SUMMARY_FILE), _digest(table), build)
    return SUMMARY_FILE
