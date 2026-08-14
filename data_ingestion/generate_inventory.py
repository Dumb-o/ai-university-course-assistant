#!/usr/bin/env python3
"""Course-specific file inventory generator (course-centric architecture).

Each invocation processes EXACTLY ONE course directory:

    python3 generate_inventory.py --course-dir course-data/raw/mytimes-47164

Behavior (one command = one course):

* The course id is derived from the directory name (``mytimes-47164``) — never
  passed by the user.
* Course metadata (course_name / course_url) is read from the scraper's
  ``course_metadata.json`` handoff file inside the course directory, so the
  user does not repeat the URL or id.
* Produces a course-specific inventory at
  ``course-data/inventories/<course_id>/file_inventory.json`` containing ONLY
  that course's files, with relative paths relative to the course directory and
  document ids prefixed with the course id.
* Registers / updates the course in the central registry
  ``course-data/metadata/courses.json`` (no duplicate entries).
* On repeat runs it is idempotent: unchanged files keep their processing
  status (matched by SHA-256), new files are added as ``pending``, and changed
  files are detected via their hash and reset to ``pending`` for reprocessing.
* The legacy global inventory at ``course-data/metadata/file_inventory.json``
  (if present) is never treated as canonical: its per-hash processing state is
  carried over once, then the file is archived under
  ``course-data/metadata/archive/``.

The script is strictly READ-ONLY over the downloaded course documents: it never
modifies, renames, moves, or re-encodes them.

Status: IMPLEMENTED. Part of the data_ingestion layer.
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
GENERATOR_NAME = "course_file_inventory"
GENERATOR_VERSION = "2.0.0"

REGISTRY_SCHEMA_VERSION = "1.0"
REGISTRY_FILENAME = "courses.json"
COURSE_METADATA_FILENAME = "course_metadata.json"
INVENTORY_FILENAME = "file_inventory.json"

# Legacy (global) inventory location — read once for migration, then archived.
LEGACY_INVENTORY_DIR = "metadata"
LEGACY_ARCHIVE_DIR = "metadata/archive"
LEGACY_ARCHIVE_NAME = "file_inventory.global.json"

# Files that are metadata handoffs, never documents.
METADATA_HANDOFF_FILENAMES = {COURSE_METADATA_FILENAME}

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

# A course id like "mytimes-47164" -> its MyTIMES course URL.
COURSE_ID_RE = re.compile(r"^[a-z0-9]+-(\d+)$")

# Cap for readable document_id slugs. Longer slugs get a short hash suffix so
# ids stay unique and manageable.
DOC_ID_MAX_LEN = 180

log = logging.getLogger("course_file_inventory")


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


def find_data_root(course_dir: Path) -> Path | None:
    """Return the course-data root owning ``course_dir``, or None.

    A course directory must live at ``<root>/raw/<course_id>``, i.e. its parent
    folder must be named ``raw``.
    """
    parent = course_dir.resolve().parent
    if parent.name == "raw":
        return parent.parent
    return None


def course_id_from_dirname(course_dir: Path) -> str:
    """The course id is the directory name, e.g. 'course-data/raw/mytimes-47164'
    -> 'mytimes-47164'."""
    return course_dir.name


def course_id_to_url(course_id: str) -> str | None:
    """Reconstruct the MyTIMES course URL from a course id when no manifest
    exists (e.g. 'mytimes-47164' -> '.../course/view.php?id=47164')."""
    m = COURSE_ID_RE.match(course_id or "")
    if m:
        return f"https://mytimes.taylors.edu.my/course/view.php?id={m.group(1)}"
    return None


def load_json_file(path: Path) -> dict | list | None:
    """Load a JSON file, returning None (with a warning) if unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # FileNotFoundError, JSONDecodeError, ...
        log.warning("cannot read %s: %s", path, exc)
        return None


def load_course_metadata(course_dir: Path) -> dict:
    """Read the scraper's course_metadata.json handoff, or return {}."""
    data = load_json_file(course_dir / COURSE_METADATA_FILENAME)
    return data if isinstance(data, dict) else {}


