"""Structured DOCX extraction via python-docx — v2.

Preserves the logical structure of a Word document instead of flattening it
into one string:

    metadata        core document properties (author, title, dates, revision)
    content
        paragraphs  in true document order (interleaved with tables), each
                    with its style, a detected type (title / heading / list /
                    paragraph), the heading level, and the list kind
        tables      in document order, rows -> cells (cell text)
        images      inline-shape count + image-part content-type references

Document order is the structure: the body is walked element by element so a
table between two paragraphs stays between them. No PDF-specific concepts
(pages, coordinates) are forced onto DOCX — the native style hierarchy and
order are the semantic model.

Heuristics (all deterministic, no LLM):

* **Headings** come from the paragraph style: ``Title`` / ``Subtitle`` and any
  ``Heading N`` / ``Heading N 1``-style name; the level comes from the trailing
  number, capped at 6.
* **Lists** are detected from the paragraph style (``List Bullet*`` →
  unordered, ``List Number*`` → ordered) or, when a ``numPr`` (numbering)
  reference is present without a list style, by resolving the numbering part to
  its ``numFmt`` (``bullet`` → unordered, everything else → ordered).
* **Paragraph type** falls back to ``paragraph`` for everything else.

The extractor is pure: it reads a DOCX file and returns a machine-readable
dict; all filesystem/output orchestration lives in the orchestrator.
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from extractors._v2common import document_identity, json_safe

METHOD = "python-docx"
VERSION = "2.0.0"
FORMAT = "docx"
MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

HEADING_RE = re.compile(r"^Heading\s+(\d+)", re.IGNORECASE)

_TITLE_STYLES = {"title", "subtitle", "heading 1"}


class DocxExtractorV2:
    """V2 semantic DOCX extractor: metadata + ordered paragraphs/tables/images."""

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
        doc = DocxDocument(file_path)
        return {
            "document": document_identity(document),
            "source": {
                "format": FORMAT,
                "mime_type": MIME_TYPE,
            },
            "extraction": {
                "version": VERSION,
                "method": METHOD,
                "status": "success",
            },
            "metadata": _extract_metadata(doc),
            "content": _extract_content(doc),
        }


def _extract_metadata(doc) -> dict:
    """Core document properties, dropping empty values and JSON-safe dates."""
    metadata: dict = {}
    props = doc.core_properties
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


def _extract_content(doc) -> dict:
    """Body content in true document order: paragraphs and tables interleaved."""
    paragraphs: list[dict] = []
    tables: list[dict] = []
    for index, child in enumerate(doc.element.body.iterchildren()):
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paragraph = Paragraph(child, doc)
            info = _paragraph_info(paragraph, index)
            if info is not None:
                paragraphs.append(info)
        elif tag == "tbl":
            table = _table_info(Table(child, doc), index)
            if table is not None:
                tables.append(table)

    content: dict = {
        "paragraphs": paragraphs,
        "tables": tables,
        "images": _image_references(doc),
    }
    if len(doc.sections) > 1:
        content["sections"] = [
            {"index": i, "orientation": s.orientation.name.lower()}
            for i, s in enumerate(doc.sections)
        ]
    return content


def _paragraph_info(paragraph: Paragraph, index: int) -> dict | None:
    """One paragraph: style, detected type, level and list kind."""
    text = paragraph.text.strip()
    if not text:
        return None  # empty paragraphs carry no structure

    info: dict = {"index": index, "text": text}
    style_name = (paragraph.style.name or "").strip() if paragraph.style else ""
    info["style"] = style_name or None

    paragraph_type, level = _classify(style_name)
    info["type"] = paragraph_type
    if level is not None:
        info["level"] = level

    is_list, ordered = _list_info(paragraph, style_name)
    if is_list:
        info["type"] = "list"
        info["list"] = "number" if ordered is True else "bullet" if ordered is False else "unknown"
        info["ordered"] = ordered
    return info


def _classify(style_name: str) -> tuple[str, int | None]:
    """Map a Word style name to ``(type, level)``.

    ``Title`` / ``Subtitle`` become their own types; ``Heading N`` becomes a
    heading with level N (clamped to 1..6); a bare ``Heading`` style becomes
    level 1; anything else is a plain paragraph.
    """
    lower = style_name.lower()
    if lower == "title":
        return "title", None
    if lower == "subtitle":
        return "subtitle", None
    match = HEADING_RE.match(style_name)
    if match:
        return "heading", max(1, min(6, int(match.group(1))))
    if lower.startswith("heading") or lower in _TITLE_STYLES:
        return "heading", 1
    return "paragraph", None


def _list_info(paragraph: Paragraph, style_name: str) -> tuple[bool, bool | None]:
    """``(is_list_item, ordered)`` — None ordered means the kind is unknown."""
    if "list bullet" in style_name.lower():
        return True, False
    if "list number" in style_name.lower():
        return True, True
    p_pr = paragraph._p.pPr
    if p_pr is None or p_pr.numPr is None:
        return False, None
    num_id = p_pr.numPr.numId.val if p_pr.numPr.numId is not None else None
    ilvl = p_pr.numPr.ilvl.val if p_pr.numPr.ilvl is not None else 0
    num_fmt = _resolve_numfmt(paragraph, num_id, ilvl)
    if num_fmt is None:
        return True, None
    return True, num_fmt.lower() != "bullet"


def _resolve_numfmt(paragraph: Paragraph, num_id: int | None, ilvl: int) -> str | None:
    """Best-effort ``numFmt`` for a ``numPr`` reference (via the numbering part)."""
    if num_id is None:
        return None
    try:
        numbering_part = paragraph.part.numbering_part
        root = numbering_part.numbering_definitions._numbering
        abstract = root.xpath(f'w:num[@w:numId="{num_id}"]/w:abstractNumId/@w:val')
        if not abstract:
            return None
        formats = root.xpath(
            f'w:abstractNum[@w:abstractNumId="{abstract[0]}"]'
            f'/w:lvl[@w:ilvl="{ilvl}"]/w:numFmt/@w:val'
        )
        return formats[0] if formats else None
    except Exception:  # noqa: BLE001 — never let numbering resolution break extraction
        return None


def _table_info(table: Table, index: int) -> dict:
    """Rows -> cells, one cell per column; merged cells repeat their value."""
    rows = []
    for row in table.rows:
        rows.append([cell.text.strip() for cell in row.cells])
    return {"index": index, "rows": rows}


def _image_references(doc) -> dict:
    """Inline-shape count plus image-part references where practical."""
    try:
        inline_count = len(doc.inline_shapes)
    except Exception:  # noqa: BLE001
        inline_count = 0
    parts: list[dict] = []
    for rel in doc.part.rels.values():
        ref = _image_ref(rel)
        if ref is not None:
            parts.append(ref)
    return {"inline_shapes": inline_count, "image_parts": parts}


def _image_ref(rel) -> dict | None:
    """One image-part reference, or None when the relationship is not an image."""
    try:
        if not rel.reltype.endswith("/image"):
            return None
        return {"content_type": rel.target_part.content_type, "target": rel.target_ref}
    except Exception:  # noqa: BLE001
        return None
