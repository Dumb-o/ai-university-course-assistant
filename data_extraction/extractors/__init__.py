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

**V1 vs V2.** ``get_extractor`` (``EXTRACTOR_REGISTRY``) is used by the v1
orchestrator (`extraction_orchestrator.py`). ``get_extractor_v2``
(``EXTRACTOR_REGISTRY_V2``) is used by the v2 orchestrator
(`extraction_orchestratorv2.py`). The two registries are deliberately separate,
and the v2 extractors are imported lazily so that importing this package never
requires python-docx / python-pptx / openpyxl — v1 keeps working in a minimal
environment.
"""

from __future__ import annotations

from extractors.pdf_extractor import PdfExtractor

# Extension (lower-cased, leading dot) -> extractor class.
# PDF is the only implemented v1 extractor; future entries are added here.
EXTRACTOR_REGISTRY: dict[str, type] = {
    ".pdf": PdfExtractor,
}

# Extension (lower-cased, leading dot) -> (module, extractor class name).
# Resolved lazily by ``get_extractor_v2`` so v1 never depends on the
# python-docx / python-pptx / openpyxl libraries.
EXTRACTOR_REGISTRY_V2: dict[str, tuple[str, str]] = {
    ".pdf": ("pdf_extractorv2", "PdfExtractorV2"),
    ".docx": ("docx_extractorv2", "DocxExtractorV2"),
    ".pptx": ("pptx_extractorv2", "PptxExtractorV2"),
    ".xlsx": ("xlsx_extractorv2", "XlsxExtractorV2"),
}


def get_extractor(extension: str | None) -> type | None:
    """Return the registered extractor class for ``extension``, or None."""
    if not extension:
        return None
    return EXTRACTOR_REGISTRY.get(extension.lower())


def get_extractor_v2(extension: str | None) -> type | None:
    """Return the v2 extractor class for ``extension`` (lazy import), or None."""
    if not extension:
        return None
    entry = EXTRACTOR_REGISTRY_V2.get(extension.lower())
    if entry is None:
        return None
    module_name, class_name = entry
    import importlib

    module = importlib.import_module(f"extractors.{module_name}")
    return getattr(module, class_name)