def load_legacy_global_inventory(data_root: Path) -> tuple[dict, Path] | None:
    """Load the legacy global inventory file, if it still exists.

    Returns (legacy_docs_by_hash, path) or None. The legacy file is NOT the
    canonical inventory anymore — it is only a one-time migration source.
    """
    path = data_root / LEGACY_INVENTORY_DIR / INVENTORY_FILENAME
    if not path.is_file():
        return None
    data = load_json_file(path)
    if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
        log.warning("legacy global inventory at %s is malformed; ignoring", path)
        return None
    by_hash: dict[str, str] = {}
    for doc in data["documents"]:
        if isinstance(doc, dict) and doc.get("content_hash"):
            by_hash[doc["content_hash"]] = doc.get("processing_status", "pending")
    return by_hash, path


def archive_legacy_global_inventory(legacy_path: Path, data_root: Path) -> Path | None:
    """Move the legacy global inventory out of the canonical path (archive).

    The file is preserved (never deleted), just relocated so it can no longer
    be mistaken for the canonical inventory.
    """
    if not legacy_path.is_file():
        return None
    archive_dir = data_root / LEGACY_ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / LEGACY_ARCHIVE_NAME
    if not dest.exists():
        dest.write_bytes(legacy_path.read_bytes())
    legacy_path.unlink()
    log.info("archived legacy global inventory: %s -> %s", legacy_path, dest)
    return dest


def load_previous_course_inventory(inventory_path: Path) -> dict[str, dict]:
    """Load the previous course-specific inventory as a map of
    relative_path -> {"content_hash", "processing_status"} (for status carry-over)."""
    data = load_json_file(inventory_path)
    if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
        return {}
    previous: dict[str, dict] = {}
    for doc in data["documents"]:
        if isinstance(doc, dict) and doc.get("relative_path"):
            previous[doc["relative_path"]] = {
                "content_hash": doc.get("content_hash"),
                "processing_status": doc.get("processing_status", "pending"),
            }
    return previous


def make_document_id(course_id: str, relative_path: str, used_ids: set[str]) -> str:
    """Deterministic, readable, unique document_id: ``<course_id>-<slug>``.

    The slug comes from the relative path, so ids are stable across re-runs and
    never collide with other courses (the course id is always present). On slug
    collisions (e.g. ``a&b.txt`` vs ``a b.txt``) a short SHA-256 of the relative
    path is appended.
    """
    base = slugify(relative_path) or "doc"
    document_id = f"{course_id}-{base}"
    if len(document_id) > DOC_ID_MAX_LEN:
        document_id = document_id[:DOC_ID_MAX_LEN].rstrip("-")
    if document_id in used_ids:
        suffix = hashlib.sha256(relative_path.encode("utf-8", "surrogateescape")).hexdigest()[:8]
        document_id = f"{document_id}-{suffix}"
    used_ids.add(document_id)
    return document_id


def resolve_processing_status(relative_path, content_hash, previous, legacy_statuses):
    """Decide the processing_status for a file.

    Priority:
      1. Hash matches the previous course inventory -> keep its status.
      2. Hash matches the legacy global inventory (one-time migration) -> keep.
      3. Anything else (new or changed file) -> 'pending' (ready to process).
    """
    if content_hash is None:
        return "pending"
    prev = previous.get(relative_path)
    if prev and prev.get("content_hash") == content_hash:
        return prev.get("processing_status", "pending")
    if legacy_statuses and content_hash in legacy_statuses:
        return legacy_statuses[content_hash]
    return "pending"


