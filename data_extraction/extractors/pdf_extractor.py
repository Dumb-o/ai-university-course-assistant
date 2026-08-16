"""Structured PDF extraction via PyMuPDF.

Preserves as much of the source document's structure as PyMuPDF exposes:

* document metadata (title, author, producer, page count, ...)
* page boundaries (each page is kept separate, 1-based page numbers preserved)
* page text
* text/image blocks with their bounding boxes
* native table extraction (``Page.find_tables``), best-effort per page

The extractor is pure: it reads a PDF file and returns a machine-readable dict.
All filesystem/output orchestration lives in the orchestrator.

Note: the module imports ``pymupdf`` (PyMuPDF). ``fitz`` is the legacy import
name and is used as a fallback so the same code runs on older PyMuPDF builds.
"""

from __future__ import annotations

from pathlib import Path

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 (canonical import name)
except ImportError:
    import fitz  # PyMuPDF < 1.24 (legacy import name)

METHOD = "pymupdf"
VERSION = "1.0.0"
FORMAT = "pdf"
MIME_TYPE = "application/pdf"

# ``page.get_text("blocks")`` emits 7-tuples; the last element is the block
# type: 0 = text, 1 = image.
TEXT_BLOCK_TYPE = 0
IMAGE_BLOCK_TYPE = 1


class PdfExtractor:
    """Extracts structured content from a single PDF document."""

    format = FORMAT
    mime_type = MIME_TYPE
    method = METHOD
    version = VERSION

    def extract(self, file_path: Path, document: dict) -> dict:
        """Extract ``file_path`` and return the extraction envelope.

        ``document`` is the inventory record for the file; it supplies the
        identity fields copied into the output. Raises on any failure so the
        orchestrator can record the error and keep going.
        """
        file_path = Path(file_path)
        doc = fitz.open(file_path)
        try:
            pages = [_extract_page(page) for page in doc]
            metadata = _extract_metadata(doc, len(pages))
        finally:
            doc.close()
        return {
            "document": {
                "document_id": document.get("document_id"),
                "course_id": document.get("course_id"),
                "filename": document.get("filename"),
                "relative_path": document.get("relative_path"),
            },
            "source": {"format": FORMAT, "mime_type": MIME_TYPE},
            "extraction": {
                "method": METHOD,
                "status": "success",
                "extractor_version": VERSION,
            },
            "metadata": metadata,
            "pages": pages,
        }


def _extract_metadata(doc, page_count: int) -> dict:
    """Document-level metadata from PyMuPDF, dropping unset values."""
    metadata = {}
    for key, value in (doc.metadata or {}).items():
        if value not in (None, ""):
            metadata[key] = value
    metadata["page_count"] = page_count
    return metadata


def _extract_page(page):
    """One page: text, text/image blocks with bboxes, and best-effort tables."""
    return {
        "page_number": page.number + 1,
        "width": page.rect.width,
        "height": page.rect.height,
        "rotation": page.rotation,
        "text": page.get_text("text"),
        "blocks": _extract_blocks(page),
        "tables": _extract_tables(page),
    }


def _extract_blocks(page) -> list[dict]:
    """Text/image blocks as ``{"type", "bbox", "text"?}`` dicts."""
    blocks: list[dict] = []
    try:
        raw_blocks = page.get_text("blocks")
    except RuntimeError:  # PyMuPDF errors are RuntimeError subclasses
        return blocks
    for x0, y0, x1, y1, text, _block_no, block_type in raw_blocks:
        block = {
            "type": "image" if block_type == IMAGE_BLOCK_TYPE else "text",
            "bbox": [x0, y0, x1, y1],
        }
        if block_type == TEXT_BLOCK_TYPE:
            block["text"] = text
        blocks.append(block)
    return blocks


def _extract_tables(page) -> list[dict]:
    """Best-effort native table extraction; never allowed to break a page.

    PyMuPDF's ``find_tables`` needs no extra dependency. A table is emitted as
    ``{"bbox", "rows"}`` where ``rows`` is a list of rows of cell texts. Any
    page that fails to produce tables simply contributes none.
    """
    try:
        finder = page.find_tables()
    except RuntimeError:  # PyMuPDF errors are RuntimeError subclasses
        return []
    tables: list[dict] = []
    for table in finder.tables:
        tables.append({
            "bbox": list(table.bbox),
            "rows": table.extract(),
        })
    return tables
