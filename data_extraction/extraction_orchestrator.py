#!/usr/bin/env python3
"""Course extraction orchestrator — structured data extraction stage.

Drives structured extraction for ONE course, using the course's inventory as
the source of truth. The only required course-selection input is the course id
(``mytimes-<id>``); everything else is derived from the project architecture:

    course-data/
    ├── metadata/courses.json                    central course registry
    ├── inventories/<course_id>/file_inventory.json   source of truth
    ├── raw/<course_id>/                         READ-ONLY source files
    └── extracted/<course_id>/                   output (mirrors raw layout)

Per document the decision chain is:

    inventory → pending? → extension → registered extractor? → extract / skip

* ``pending`` is the only status processed. ``extracted``, ``extraction_failed``
  and other statuses are left alone.
* ``.pdf`` is the only supported extension right now. Unsupported files
  (DOCX/PPTX/XLSX/images, ...) are skipped and remain ``pending`` — they are
  waiting for a future extractor, they are not errors.
* The inventory's ``relative_path`` is authoritative: raw files are resolved as
  ``raw/<course_id>/<relative_path>`` and outputs are written to
  ``extracted/<course_id>/<relative_path with .json suffix>`` so the course's
  original directory structure is preserved.
* Raw files are never modified.
* The inventory is written back atomically (temp file → fsync → replace) after
  each document, so a crash never corrupts it.
* Re-running is idempotent: already-``extracted`` documents are not reprocessed.

Usage:

    python3 data_extraction/extraction_orchestrator.py --course-id mytimes-47164
    python3 data_extraction/extraction_orchestrator.py --course-id mytimes-47164 --limit 1
    python3 data_extraction/extraction_orchestrator.py --course-id mytimes-47164 --document-id <document_id>

Status: IMPLEMENTED (PDF-only first version). Part of the data_extraction stage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from extractors import get_extractor

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = REPO_ROOT / "course-data"

REGISTRY_FILENAME = "courses.json"
INVENTORY_FILENAME = "file_inventory.json"

PROCESSING_PENDING = "pending"
PROCESSING_EXTRACTED = "extracted"
PROCESSING_FAILED = "extraction_failed"

# Exit code used for any hard failure (unknown course, missing inventory, ...).
EXIT_FAILURE = 1


def load_json(path: Path) -> dict | None:
    """Load a JSON dict, returning None (with a warning) if unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        return None


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON safely: temp file in the same directory → fsync → replace.

    ``os.replace`` is atomic on the same filesystem, so a crash mid-write never
    leaves a truncated/corrupt file at the final path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_registry(data_root: Path) -> list[dict]:
    """Load the central course registry as a list of course entries."""
    registry = load_json(data_root / "metadata" / REGISTRY_FILENAME)
    if isinstance(registry, dict) and isinstance(registry.get("courses"), list):
        return registry["courses"]
    return []


def find_course(registry: list[dict], course_id: str) -> dict | None:
    """The registry entry whose ``course_id`` matches, else None."""
    for course in registry:
        if isinstance(course, dict) and course.get("course_id") == course_id:
            return course
    return None


def load_inventory(inventory_path: Path) -> dict:
    """Load a course inventory, failing clearly on any problem."""
    if not inventory_path.is_file():
        print(f"ERROR: inventory not found: {inventory_path}", file=sys.stderr)
        raise FileNotFoundError(inventory_path)
    inventory = load_json(inventory_path)
    if not isinstance(inventory, dict) or not isinstance(inventory.get("documents"), list):
        print(f"ERROR: malformed inventory (missing documents list): {inventory_path}", file=sys.stderr)
        raise ValueError(f"malformed inventory: {inventory_path}")  # noqa: TRY004 — data, not type, error
    return inventory


def output_path_for_document(relative_path: str) -> Path:
    """Map a raw relative path to the extracted relative path (``.pdf`` -> ``.json``)."""
    path = Path(relative_path)
    if path.suffix:
        return path.with_suffix(".json")
    return Path(str(path) + ".json")


def write_inventory(inventory_path: Path, inventory: dict) -> None:
    """Atomically write the inventory back."""
    write_json_atomic(inventory_path, inventory)


def _fail_document(doc: dict, inventory_path: Path, inventory: dict, error: str) -> None:
    """Mark a document ``extraction_failed``, record the error, write back."""
    doc["processing_status"] = PROCESSING_FAILED
    doc["extraction_error"] = error
    write_inventory(inventory_path, inventory)