def describe_file(course_id, path: Path, relative_path: str, used_ids: set[str],
                  previous: dict[str, dict], legacy_statuses: dict[str, str]) -> dict:
    """Build the inventory record for one file without touching its contents."""
    parts = relative_path.split("/")
    section = parts[0] if len(parts) >= 2 else None

    size = None
    try:
        size = path.stat().st_size
    except OSError as exc:
        log.warning("cannot stat %r: %s", relative_path, exc)

    content_hash, hash_error = sha256_of_file(path)
    if hash_error:
        log.warning("cannot hash %r: %s", relative_path, hash_error)

    return {
        "document_id": make_document_id(course_id, relative_path, used_ids),
        "course_id": course_id,
        "filename": path.name,
        "relative_path": relative_path,
        "extension": path.suffix.lower() or None,
        "mime_type": detect_mime(path),
        "size_bytes": size,
        "section": section,
        "week": infer_week(section),
        "source_directory": "/".join(parts[:-1]) if len(parts) > 1 else None,
        "content_hash": content_hash,
        "hash_algorithm": "sha256",
        "hash_error": hash_error,
        "processing_status": resolve_processing_status(
            relative_path, content_hash, previous, legacy_statuses),
    }


def build_course_inventory(course_dir: Path, course_id: str, course_name: str,
                           previous: dict[str, dict] | None = None,
                           legacy_statuses: dict[str, str] | None = None) -> dict:
    """Scan ONE course directory and return its course-specific inventory.

    ``relative_path`` values are relative to ``course_dir`` (the course
    identity already lives in ``course_id``). Metadata handoff files (e.g.
    ``course_metadata.json``) are never treated as documents.
    """
    previous = previous or {}
    legacy_statuses = legacy_statuses or {}
    used_ids: set[str] = set()
    documents: list[dict] = []

    def on_walk_error(exc: OSError) -> None:
        log.warning("cannot walk %r: %s", exc.filename, exc)

    for dirpath, dirnames, filenames in os.walk(course_dir, onerror=on_walk_error):
        dirnames.sort()
        dir_path = Path(dirpath)
        for filename in sorted(filenames):
            if filename in METADATA_HANDOFF_FILENAMES:
                continue
            file_path = dir_path / filename
            try:
                relative_path = file_path.relative_to(course_dir).as_posix()
            except ValueError:
                relative_path = filename
            documents.append(describe_file(course_id, file_path, relative_path,
                                           used_ids, previous, legacy_statuses))

    documents.sort(key=lambda doc: doc["relative_path"])

    file_types: dict[str, int] = {}
    sections: dict[str, int] = {}
    weeks: dict[str, int] = {}
    total_bytes = 0
    unreadable: list[str] = []
    added = changed = unchanged = 0
    for doc in documents:
        ext = doc["extension"] or "(none)"
        file_types[ext] = file_types.get(ext, 0) + 1
        if doc["section"]:
            sections[doc["section"]] = sections.get(doc["section"], 0) + 1
        if doc["week"]:
            weeks[doc["week"]] = weeks.get(doc["week"], 0) + 1
        if doc["size_bytes"] is not None:
            total_bytes += doc["size_bytes"]
        if doc["hash_error"]:
            unreadable.append(doc["relative_path"])

        prev = previous.get(doc["relative_path"])
        if prev and prev.get("content_hash") == doc["content_hash"]:
            unchanged += 1
        elif doc["content_hash"] and doc["content_hash"] in legacy_statuses:
            unchanged += 1
        elif prev is not None:
            changed += 1
        else:
            added += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "inventory_type": "course_file_inventory",
        "course": {
            "course_id": course_id,
            "course_name": course_name,
        },
        "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "course_id": course_id,
            "total_files": len(documents),
            "total_bytes": total_bytes,
            "added_files": added,
            "changed_files": changed,
            "unchanged_files": unchanged,
            "file_types": file_types,
            "sections": sections,
            "weeks": weeks,
            "unreadable_files": unreadable,
        },
        "documents": documents,
    }


def load_registry(registry_path: Path) -> dict:
    """Load the central course registry, initialising an empty one if needed."""
    data = load_json_file(registry_path)
    if isinstance(data, dict) and isinstance(data.get("courses"), list):
        return data
    return {"schema_version": REGISTRY_SCHEMA_VERSION, "courses": []}


