"""Extractor package: routes a file extension to the extractor that handles it.

The orchestrator follows this decision chain per document:

    inventory
        ↓
    processing_status == "pending"?
        ↓
    extension
        ↓
    registered extractor exists?
        ↓
    YES → extract        NO → skip, keep "pending"

An extension with no registry entry has no extractor yet (DOCX, PPTX, XLSX,
images, ...) — such documents are skipped and stay ``pending``; they are never
marked failed merely because their extractor is not implemented.

Adding a future format means:

    1. create ``extractors/<format>_extractor.py`` implementing the same
       ``extract(file_path, document) -> dict`` interface as ``PdfExtractor``
    2. register it here: ``".docx": DocxExtractor``

No other pipeline code needs to change.
"""

from __future__ import annotations

from extractors.pdf_extractor import PdfExtractor

# Extension (lower-cased, leading dot) -> extractor class.
# PDF is the only implemented extractor; future entries are added here.
EXTRACTOR_REGISTRY: dict[str, type] = {
    ".pdf": PdfExtractor,
}


def get_extractor(extension: str | None) -> type | None:
    """Return the registered extractor class for ``extension``, or None."""
    if not extension:
        return None
    return EXTRACTOR_REGISTRY.get(extension.lower())