def _extract_document(doc: dict, raw_root: Path, extracted_root: Path,
                      inventory_path: Path, inventory: dict) -> None:
    """Extract one document end-to-end.

    Raises on any failure so the caller records the error and continues.
    On success the extracted JSON is written and the status becomes
    ``extracted``. The inventory is written back after this document, so a
    crash later never loses this document's progress.
    """
    relative_path = doc.get("relative_path")
    if not relative_path:
        raise ValueError("document record has no relative_path")

    raw_path = raw_root / relative_path
    if not raw_path.is_file():
        raise FileNotFoundError(f"raw file missing: {raw_path}")

    extractor = get_extractor(doc.get("extension"))
    if extractor is None:
        # Unsupported extension: skip and leave pending (never mark failed).
        return

    result = extractor().extract(raw_path, doc)

    output_path = extracted_root / output_path_for_document(relative_path)
    write_json_atomic(output_path, result)

    doc["processing_status"] = PROCESSING_EXTRACTED
    write_inventory(inventory_path, inventory)


def run_extraction(course_id: str, data_root: Path | None = None,
                   limit: int | None = None, document_id: str | None = None) -> int:
    """Run structured extraction for ONE course. Returns a process exit code.

    ``data_root`` defaults to the repo-anchored ``course-data/``; tests pass a
    temporary root. ``limit`` caps the number of pending PDFs processed (one-
    document test mode). ``document_id`` restricts processing to one document.
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
    extracted_root = data_root / "extracted" / course_id

    course_name = course.get("course_name") or course_id
    print(f"Course: {course_name}")
    print(f"Course ID: {course_id}")
    print()
    print(f"Inventory: {inventory_path}")
    print(f"Raw: {raw_root}/")
    print(f"Extracted: {extracted_root}/")
    print()

    if not raw_root.is_dir():
        print(f"WARNING: raw course directory does not exist: {raw_root}")

    # STEP 3 — pending-only candidates.
    pending = [d for d in documents if d.get("processing_status") == PROCESSING_PENDING]
    pdf_docs = [d for d in documents if (d.get("extension") or "").lower() == ".pdf"]
    unsupported = [d for d in documents if get_extractor(d.get("extension")) is None]
    pdf_pending = [d for d in pdf_docs if d.get("processing_status") == PROCESSING_PENDING]

    # STEP 4 — route by extension: only pending documents whose extension has a
    # registered extractor are candidates for this run.
    candidates = [d for d in pending if get_extractor(d.get("extension")) is not None]

    if document_id:
        targeted = [d for d in candidates if d.get("document_id") == document_id]
        if not targeted:
            existing = next((d for d in documents if d.get("document_id") == document_id), None)
            if existing is None:
                print(f"ERROR: document_id '{document_id}' not found in the inventory.", file=sys.stderr)
                return EXIT_FAILURE
            print(f"Document '{document_id}' is already {existing.get('processing_status')}; nothing to do.")
            candidates = []
        else:
            candidates = targeted

    if limit is not None and limit > 0:
        candidates = candidates[:limit]

    print("Documents:")
    print(f"Total: {len(documents)}")
    print(f"Pending: {len(pending)}")
    print(f"PDF: {len(pdf_docs)}")
    print(f"Unsupported: {len(unsupported)}")
    print()

    # STEP 5-9 — extract pending PDFs, mirror relative paths, update statuses.
    already_extracted = len([d for d in pdf_docs if d.get("processing_status") == PROCESSING_EXTRACTED])
    extracted_count = 0
    failed_count = 0

    print("Processing PDF:")
    total_candidates = len(candidates)
    for index, doc in enumerate(candidates, start=1):
        print(f"[{index}/{total_candidates}] {doc.get('relative_path')}")
        try:
            _extract_document(doc, raw_root, extracted_root, inventory_path, inventory)
            extracted_count += 1
        except Exception as exc:  # noqa: BLE001 — one bad document must never abort the batch
            _fail_document(doc, inventory_path, inventory, str(exc))
            print(f"  failed: {exc}")
            failed_count += 1

    print()
    print("Extraction complete.")
    print(f"PDF files found: {len(pdf_docs)}")
    print(f"PDF files pending: {len(pdf_pending)}")
    print(f"PDF files extracted: {extracted_count}")
    print(f"PDF files already extracted: {already_extracted}")
    print(f"PDF files failed: {failed_count}")
    print(f"Unsupported files skipped: {len(unsupported)}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Structured data extraction for ONE course (inventory-driven).",
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
        help="process at most N pending PDFs (one-document test mode: --limit 1)",
    )
    parser.add_argument(
        "--document-id",
        default=None,
        help="process only this pending document (must be a supported PDF)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_extraction(args.course_id, limit=args.limit, document_id=args.document_id)


if __name__ == "__main__":
    sys.exit(main())
