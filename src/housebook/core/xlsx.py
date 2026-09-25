"""XLSX → text/CSV helpers.

XLSX inputs are rarer than PDFs but every bit as fragile when dumped
into ad-hoc parsers (Brazilian tax reports, vendor account exports,
etc.). The import SOP routes XLSX through the same sidecar→ingest
flow as PDFs; this module gives the AI agent a deterministic way to
read the file.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


class XlsxError(Exception):
    """Raised when openpyxl isn't available or the file is bad."""


@dataclass
class Sheet:
    name: str
    rows: list[list[str]]   # cells stringified, blanks → ""


def read_xlsx(path: str) -> list[Sheet]:
    """Return every sheet as a list of string rows.

    Implementation uses `openpyxl` (already a dependency).
    """
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise XlsxError(
            "openpyxl is required to read XLSX files"
        ) from e

    wb = load_workbook(path, read_only=True, data_only=True)
    sheets: list[Sheet] = []
    for ws in wb.worksheets:
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else str(v) for v in row])
        sheets.append(Sheet(name=ws.title, rows=rows))
    wb.close()
    return sheets


def sheet_to_csv(sheet: Sheet) -> str:
    """Render a sheet as CSV text (RFC 4180)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for row in sheet.rows:
        w.writerow(row)
    return buf.getvalue()


def xlsx_to_csv_bundle(path: str) -> dict[str, str]:
    """Map sheet-name → CSV text for every sheet in the workbook."""
    return {s.name: sheet_to_csv(s) for s in read_xlsx(path)}


def xlsx_to_text(
    path: str, separator: str = " | ",
    sheet_separator: str = "\n\f\n",
) -> str:
    """Best-effort plain-text dump for AI consumption.

    Each sheet is emitted with a header line and pipe-separated rows;
    sheets are separated by form feeds so they're easy to chunk.
    """
    sheets: list[str] = []
    for sheet in read_xlsx(path):
        lines = [f"# Sheet: {sheet.name}"]
        lines.extend(separator.join(row) for row in sheet.rows)
        sheets.append("\n".join(lines))
    return sheet_separator.join(sheets)
