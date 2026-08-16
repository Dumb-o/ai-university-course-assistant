"""Shared helpers for the v2 non-PDF extractors.

Keeps the document-identity block and JSON serialization consistent across the
DOCX / PPTX / XLSX extractors without coupling their format-specific code.
"""

from __future__ import annotations

import datetime
from decimal import Decimal


def document_identity(document: dict) -> dict:
    """The identity block every v2 extraction envelope carries.

    ``document`` is the inventory record for the file; only the stable identity
    fields are copied (same as PdfExtractorV2).
    """
    return {
        "document_id": document.get("document_id"),
        "course_id": document.get("course_id"),
        "filename": document.get("filename"),
        "relative_path": document.get("relative_path"),
    }


def json_safe(value):
    """Coerce non-JSON values (datetime, Decimal, ...) to JSON-safe values.

    Anything that is not a primitive is stringified so the orchestrator can
    always ``json.dump`` the extraction envelope.
    """
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