def register_course(registry_path: Path, entry: dict) -> dict:
    """Add a course to (or update it in) the central registry. Never duplicates.

    Matching is by ``course_id``; an existing entry is updated in place and the
    registry is written back deterministically (courses sorted by course_id).
    """
    registry = load_registry(registry_path)
    registry.setdefault("schema_version", REGISTRY_SCHEMA_VERSION)
    courses = registry.setdefault("courses", [])

    replaced = False
    for existing in courses:
        if existing.get("course_id") == entry["course_id"]:
            existing.update(entry)
            replaced = True
            break
    if not replaced:
        courses.append(entry)

    courses.sort(key=lambda c: c.get("course_id", ""))
    write_json(registry_path, registry)
    return registry


def write_json(path: Path, data: dict) -> None:
    """Write JSON with deterministic key order and UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def write_inventory(inventory: dict, output_path: Path) -> None:
    """Write the inventory JSON with deterministic key order and UTF-8."""
    write_json(output_path, inventory)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a course-specific file inventory for ONE course.",
    )
    parser.add_argument(
        "--course-dir",
        required=True,
        type=Path,
        help="path to ONE course raw directory, e.g. course-data/raw/mytimes-47164 "
             "(the course id is derived from the directory name)",
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

    course_dir = args.course_dir
    if not course_dir.is_dir():
        log.error("course directory does not exist or is not a directory: %s", course_dir)
        return 1

    data_root = find_data_root(course_dir)
    if data_root is None:
        log.error("course directory must live under <data-root>/raw/, got: %s", course_dir)
        return 1

    course_id = course_id_from_dirname(course_dir)
    metadata = load_course_metadata(course_dir)
    course_name = metadata.get("course_name") or course_id
    course_url = metadata.get("course_url") or course_id_to_url(course_id)

    registry_path = data_root / "metadata" / REGISTRY_FILENAME
    inventory_path = data_root / "inventories" / course_id / INVENTORY_FILENAME

    previous = load_previous_course_inventory(inventory_path)

    legacy = load_legacy_global_inventory(data_root)
    legacy_statuses = legacy[0] if legacy else {}
    if legacy:
        log.info("found legacy global inventory (%d hashes) — migrating state", len(legacy_statuses))

    log.info("scanning course %s (%s)", course_id, course_dir)
    inventory = build_course_inventory(course_dir, course_id, course_name,
                                       previous, legacy_statuses)

    write_inventory(inventory, inventory_path)
    log.info("wrote %d documents to %s", len(inventory["documents"]), inventory_path)

    entry = {
        "course_id": course_id,
        "course_name": course_name,
        "course_url": course_url,
        "raw_path": f"raw/{course_id}",
        "inventory_path": f"inventories/{course_id}/{INVENTORY_FILENAME}",
        "status": "active",
    }
    register_course(registry_path, entry)
    log.info("registered course in %s", registry_path)

    if legacy:
        archive_legacy_global_inventory(legacy[1], data_root)

    summary = inventory["summary"]
    print("\n=== Course inventory summary ===")
    print(f"course id:  {course_id}")
    print(f"course name:{course_name}")
    print(f"course url: {course_url}")
    print(f"course dir: {course_dir}")
    print(f"inventory:  {inventory_path}")
    print(f"registry:   {registry_path}")
    print(f"files:      {summary['total_files']}")
    print(f"bytes:      {summary['total_bytes']:,}")
    print(f"added:      {summary['added_files']}")
    print(f"changed:    {summary['changed_files']}")
    print(f"unchanged:  {summary['unchanged_files']}")
    print("file types:")
    for ext, count in summary["file_types"].items():
        print(f"  - {ext or '(no extension)'}: {count}")
    print("sections:")
    for section, count in summary["sections"].items():
        print(f"  - {section}: {count}")
    if summary["unreadable_files"]:
        print("unreadable/problematic:")
        for rel in summary["unreadable_files"]:
            print(f"  - {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
