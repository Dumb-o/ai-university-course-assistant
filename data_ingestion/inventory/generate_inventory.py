#!/usr/bin/env python3
"""File inventory generator for the course-data/raw/ corpus.

Recursively scans a raw-data directory and produces a machine-readable JSON
inventory (manifest) describing every file on disk. It is strictly READ-ONLY:
it never modifies, renames, moves, or re-encodes the source files.

The inventory is the authoritative manifest for every downstream ingestion
stage (extraction, normalization, classification, metadata, chunking). Each
downstream stage references documents through their ``document_id`` and uses
``content_hash`` (SHA-256 of the file bytes) to detect duplicates, unchanged
files, modified files, and already-processed files.

Usage:
    python3 generate_inventory.py [--raw-root ../../course-data/raw]
                                  [--output ../../course-data/metadata/file_inventory.json]

Status: IMPLEMENTED. This is the first component of the data_ingestion layer.
"""

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0"
GENERATOR_NAME = "file_inventory"
GENERATOR_VERSION = "1.0.0"

# Human-friendly extension -> MIME mapping, so well-known values do not depend
# on the host's platform mimetypes database. Files with unknown extensions fall
# back to ``mimetypes.guess_type`` and may legitimately be None.
MIME_OVERRIDES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".dot": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".zip": "application/zip",
}

# Section folders that look like a numbered week, e.g. "Week 3".
WEEK_RE = re.compile(r"^week\s*\d+$", re.IGNORECASE)

# Cap for readable document_id slugs. Longer slugs get a short hash suffix so
# ids stay unique and manageable.
DOC_ID_MAX_LEN = 160

log = logging.getLogger("file_inventory")


def slugify(text: str) -> str:
    """Turn an arbitrary filename/path into a safe, readable, lowercase slug.

    Handles spaces, parentheses, unicode (NFKD + ASCII-folded), HTML-encoded
    names such as ``Class &amp; Object Modeling`` and any other punctuation.
    Non-alphanumeric runs collapse to ``-``.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return text.strip("-")


def sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str | None, str | None]:
    """Return (sha256_hex, error). error is None on success.

    Streams the file so large documents are hashed without loading fully into
    memory. On any I/O error returns (None, str(exc)) so the caller can keep
    the file in the inventory with an error marker instead of aborting.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        return None, str(exc)
    return digest.hexdigest(), None


def detect_mime(path: Path) -> str | None:
    """Best-effort MIME type from the extension (override table, then mimetypes)."""
    ext = path.suffix.lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(path.name)
    return mime


def infer_week(section: str | None) -> str | None:
    """Return the section name when it is a numbered week, else None."""
    if section and WEEK_RE.match(section):
        return section
    return None


def make_document_id(relative_path: str, used_ids: set[str]) -> str:
    """Deterministic, readable, unique document_id derived from the path.

    Base = slugified relative path (including extension). If the slug is empty
    or already used (slug collisions are possible, e.g. ``a&b.txt`` vs
    ``ab.txt``), a short SHA-256 of the relative path is appended.
    """
    base = slugify(relative_path) or "doc"
    if len(base) > DOC_ID_MAX_LEN:
        base = base[:DOC_ID_MAX_LEN].rstrip("-")
    document_id = base
    if document_id in used_ids:
        suffix = hashlib.sha256(relative_path.encode("utf-8", "surrogateescape")).hexdigest()[:8]
        document_id = f"{base}-{suffix}"
    used_ids.add(document_id)
    return document_id


def describe_file(path: Path, relative_path: str, used_ids: set[str]) -> dict:
    """Build the inventory record for one file without touching its contents."""
    parts = relative_path.split("/")
    course = parts[0] if len(parts) >= 2 else None
    section = parts[1] if len(parts) >= 3 else None

    size = None
    try:
        size = path.stat().st_size
    except OSError as exc:
        log.warning("cannot stat %r: %s", relative_path, exc)

    content_hash, hash_error = sha256_of_file(path)
    if hash_error:
        log.warning("cannot hash %r: %s", relative_path, hash_error)

    return {
        "document_id": make_document_id(relative_path, used_ids),
        "filename": path.name,
        "relative_path": relative_path,
        "extension": path.suffix.lower() or None,
        "mime_type": detect_mime(path),
        "size_bytes": size,
        "course": course,
        "section": section,
        "week": infer_week(section),
        "source_directory": "/".join(parts[:-1]) if len(parts) > 1 else None,
        "content_hash": content_hash,
        "hash_algorithm": "sha256",
        "hash_error": hash_error,
        "processing_status": "pending",
    }


def build_inventory(raw_root: Path) -> dict:
    """Recursively scan ``raw_root`` and return the full inventory document.

    Walk is deterministic (sorted dirs/files), handles undecodable filenames and
    unreadable entries gracefully, and never modifies the source tree.
    """
    used_ids: set[str] = set()
    documents: list[dict] = []

    def on_walk_error(exc: OSError) -> None:
        log.warning("cannot walk %r: %s", exc.filename, exc)

    for dirpath, dirnames, filenames in os.walk(raw_root, onerror=on_walk_error):
        dirnames.sort()
        dir_path = Path(dirpath)
        for filename in sorted(filenames):
            file_path = dir_path / filename
            try:
                relative_path = file_path.relative_to(raw_root).as_posix()
            except ValueError:
                relative_path = filename
            documents.append(describe_file(file_path, relative_path, used_ids))

    documents.sort(key=lambda doc: doc["relative_path"])

    courses = {}
    file_types = {}
    total_bytes = 0
    unreadable = []
    for doc in documents:
        courses[doc["course"]] = courses.get(doc["course"], 0) + 1
        ext = doc["extension"] or "(none)"
        file_types[ext] = file_types.get(ext, 0) + 1
        if doc["size_bytes"] is not None:
            total_bytes += doc["size_bytes"]
        if doc["hash_error"]:
            unreadable.append(doc["relative_path"])

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION},
        "inventory_type": "file_inventory",
        "root": str(raw_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_files": len(documents),
            "total_bytes": total_bytes,
            "courses": courses,
            "file_types": file_types,
            "unreadable_files": unreadable,
        },
        "documents": documents,
    }


def write_inventory(inventory: dict, output_path: Path) -> None:
    """Write the inventory JSON with deterministic key order and UTF-8."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a file inventory (manifest) for course-data/raw/.",
    )
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "course-data" / "raw",
        help="raw course-data directory to scan (default: ../../course-data/raw)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "course-data" / "metadata" / "file_inventory.json",
        help="output JSON path (default: ../../course-data/metadata/file_inventory.json)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.raw_root.is_dir():
        log.error("raw root does not exist or is not a directory: %s", args.raw_root)
        return 1

    log.info("scanning %s", args.raw_root)
    inventory = build_inventory(args.raw_root)

    write_inventory(inventory, args.output)
    log.info("wrote %d documents to %s", len(inventory["documents"]), args.output)

    summary = inventory["summary"]
    print("\n=== File inventory summary ===")
    print(f"root:        {args.raw_root}")
    print(f"output:      {args.output}")
    print(f"files:       {summary['total_files']}")
    print(f"bytes:       {summary['total_bytes']:,}")
    print(f"courses:     {len(summary['courses'])}")
    for course, count in summary["courses"].items():
        print(f"  - {course}: {count}")
    print("file types:")
    for ext, count in summary["file_types"].items():
        print(f"  - {ext or '(no extension)'}: {count}")
    if summary["unreadable_files"]:
        print("unreadable/problematic:")
        for rel in summary["unreadable_files"]:
            print(f"  - {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
