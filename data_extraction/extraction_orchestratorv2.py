#!/usr/bin/env python3
"""Course extraction orchestrator — V2 semantic extraction (multi-format).

The V2 pipeline is an experimental second extraction implementation that runs
*alongside* v1 so both outputs can be compared. Like v1 it is course-centric
and inventory-driven:

    course-data/
    ├── metadata/courses.json                      central course registry
    ├── inventories/<course_id>/file_inventory.json   what files exist
    ├── raw/<course_id>/                           READ-ONLY original files
    ├── extracted/<course_id>/                     V1 extraction output
    └── extracted_v2/<course_id>/                  V2 extraction output

Responsibilities are kept strictly separate:

* ``file_inventory.json`` describes **what files exist** in a course. It is
  **never modified** by v2 and it is **not** used to decide what v2 should
  process — ``processing_status`` reflects the existing (v1) workflow and v2
  deliberately does not depend on it.
* ``raw/`` holds the original downloaded files. Never modified.
* ``extracted/`` is v1 output. Never touched by v2.
* ``extracted_v2/`` is v2 output **and** v2's processing state: a document is
  considered already processed by v2 iff its expected v2 JSON already exists.
  This is what makes v2 independently testable (v1 may already have marked the
  inventory record ``extracted``) and what keeps v1/v2 state distinguishable
  without ever adding ``v2_processing_status``-style fields to the inventory.

Per document the decision chain is:

    inventory (documents list) → v2 extractor for extension? → output exists? → extract
        │                                                            │
        │ (unsupported formats stay untouched)                      └── exists → skip
        └── ... with --force the output-existence check is skipped     missing → extract

Supported formats (V2, modern, registered in ``extractors/__init__.py``):

    .pdf  → PdfExtractorV2   (PyMuPDF)          .pptx → PptxExtractorV2  (python-pptx)
    .docx → DocxExtractorV2  (python-docx)      .xlsx → XlsxExtractorV2  (openpyxl)

Legacy Microsoft Office formats have no *direct* v2 extractor. They are
converted to their modern equivalents with LibreOffice (headless) in a
temporary directory and then extracted by the existing modern extractor — the
modern extractors stay completely oblivious to the conversion:

    .doc → (LibreOffice) → .docx → DocxExtractorV2
    .ppt → (LibreOffice) → .pptx → PptxExtractorV2
    .xls → (LibreOffice) → .xlsx → XlsxExtractorV2

See ``legacy_conversion.py`` for the converter and ``README.md`` (section
"Legacy Microsoft Office Format Handling") for the design. Everything else
(``.jpg``, ``.png``, ...) is unsupported: it is reported under ``Unsupported:``
and never marked failed.

CLI:

    python3 data_extraction/extraction_orchestratorv2.py --course-id mytimes-47164
    python3 data_extraction/extraction_orchestratorv2.py --course-id mytimes-47164 --force
    python3 data_extraction/extraction_orchestratorv2.py --course-id mytimes-47164 --dry-run
    python3 data_extraction/extraction_orchestratorv2.py \
        --course-id mytimes-47164 --document-id mytimes-47164-group-assignment-group-assignment-pdf

Status: IMPLEMENTED (v2, PDF + DOCX + PPTX + XLSX + DOC/PPT/XLS via LibreOffice).
Part of the data_extraction stage.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

from extraction_orchestrator import (
    DEFAULT_DATA_ROOT,
    EXIT_FAILURE,
    find_course,
    load_inventory,
    load_registry,
    output_path_for_document,
    write_json_atomic,
)
from extractors import get_extractor_v2
from legacy_conversion import (
    LEGACY_FORMATS,
    MIME_TO_LEGACY,
    LegacyConversionError,
    convert_legacy_file,
    find_libreoffice,
)

REGISTRY_FILENAME = "courses.json"
INVENTORY_FILENAME = "file_inventory.json"

MODERN_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx")


def _extension(doc: dict) -> str:
    return (doc.get("extension") or "").lower()


_MODERN_MIME_TO_EXT: dict[str, str] | None = None


def _modern_mime_to_ext() -> dict[str, str]:
    """Modern-format MIME → extension map, built lazily from the extractors.

    Built on first use so a course with only PDFs never forces the lazy
    python-docx / python-pptx / openpyxl imports.
    """
    global _MODERN_MIME_TO_EXT  # noqa: PLW0603 — one-shot lazy cache
    if _MODERN_MIME_TO_EXT is None:
        _MODERN_MIME_TO_EXT = {
            get_extractor_v2(ext).mime_type: ext
            for ext in MODERN_EXTENSIONS
            if get_extractor_v2(ext) is not None
        }
    return _MODERN_MIME_TO_EXT


def _route_document(doc: dict) -> tuple[str, str | None]:
    """Classify an inventory document.

    Returns ``(kind, key)`` where ``kind`` is one of ``"modern"``, ``"legacy"``
    or ``"unsupported"``:

    * modern → the modern extension (e.g. ``".docx"``) whose extractor is used
      directly;
    * legacy → the legacy extension (e.g. ``".doc"``), looked up in
      ``LEGACY_FORMATS`` for the conversion target;
    * unsupported → ``None`` (reported, never failed).

    The file extension is authoritative; the inventory's MIME type is used as a
    routing fallback when the extension is missing or unrecognized (never the
    other way round).
    """
    extension = _extension(doc)
    if get_extractor_v2(extension) is not None:
        return "modern", extension
    if extension in LEGACY_FORMATS:
        return "legacy", extension
    mime = (doc.get("mime_type") or "").lower()
    legacy_ext = MIME_TO_LEGACY.get(mime)
    if legacy_ext is not None:
        return "legacy", legacy_ext
    modern_ext = _modern_mime_to_ext().get(mime)
    if modern_ext is not None:
        return "modern", modern_ext
    return "unsupported", None


def run_extraction(course_id: str, data_root: Path | None = None,
                   limit: int | None = None, document_id: str | None = None,
                   force: bool = False, dry_run: bool = False) -> int:
    """Run V2 semantic extraction for ONE course. Returns a process exit code.

    ``data_root`` defaults to the repo-anchored ``course-data/``; tests pass a
    temporary root. ``limit`` caps the number of documents processed (any
    supported type). ``document_id`` restricts processing to one document.
    ``force`` re-extracts documents whose v2 output already exists. ``dry_run``
    reports what would be extracted (including LibreOffice conversions) without
    converting or writing anything.
    """
    data_root = Path(data_root) if data_root is not None else DEFAULT_DATA_ROOT
    registry_path = data_root / "metadata" / REGISTRY_FILENAME

    # STEP 1 — validate the course against the central registry.
    course = find_course(load_registry(data_root), course_id)
    if course is None:
        print(f"ERROR: course '{course_id}' not found in {registry_path}", file=sys.stderr)
        print("Run data_ingestion/generate_inventory.py for the course first.", file=sys.stderr)
        return EXIT_FAILURE

    # STEP 2 — locate the course inventory (never auto-generated here).
    inventory_path = data_root / "inventories" / course_id / INVENTORY_FILENAME
    try:
        inventory = load_inventory(inventory_path)
    except (FileNotFoundError, ValueError):
        return EXIT_FAILURE

    documents = inventory["documents"]
    raw_root = data_root / "raw" / course_id
    v2_root = data_root / "extracted_v2" / course_id

    course_name = course.get("course_name") or course_id
    print(f"Course: {course_name}")
    print(f"Course ID: {course_id}")
    print()
    print(f"Inventory: {inventory_path}  (read-only — never modified by v2)")
    print(f"Raw: {raw_root}/")
    print(f"Extracted V2: {v2_root}/")
    print()

    if not raw_root.is_dir():
        print(f"WARNING: raw course directory does not exist: {raw_root}")

    # STEP 3 — v2 candidates are ALL documents whose format is supported,
    # regardless of the inventory's processing_status (that status reflects
    # v1). Supported = modern formats (direct v2 extractor) plus legacy
    # formats (LibreOffice conversion → modern extractor). Everything else is
    # reported under Unsupported: and never processed or failed.
    modern_docs: list[tuple[dict, str]] = []
    legacy_docs: list[tuple[dict, str]] = []
    unsupported_docs: list[dict] = []
    for doc in documents:
        kind, key = _route_document(doc)
        if kind == "modern":
            modern_docs.append((doc, key))
        elif kind == "legacy":
            legacy_docs.append((doc, key))
        else:
            unsupported_docs.append(doc)

    supported_counts = Counter(key for _, key in modern_docs + legacy_docs)
    unsupported_counts = Counter(_extension(doc) for doc in unsupported_docs)

    candidates = [doc for doc, _ in modern_docs] + [doc for doc, _ in legacy_docs]

    if document_id:
        targeted = [d for d in candidates if d.get("document_id") == document_id]
        if not targeted:
            existing = next((d for d in documents if d.get("document_id") == document_id), None)
            if existing is None:
                print(f"ERROR: document_id '{document_id}' not found in the inventory.", file=sys.stderr)
                return EXIT_FAILURE
            kind, key = _route_document(existing)
            if kind == "legacy":
                print(f"Document '{document_id}' is a legacy format "
                      f"({_extension(existing)}); converted via LibreOffice.")
                candidates = [existing]
            else:
                print(f"Document '{document_id}' has no v2 extractor "
                      f"(extension '{_extension(existing)}'); nothing to do.")
                candidates = []
        else:
            candidates = targeted

    if limit is not None and limit > 0:
        candidates = candidates[:limit]

    _print_counts(supported_counts, unsupported_counts, total=len(documents))
    print()

    # STEP 4 — split candidates by whether their v2 output already exists.
    # Output-existence is v2's processing state: a document is "already done"
    # iff its expected v2 JSON exists (unless --force re-extracts it).
    already_extracted = 0
    to_process: list[tuple[dict, Path, str, str]] = []
    for doc in candidates:
        output_path = v2_root / output_path_for_document(doc.get("relative_path"))
        if output_path.is_file() and not force:
            already_extracted += 1
        else:
            kind, key = _route_document(doc)
            to_process.append((doc, output_path, kind, key))

    # Detect LibreOffice once per run, but only when a legacy document needs it.
    libreoffice_info = (
        find_libreoffice() if any(kind == "legacy" for _, _, kind, _ in to_process) else None
    )
    if any(kind == "legacy" for _, _, kind, _ in to_process) and libreoffice_info is None:
        print(
            "WARNING: LibreOffice is required for legacy Office (.doc/.ppt/.xls) "
            "extraction, but neither 'libreoffice' nor 'soffice' was found on PATH. "
            "Legacy documents will be reported as failed (conversion_failed) while "
            "modern formats continue. See legacy_conversion.py.",
            file=sys.stderr,
        )

    extracted_count = 0
    failed_count = 0

    print("Processing V2 documents:")
    total = len(to_process)

    # A per-run temporary directory for LibreOffice conversion output. It lives
    # outside course-data/raw/ and is removed automatically when the run ends,
    # so converted files are never persisted and raw files are never touched.
    with tempfile.TemporaryDirectory(prefix="v2_conversion_") as tmp_root:
        for index, (doc, output_path, kind, key) in enumerate(to_process, start=1):
            relative_path = doc.get("relative_path")
            print(f"[{index}/{total}] {relative_path}")
            if dry_run:
                print(f"  would process via {_plan_line(kind, key)}")
                continue
            raw_path = raw_root / relative_path
            if not raw_path.is_file():
                print(f"  failed: raw file missing: {raw_path}")
                failed_count += 1
                continue
            try:
                result = _extract_one(doc, raw_path, kind, key, tmp_root, libreoffice_info)
                write_json_atomic(output_path, result)
                extracted_count += 1
            except LegacyConversionError as exc:
                # A legacy conversion failure is a hard document failure: the
                # original file is untouched, no output is written (so the
                # document stays eligible for retry), and the batch continues.
                print(f"  failed (conversion_failed): {exc}")
                failed_count += 1
            except Exception as exc:  # noqa: BLE001 — one bad document must never abort the batch
                print(f"  failed: {exc}")
                failed_count += 1

    print()
    print("Extraction V2 complete.")
    print(f"Documents found: {len(documents)}")
    for extension, count in sorted(supported_counts.items()):
        print(f"  {extension[1:].upper()} files found: {count}")
    print(f"Documents already extracted (v2): {already_extracted}")
    print(f"Documents extracted now: {extracted_count}")
    print(f"Documents failed: {failed_count}")
    print(f"Unsupported files skipped: {len(unsupported_docs)}")
    print("Note: the inventory was not modified; v2 state lives in extracted_v2/.")
    return 0


def _plan_line(kind: str, key: str) -> str:
    """Human-readable route summary (used by --dry-run)."""
    if kind == "legacy":
        legacy = LEGACY_FORMATS[key]
        return (
            f"LibreOffice conversion {legacy.extension} -> {legacy.target_extension} "
            f"then {legacy.target_format_name}_extractorv2"
        )
    return f"modern {get_extractor_v2(key).__name__}"  # type: ignore[union-attr]


def _extract_one(doc: dict, raw_path: Path, kind: str, key: str,
                 tmp_root: str, libreoffice_info) -> dict:
    """Extract ONE document, converting it first when it is a legacy format.

    For legacy documents the converted file is produced inside ``tmp_root`` and
    the modern extractor for the target extension is used; the result envelope
    is then augmented with the original-source + conversion provenance. Modern
    documents bypass conversion entirely.
    """
    if kind == "legacy":
        legacy = LEGACY_FORMATS[key]
        converted = convert_legacy_file(
            raw_path,
            legacy.target_format_name,
            Path(tmp_root) / _safe_dir_name(doc),
            libreoffice=libreoffice_info,
            profile_dir=Path(tmp_root) / "lo_profile",
        )
        extractor_class = get_extractor_v2(legacy.target_extension)
        result = extractor_class().extract(converted, doc)
        return _augment_legacy_result(result, legacy)
    extractor_class = get_extractor_v2(key)
    return extractor_class().extract(raw_path, doc)


def _augment_legacy_result(result: dict, legacy) -> dict:
    """Record the ORIGINAL source + conversion provenance on a legacy envelope.

    The modern extractor already built the envelope with the inventory's
    identity (original filename / relative_path); this replaces ``source`` with
    the original format facts, adds the ``conversion`` block, and names the
    extractor that actually parsed the file. ``extraction_format`` / the
    ``conversion`` block record that the file was parsed via its modern
    equivalent — the conversion stays an internal implementation detail.
    """
    result["source"] = {
        "format": legacy.format_name,
        "mime_type": legacy.mime_type,
        "extension": legacy.extension,
        "extraction_format": legacy.target_format_name,
    }
    result["conversion"] = {
        "performed": True,
        "tool": "LibreOffice",
        "source_format": legacy.extension,
        "target_format": legacy.target_extension,
        "status": "success",
    }
    result["extraction"]["extractor"] = f"{legacy.target_format_name}_extractorv2"
    return result


def _safe_dir_name(doc: dict) -> str:
    """A filesystem-safe per-document directory name under the conversion root.

    Keeps every legacy file in its own sub-directory so two documents with the
    same filename never collide in LibreOffice's ``--outdir``.
    """
    document_id = doc.get("document_id") or ""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", document_id).strip("._") or "document"
    return f"{slug}"


def _print_counts(supported_counts: Counter, unsupported_counts: Counter, total: int) -> None:
    """Report supported/unsupported document counts, calculated from the inventory."""
    print("Documents:")
    print(f"Total: {total}")
    print("Supported:")
    if supported_counts:
        for extension, count in sorted(supported_counts.items()):
            print(f"  {extension[1:].upper()}: {count}")
    else:
        print("  (none)")
    print("Unsupported:")
    if unsupported_counts:
        for extension, count in sorted(unsupported_counts.items()):
            print(f"  {extension[1:].upper()}: {count}")
    else:
        print("  (none)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="V2 structured data extraction for ONE course (inventory-driven, output-existence state).",
    )
    parser.add_argument(
        "--course-id",
        required=True,
        help="the course id, e.g. mytimes-47164 (must exist in course-data/metadata/courses.json)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process at most N documents (one-document test mode: --limit 1)",
    )
    parser.add_argument(
        "--document-id",
        default=None,
        help="process only this document (must be a supported type in the inventory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-extract documents whose v2 output already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be extracted (incl. LibreOffice conversions) without doing it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_extraction(args.course_id, limit=args.limit,
                          document_id=args.document_id, force=args.force,
                          dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
