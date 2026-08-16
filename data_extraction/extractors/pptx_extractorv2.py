"""Structured PPTX extraction via python-pptx — v2.

Preserves the slide structure of a presentation instead of flattening it:

    metadata        core presentation properties (author, title, dates, ...)
    content
        slides      in order, each with its slide number, layout name, title,
                    shapes (text / picture / table / chart / group), and
                    speaker notes

A slide stays a slide — ``presentation → slide → title/text/table/notes`` —
so downstream stages can treat each slide as a unit. No PDF-specific concepts
(pages, coordinates) are forced onto PPTX; the native slide/shape hierarchy is
the semantic model.

Shape handling (deterministic):

* **Text shapes** expose their paragraphs in order, each with ``level`` and a
  best-effort bullet kind derived from the paragraph properties (``buChar`` →
  bullet, ``buAutoNum`` → number).
* **Tables** are emitted as rows of cell texts.
* **Pictures / charts** are recorded as references (content type) rather than
  pixels — the raw file is the image source of record.
* **Notes** are the slide's speaker-notes text when present.

The extractor is pure: it reads a PPTX file and returns a machine-readable
dict; all filesystem/output orchestration lives in the orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from extractors._v2common import document_identity, json_safe

METHOD = "python-pptx"
VERSION = "2.0.0"
FORMAT = "pptx"
MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}

PICTURE_SHAPE_TYPE = MSO_SHAPE_TYPE.PICTURE
CHART_SHAPE_TYPE = MSO_SHAPE_TYPE.CHART
TABLE_SHAPE_TYPE = MSO_SHAPE_TYPE.TABLE
GROUP_SHAPE_TYPE = MSO_SHAPE_TYPE.GROUP


class PptxExtractorV2:
    """V2 semantic PPTX extractor: metadata + ordered slides with shapes/notes."""

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
        prs = Presentation(file_path)
        return {
            "document": document_identity(document),
            "source": {
                "format": FORMAT,
                "mime_type": MIME_TYPE,
                "slide_count": len(prs.slides),
            },
            "extraction": {
                "version": VERSION,
                "method": METHOD,
                "status": "success",
            },
            "metadata": _extract_metadata(prs),
            "content": {"slides": _extract_slides(prs)},
        }


def _extract_metadata(prs) -> dict:
    """Core presentation properties, dropping empty values and JSON-safe dates."""
    metadata: dict = {}
    props = prs.core_properties
    for key in (
        "author", "title", "subject", "keywords", "comments",
        "category", "language", "last_modified_by", "revision",
        "content_status", "identifier", "version", "created", "modified",
        "last_printed",
    ):
        value = getattr(props, key, None)
        if value not in (None, ""):
            metadata[key] = json_safe(value)
    return metadata


def _extract_slides(prs) -> list[dict]:
    slides = []
    for slide_number, slide in enumerate(prs.slides, start=1):
        slides.append(_extract_slide(slide, slide_number))
    return slides


def _extract_slide(slide, slide_number: int) -> dict:
    entry: dict = {"slide_number": slide_number}

    try:
        layout = slide.slide_layout
        entry["layout_name"] = layout.name if layout is not None else None
    except Exception:  # noqa: BLE001
        entry["layout_name"] = None

    title = slide.shapes.title
    entry["title"] = title.text.strip() if title is not None and title.text.strip() else None

    shapes = []
    for shape in slide.shapes:
        shapes.append(_extract_shape(shape))
    entry["shapes"] = shapes

    notes = _extract_notes(slide)
    if notes:
        entry["notes"] = notes
    return entry


def _extract_shape(shape) -> dict:
    """One shape as a typed dict; group shapes flatten their children."""
    if shape.shape_type == TABLE_SHAPE_TYPE:
        return {"type": "table", "name": shape.name, "rows": _shape_table(shape)}
    if shape.shape_type == PICTURE_SHAPE_TYPE:
        return {"type": "picture", "name": shape.name, **_picture_ref(shape)}
    if shape.shape_type == CHART_SHAPE_TYPE:
        return {"type": "chart", "name": shape.name}
    if shape.shape_type == GROUP_SHAPE_TYPE:
        try:
            return {
                "type": "group",
                "name": shape.name,
                "shapes": [_extract_shape(child) for child in shape.shapes],
            }
        except Exception:  # noqa: BLE001
            return {"type": "group", "name": shape.name, "shapes": []}
    if shape.has_text_frame:
        paragraphs = [_extract_paragraph(p) for p in shape.text_frame.paragraphs]
        paragraphs = [p for p in paragraphs if p is not None]
        entry: dict = {"type": "text", "name": shape.name}
        if paragraphs:
            entry["paragraphs"] = paragraphs
        return entry
    return {"type": "other", "name": shape.name}


def _extract_paragraph(paragraph) -> dict | None:
    text = paragraph.text.strip()
    if not text:
        return None
    entry: dict = {"text": text, "level": paragraph.level}
    bullet = _bullet_kind(paragraph)
    if bullet is not None:
        entry["bullet"] = bullet
    return entry


def _bullet_kind(paragraph) -> str | None:
    """Best-effort bullet kind from the paragraph's text properties.

    ``buChar`` → "bullet", ``buAutoNum`` → "number", ``buNone`` → None.
    """
    try:
        p_pr = paragraph._pPr
        if p_pr is None:
            return None
        p_pr = p_pr.find(f"{{{_NS['a']}}}pPr")
        if p_pr is None:
            return None
        if p_pr.find(f"{{{_NS['a']}}}buNone") is not None:
            return None
        if p_pr.find(f"{{{_NS['a']}}}buChar") is not None:
            return "bullet"
        if p_pr.find(f"{{{_NS['a']}}}buAutoNum") is not None:
            return "number"
    except Exception:  # noqa: BLE001
        return None
    return None


def _shape_table(shape) -> list[list[str]]:
    return [
        [cell.text.strip() for cell in row.cells]
        for row in shape.table.rows
    ]


def _picture_ref(shape) -> dict:
    try:
        return {"content_type": shape.image.content_type}
    except Exception:  # noqa: BLE001
        return {}


def _extract_notes(slide) -> str | None:
    try:
        if not slide.has_notes_slide:
            return None
        text = slide.notes_slide.notes_text_frame.text.strip()
        return text or None
    except Exception:  # noqa: BLE001
        return None
