"""Structured XLSX extraction via openpyxl — v2.

Preserves the workbook/worksheet structure instead of flattening it:

    metadata        core workbook properties (creator, dates, title, ...)
    content
        sheets      in workbook order, each with its name, dimensions, rows of
                    cells (coordinate, value, data type, formula), merged-cell
                    ranges and defined table references

The structure ``workbook → worksheet → rows → cells`` is preserved exactly.
Cell content is never concatenated into one string.

* **Formulas** are preserved: a formula cell keeps ``formula`` (the ``=...``
  text) and the last calculated value in ``value``. openpyxl loads the file
  with ``data_only=False`` so the formula string is what ``cell.value`` holds.
* **Data types** are carried through (``s`` string, ``n`` number, ``b`` bool,
  ``f`` formula, ``d`` date, ``e`` error, ...).
* **Dates / times / Decimal values** are JSON-serialized via ``json_safe``.
* **Merged ranges and named tables** are recorded where present.
* Embedded images are **not** captured — openpyxl does not expose them through
  a stable public API, so the raw file remains the source of record.

The extractor is pure: it reads an XLSX file and returns a machine-readable
dict; all filesystem/output orchestration lives in the orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from extractors._v2common import document_identity, json_safe

METHOD = "openpyxl"
VERSION = "2.0.0"
FORMAT = "xlsx"
MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class XlsxExtractorV2:
    """V2 semantic XLSX extractor: workbook metadata + ordered worksheets."""

    format = FORMAT
    mime_type = MIME_TYPE
    method = METHOD
    version = VERSION

    def extract(self, file_path: Path, document: dict) -> dict:
        """Extract ``file_path`` and return the v2 extraction envelope.

        ``document`` is the inventory record for the file (identity fields
        only). Raises on any failure so the orchestrator can report it and
        keep going.
        """
        file_path = Path(file_path)
        workbook = load_workbook(file_path, data_only=False)
        return {
            "document": document_identity(document),
            "source": {
                "format": FORMAT,
                "mime_type": MIME_TYPE,
                "sheet_count": len(workbook.sheetnames),
            },
            "extraction": {
                "version": VERSION,
                "method": METHOD,
                "status": "success",
            },
            "metadata": _extract_metadata(workbook),
            "content": {"sheets": _extract_sheets(workbook)},
        }


def _extract_metadata(workbook) -> dict:
    """Core workbook properties, dropping empty values and JSON-safe dates."""
    metadata: dict = {}
    props = workbook.properties
    for key in (
        "creator", "title", "subject", "description", "keywords",
        "category", "language", "version", "created", "modified",
        "lastModifiedBy", "lastPrinted", "identifier",
    ):
        value = getattr(props, key, None)
        if value not in (None, ""):
            metadata[key] = json_safe(value)
    return metadata


def _extract_sheets(workbook) -> list[dict]:
    sheets = []
    for sheet_index, name in enumerate(workbook.sheetnames):
        worksheet = workbook[name]
        sheets.append(_extract_sheet(worksheet, sheet_index))
    return sheets


def _extract_sheet(worksheet, sheet_index: int) -> dict:
    entry: dict = {
        "name": worksheet.title,
        "sheet_index": sheet_index,
        "dimensions": worksheet.dimensions,
        "max_row": worksheet.max_row,
        "max_column": worksheet.max_column,
    }

    rows = []
    for row in worksheet.iter_rows():
        cells = [_extract_cell(cell) for cell in row]
        if not any(cell.get("value") is not None or cell.get("formula") for cell in cells):
            continue  # skip fully-empty rows
        rows.append({"row_index": row[0].row, "cells": cells})
    entry["rows"] = rows

    merged = sorted(str(rng) for rng in worksheet.merged_cells.ranges)
    if merged:
        entry["merged_cells"] = merged

    tables = sorted(
        [{"name": table.displayName, "ref": table.ref} for table in worksheet.tables.values()],
        key=lambda t: t["name"],
    )
    if tables:
        entry["tables"] = tables
    return entry


def _extract_cell(cell) -> dict:
    """One cell: coordinate, value, data type, and formula where present.

    MergedCell placeholders have no ``internal_value``; they read as None,
    which is exactly what they are.
    """
    try:
        internal_value = cell.internal_value
    except AttributeError:
        internal_value = None
    entry: dict = {
        "coordinate": cell.coordinate,
        "column": cell.column,
        "row": cell.row,
        "value": json_safe(internal_value),
        "data_type": cell.data_type,
    }
    if cell.data_type == "f" and isinstance(cell.value, str):
        entry["formula"] = cell.value  # the "=..." text; the cached value stays in "value"
    return entry
