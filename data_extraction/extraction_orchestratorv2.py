#!/usr/bin/env python3
"""Course extraction orchestrator — V2 semantic extraction.

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

    inventory (documents list) → extension == ".pdf"? → output exists? → extract
        │                                                        │
        │ (unsupported formats stay untouched)                  └── exists → skip
        │                                                           missing → extract
        └── ... with --force the output-existence check is skipped

CLI:

    python3 data_extraction/extraction_orchestratorv2.py --course-id mytimes-47164
    python3 data_extraction/extraction_orchestratorv2.py --course-id mytimes-47164 --force
    python3 data_extraction/extraction_orchestratorv2.py \
        --course-id mytimes-47164 --document-id mytimes-47164-group-assignment-group-assignment-pdf

Status: IMPLEMENTED (PDF-only, v2 experimental). Part of the data_extraction stage.
"""

from __future__ import annotations

import argparse
import sys
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
from extractors.pdf_extractorv2 import PdfExtractorV2

REGISTRY_FILENAME = "courses.json"
INVENTORY_FILENAME = "file_inventory.json"


def run_extraction(course_id: str, data_root: Path | None = None,
                   limit: int | None = None, document_id: str | None = None,
                   force: bool = False) -> int:
    """Run V2 semantic extraction for ONE course. Returns a process exit code.

    ``data_root`` defaults to the repo-anchored ``course-data/``; tests pass a
    temporary root. ``limit`` caps the number of PDFs processed. ``document_id``
    restricts processing to one document. ``force`` re-extracts documents whose
    v2 output already exists.
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

    # STEP 3 — v2 candidates are ALL PDFs in the inventory, regardless of the
    # inventory's processing_status (that status reflects v1).
    pdf_docs = [d for d in documents if (d.get("extension") or "").lower() == ".pdf"]
    unsupported = [d for d in documents if (d.get("extension") or "").lower() != ".pdf"]
    candidates = pdf_docs

    if document_id:
        targeted = [d for d in candidates if d.get("document_id") == document_id]
        if not targeted:
            existing = next((d for d in documents if d.get("document_id") == document_id), None)
            if existing is None:
                print(f"ERROR: document_id '{document_id}' not found in the inventory.", file=sys.stderr)
                return EXIT_FAILURE
            print(f"Document '{document_id}' is not a PDF supported by v2; nothing to do.")
            candidates = []
        else:
            candidates = targeted

    if limit is not None and limit > 0:
        candidates = candidates[:limit]

    print("Documents:")
    print(f"Total: {len(documents)}")
    print(f"PDF: {len(pdf_docs)}")
    print(f"Unsupported (skipped by v2): {len(unsupported)}")
    print()

    # STEP 4 — split candidates by whether their v2 output already exists.
    already_extracted = 0
    to_process: list[tuple[dict, Path]] = []
    for doc in candidates:
        output_path = v2_root / output_path_for_document(doc.get("relative_path"))
        if output_path.is_file() and not force:
            already_extracted += 1
        else:
            to_process.append((doc, output_path))

    extracted_count = 0
    failed_count = 0

    print("Processing V2 PDFs:")
    total = len(to_process)
    for index, (doc, output_path) in enumerate(to_process, start=1):
        relative_path = doc.get("relative_path")
        print(f"[{index}/{total}] {relative_path}")
        raw_path = raw_root / relative_path
        if not raw_path.is_file():
            print(f"  failed: raw file missing: {raw_path}")
            failed_count += 1
            continue
        try:
            result = PdfExtractorV2().extract(raw_path, doc)
            write_json_atomic(output_path, result)
            extracted_count += 1
        except Exception as exc:  # noqa: BLE001 — one bad document must never abort the batch
            print(f"  failed: {exc}")
            failed_count += 1

    print()
    print("Extraction V2 complete.")
    print(f"PDF files found: {len(pdf_docs)}")
    print(f"PDF files already extracted (v2): {already_extracted}")
    print(f"PDF files extracted now: {extracted_count}")
    print(f"PDF files failed: {failed_count}")
    print(f"Unsupported files skipped: {len(unsupported)}")
    print("Note: the inventory was not modified; v2 state lives in extracted_v2/.")
    return 0


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
        help="process at most N PDFs (one-document test mode: --limit 1)",
    )
    parser.add_argument(
        "--document-id",
        default=None,
        help="process only this document (must be a PDF in the inventory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-extract documents whose v2 output already exists",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_extraction(args.course_id, limit=args.limit,
                          document_id=args.document_id, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
