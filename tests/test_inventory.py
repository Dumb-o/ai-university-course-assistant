"""Tests for the course-specific file inventory generator (data_ingestion).

Runs without the scraper/browser harness — pure filesystem + logic tests.
Covers: one-course-per-invocation, course id from directory name, metadata
handoff, course-scoped document ids and relative paths, SHA-256 correctness,
status preservation on repeat runs, changed/new file detection, the central
course registry, legacy-global-inventory migration/archiving, and isolation
between coexisting course directories.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA_INGESTION = ROOT / "data_ingestion"
if str(DATA_INGESTION) not in sys.path:
    sys.path.insert(0, str(DATA_INGESTION))

from generate_inventory import (  # noqa: E402
    archive_legacy_global_inventory,
    build_course_inventory,
    course_id_from_dirname,
    course_id_to_url,
    detect_mime,
    find_data_root,
    infer_week,
    load_course_metadata,
    load_previous_course_inventory,
    main,
    make_document_id,
    register_course,
    sha256_of_file,
    slugify,
    write_inventory,
)

COURSE_TREE = {
    "Week 3": {
        "Lecture 3 - A &amp; B (Basics).pdf": b"%PDF-1.4 trickypdf\n",
        "Héllo Wörld 文件.txt": "unicode content".encode("utf-8"),
    },
    "General": {
        "Module Information (MI).pdf": b"%PDF-1.4 mi\n",
    },
    "MLO2 - Individual Assignment 30%": {
        "Rubric & Marking.xlsx": b"xlsx",
    },
    "Week 2": {
        "a&b.txt": b"aaa",
        "a b.txt": b"aab",
        "ab.txt": b"bbb",
    },
    "Week 10": {
        "Deeply Nested Doc.ppt": b"ppt",
    },
}


def make_tree(base: Path, tree: dict, prefix: Path | None = None) -> Path:
    for name, value in tree.items():
        cur = (prefix or base) / name
        if isinstance(value, dict):
            cur.mkdir(parents=True, exist_ok=True)
            make_tree(base, value, cur)
        else:
            cur.parent.mkdir(parents=True, exist_ok=True)
            cur.write_bytes(value)
    return base


@pytest.fixture
def data_root(tmp_path):
    return tmp_path


@pytest.fixture
def course_dir(data_root):
    course = data_root / "raw" / "mytimes-47164"
    make_tree(course, COURSE_TREE)
    return course


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    ("Week 3", "week-3"),
    ("Lecture 3 - A &amp; B (Basics).pdf", "lecture-3-a-amp-b-basics-pdf"),
    ("Héllo Wörld 文件.txt", "hello-world-txt"),
    ("a&b.txt", "a-b-txt"),
    ("", ""),
])
def test_slugify(value, expected):
    assert slugify(value) == expected


def test_course_id_from_dirname():
    assert course_id_from_dirname(Path("course-data/raw/mytimes-47164")) == "mytimes-47164"
    assert course_id_from_dirname(Path("raw/mytimes-123")) == "mytimes-123"


def test_course_id_to_url():
    assert course_id_to_url("mytimes-47164") == (
        "https://mytimes.taylors.edu.my/course/view.php?id=47164")
    assert course_id_to_url("not-a-course") is None


def test_find_data_root():
    root = Path("/tmp/course-data")
    assert find_data_root(root / "raw" / "mytimes-1") == root
    assert find_data_root(root / "something-else" / "mytimes-1") is None


def test_make_document_id_course_scoped_and_collision_free():
    used: set[str] = set()
    first = make_document_id("mytimes-47164", "Week 1/a&b.txt", used)
    second = make_document_id("mytimes-47164", "Week 1/a b.txt", used)
    third = make_document_id("mytimes-47165", "Week 1/a&b.txt", used)
    assert first != second
    assert first == "mytimes-47164-week-1-a-b-txt"
    assert second.startswith("mytimes-47164-week-1-a-b-txt-")
    # Same relative path in another course gets a distinct id (course id prefix).
    assert third.startswith("mytimes-47165-")
    assert third not in (first, second)
    assert len(used) == 3


def test_infer_week():
    assert infer_week("Week 3") == "Week 3"
    assert infer_week("week10") == "week10"
    assert infer_week("General") is None
    assert infer_week("MLO1 - Group Assignment 30%") is None
    assert infer_week(None) is None


def test_sha256_matches_known(course_dir):
    p = course_dir / "General" / "Module Information (MI).pdf"
    expected = hashlib.sha256(b"%PDF-1.4 mi\n").hexdigest()
    digest, error = sha256_of_file(p)
    assert error is None
    assert digest == expected


def test_detect_mime():
    assert detect_mime(Path("x.pdf")) == "application/pdf"
    assert detect_mime(Path("x.doc")) == "application/msword"
    assert detect_mime(Path("x.pptx")) == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert detect_mime(Path("x.unknownext")) is None


# --------------------------------------------------------------------------- #
# Course-specific inventory behaviour
# --------------------------------------------------------------------------- #
def test_build_course_inventory_shape_counts_and_scoping(course_dir):
    inv = build_course_inventory(course_dir, "mytimes-47164", "Software Design")
    docs = inv["documents"]
    assert inv["schema_version"] == "1.0"
    assert inv["inventory_type"] == "course_file_inventory"
    assert inv["course"]["course_id"] == "mytimes-47164"
    assert inv["course"]["course_name"] == "Software Design"
    assert len(docs) == 8  # 2 (Wk3) + 1 (General) + 1 (MLO2) + 3 (Wk2) + 1 (Wk10)
    assert inv["summary"]["total_files"] == 8
    assert inv["summary"]["total_bytes"] == sum(
        len(body) for section in COURSE_TREE.values() for body in section.values())
    assert set(inv["summary"]["file_types"]) == {".pdf", ".txt", ".xlsx", ".ppt"}
    assert set(inv["summary"]["sections"]) == {"Week 3", "General", "MLO2 - Individual Assignment 30%", "Week 2", "Week 10"}
    # Every document belongs to THIS course.
    assert all(d["course_id"] == "mytimes-47164" for d in docs)
    # relative_path is relative to the course dir (no course name / id prefix).
    assert all(not d["relative_path"].startswith("mytimes-47164") for d in docs)


def test_metadata_handoff_file_is_not_a_document(course_dir):
    (course_dir / "course_metadata.json").write_text(
        json.dumps({"course_id": "mytimes-47164"}), encoding="utf-8")
    inv = build_course_inventory(course_dir, "mytimes-47164", "x")
    assert len(inv["documents"]) == 8
    assert all(d["filename"] != "course_metadata.json" for d in inv["documents"])


def test_build_course_inventory_metadata_and_hashes(course_dir):
    docs = build_course_inventory(course_dir, "mytimes-47164", "x")["documents"]
    by_path = {d["relative_path"]: d for d in docs}

    doc = by_path["Week 3/Lecture 3 - A &amp; B (Basics).pdf"]
    assert doc["document_id"].startswith("mytimes-47164-")
    assert doc["section"] == "Week 3"
    assert doc["week"] == "Week 3"
    assert doc["source_directory"] == "Week 3"
    assert doc["mime_type"] == "application/pdf"
    assert doc["processing_status"] == "pending"
    assert doc["content_hash"] == hashlib.sha256(b"%PDF-1.4 trickypdf\n").hexdigest()
    assert doc["hash_algorithm"] == "sha256"
    assert doc["hash_error"] is None

    gen = by_path["General/Module Information (MI).pdf"]
    assert gen["week"] is None

    assignment = by_path["MLO2 - Individual Assignment 30%/Rubric & Marking.xlsx"]
    assert assignment["week"] is None
    assert assignment["section"] == "MLO2 - Individual Assignment 30%"


def test_document_ids_unique(course_dir):
    docs = build_course_inventory(course_dir, "mytimes-47164", "x")["documents"]
    ids = [d["document_id"] for d in docs]
    assert len(ids) == len(set(ids))


def test_every_relative_path_points_to_a_real_file(course_dir):
    docs = build_course_inventory(course_dir, "mytimes-47164", "x")["documents"]
    for d in docs:
        target = Path(d["relative_path"])
        assert (course_dir / target).is_file()


def test_unreadable_file_flagged_not_crashing(course_dir, data_root):
    dangling = course_dir / "Week 3" / "broken-link.txt"
    dangling.symlink_to(data_root / "does-not-exist.txt")
    inv = build_course_inventory(course_dir, "mytimes-47164", "x")
    broken = [d for d in inv["documents"] if d["filename"] == "broken-link.txt"]
    assert len(broken) == 1
    assert broken[0]["size_bytes"] is None
    assert broken[0]["content_hash"] is None
    assert broken[0]["hash_error"] is not None
    assert broken[0]["processing_status"] == "pending"
    assert broken[0]["relative_path"] in inv["summary"]["unreadable_files"]


# --------------------------------------------------------------------------- #
# Repeat runs: idempotency, status preservation, new/changed detection
# --------------------------------------------------------------------------- #
def test_unchanged_file_keeps_processing_status(course_dir):
    inv = build_course_inventory(course_dir, "mytimes-47164", "x")
    # Seed the "previous" state as if the file had already been extracted.
    doc = inv["documents"][0]
    previous = {doc["relative_path"]: {"content_hash": doc["content_hash"],
                                       "processing_status": "extracted"}}
    again = build_course_inventory(course_dir, "mytimes-47164", "x", previous)
    by_path = {d["relative_path"]: d for d in again["documents"]}
    assert by_path[doc["relative_path"]]["processing_status"] == "extracted"
    assert again["summary"]["unchanged_files"] == 1
    assert again["summary"]["added_files"] == 7
    assert again["summary"]["changed_files"] == 0


def test_changed_file_detected_via_hash(course_dir):
    inv = build_course_inventory(course_dir, "mytimes-47164", "x")
    doc = inv["documents"][0]
    previous = {doc["relative_path"]: {"content_hash": "0" * 64,
                                       "processing_status": "extracted"}}
    again = build_course_inventory(course_dir, "mytimes-47164", "x", previous)
    by_path = {d["relative_path"]: d for d in again["documents"]}
    assert by_path[doc["relative_path"]]["processing_status"] == "pending"
    assert again["summary"]["changed_files"] == 1


def test_new_file_added_as_pending(course_dir):
    inv = build_course_inventory(course_dir, "mytimes-47164", "x")
    previous = {d["relative_path"]: {"content_hash": d["content_hash"],
                                     "processing_status": "extracted"}
                for d in inv["documents"]}
    (course_dir / "Week 4").mkdir()
    (course_dir / "Week 4" / "New Material.pdf").write_bytes(b"%PDF-1.4 new\n")
    again = build_course_inventory(course_dir, "mytimes-47164", "x", previous)
    new_doc = [d for d in again["documents"] if d["filename"] == "New Material.pdf"][0]
    assert new_doc["processing_status"] == "pending"
    assert again["summary"]["added_files"] == 1
    assert again["summary"]["unchanged_files"] == 8


# --------------------------------------------------------------------------- #
# Central course registry
# --------------------------------------------------------------------------- #
def test_register_course_no_duplicates(data_root):
    registry_path = data_root / "metadata" / "courses.json"
    entry = {"course_id": "mytimes-47164", "course_name": "Software Design",
             "course_url": "https://mytimes.taylors.edu.my/course/view.php?id=47164",
             "raw_path": "raw/mytimes-47164",
             "inventory_path": "inventories/mytimes-47164/file_inventory.json",
             "status": "active"}
    register_course(registry_path, entry)
    register_course(registry_path, entry)
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    assert len(data["courses"]) == 1
    assert data["courses"][0]["course_id"] == "mytimes-47164"


def test_register_course_supports_many_courses(data_root):
    registry_path = data_root / "metadata" / "courses.json"
    for cid in ("mytimes-47164", "mytimes-47165", "mytimes-47166"):
        register_course(registry_path, {"course_id": cid, "course_name": cid,
                                        "course_url": course_id_to_url(cid),
                                        "status": "active"})
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(data["courses"]) == 3
    assert [c["course_id"] for c in data["courses"]] == sorted(
        ["mytimes-47164", "mytimes-47165", "mytimes-47166"])


# --------------------------------------------------------------------------- #
# Legacy global inventory migration
# --------------------------------------------------------------------------- #
def test_legacy_global_inventory_migrated_and_archived(data_root, course_dir):
    legacy_path = data_root / "metadata" / "file_inventory.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_docs = []
    for rel, body in [
        ("Software Design SEP-2023 (202605-LLecture)/General/Module Information (MI).pdf",
         b"%PDF-1.4 mi\n"),
        ("Software Design SEP-2023 (202605-LLecture)/Week 3/Lecture 3 - A &amp; B (Basics).pdf",
         b"%PDF-1.4 trickypdf\n"),
    ]:
        legacy_docs.append({
            "document_id": "x", "relative_path": rel,
            "content_hash": hashlib.sha256(body).hexdigest(),
            "processing_status": "extracted",
        })
    legacy_path.write_text(json.dumps({"documents": legacy_docs}), encoding="utf-8")

    assert main(["--course-dir", str(course_dir)]) == 0

    # Per-course inventory exists and carried over legacy statuses by hash.
    inventory = json.loads(
        (data_root / "inventories" / "mytimes-47164" / "file_inventory.json").read_text("utf-8"))
    by_path = {d["relative_path"]: d for d in inventory["documents"]}
    assert by_path["General/Module Information (MI).pdf"]["processing_status"] == "extracted"
    assert by_path["Week 3/Lecture 3 - A &amp; B (Basics).pdf"]["processing_status"] == "extracted"
    # Unrelated new files are pending.
    assert by_path["Week 2/a&b.txt"]["processing_status"] == "pending"

    # Legacy file is gone from the canonical path and preserved in the archive.
    assert not legacy_path.exists()
    archived = data_root / "metadata" / "archive" / "file_inventory.global.json"
    assert archived.is_file()
    assert json.loads(archived.read_text("utf-8"))["documents"] == legacy_docs


def test_archive_legacy_global_inventory_preserves_data(data_root):
    legacy = data_root / "metadata" / "file_inventory.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}", encoding="utf-8")
    dest = archive_legacy_global_inventory(legacy, data_root)
    assert dest is not None and dest.is_file()
    assert not legacy.exists()
    assert dest.read_text("utf-8") == "{}"


# --------------------------------------------------------------------------- #
# Full pipeline via CLI
# --------------------------------------------------------------------------- #
def test_main_idempotent_no_duplicates_or_lost_status(data_root, course_dir):
    (course_dir / "course_metadata.json").write_text(json.dumps({
        "schema_version": "1.0", "course_id": "mytimes-47164",
        "course_url": "https://mytimes.taylors.edu.my/course/view.php?id=47164",
        "course_name": "Software Design SEP-2023 (202605-LLecture)",
    }), encoding="utf-8")

    assert main(["--course-dir", str(course_dir)]) == 0
    assert main(["--course-dir", str(course_dir)]) == 0

    registry = json.loads((data_root / "metadata" / "courses.json").read_text("utf-8"))
    assert len(registry["courses"]) == 1
    entry = registry["courses"][0]
    assert entry["course_id"] == "mytimes-47164"
    assert entry["course_name"] == "Software Design SEP-2023 (202605-LLecture)"
    assert entry["course_url"] == "https://mytimes.taylors.edu.my/course/view.php?id=47164"
    assert entry["raw_path"] == "raw/mytimes-47164"
    assert entry["inventory_path"] == "inventories/mytimes-47164/file_inventory.json"
    assert entry["status"] == "active"

    inv1 = json.loads((data_root / "inventories" / "mytimes-47164" / "file_inventory.json")
                      .read_text("utf-8"))
    inv2 = json.loads((data_root / "inventories" / "mytimes-47164" / "file_inventory.json")
                      .read_text("utf-8"))
    assert len(inv1["documents"]) == 8 == len(inv2["documents"])
    # Second run: nothing added, nothing changed, everything unchanged.
    assert inv2["summary"]["added_files"] == 0
    assert inv2["summary"]["changed_files"] == 0
    assert inv2["summary"]["unchanged_files"] == 8
    assert [d["relative_path"] for d in inv1["documents"]] == \
        [d["relative_path"] for d in inv2["documents"]]

    # The manifest itself is excluded from the documents list.
    assert all(d["filename"] != "course_metadata.json" for d in inv2["documents"])


def test_main_new_weekly_file_detected_on_second_run(data_root, course_dir):
    assert main(["--course-dir", str(course_dir)]) == 0
    (course_dir / "Week 4").mkdir()
    (course_dir / "Week 4" / "Week 4 - New Slides.pdf").write_bytes(b"%PDF-1.4 w4\n")
    assert main(["--course-dir", str(course_dir)]) == 0
    inv = json.loads((data_root / "inventories" / "mytimes-47164" / "file_inventory.json")
                     .read_text("utf-8"))
    assert len(inv["documents"]) == 9
    assert inv["summary"]["added_files"] == 1
    assert inv["summary"]["unchanged_files"] == 8


def test_main_one_course_does_not_scan_others(data_root, course_dir):
    other = data_root / "raw" / "mytimes-47165"
    (other / "General").mkdir(parents=True)
    (other / "General" / "Other Course.pdf").write_bytes(b"%PDF-1.4 other\n")

    assert main(["--course-dir", str(course_dir)]) == 0

    inv = json.loads((data_root / "inventories" / "mytimes-47164" / "file_inventory.json")
                     .read_text("utf-8"))
    names = [d["filename"] for d in inv["documents"]]
    assert "Other Course.pdf" not in names
    assert len(inv["documents"]) == 8
    # The other course was not scanned and has no inventory.
    assert not (data_root / "inventories" / "mytimes-47165").exists()
    # Both are registered though.
    registry = json.loads((data_root / "metadata" / "courses.json").read_text("utf-8"))
    assert {c["course_id"] for c in registry["courses"]} == {"mytimes-47164"}


def test_main_requires_course_dir_under_raw(data_root, course_dir):
    bad = data_root / "not-raw" / "mytimes-47164"
    bad.mkdir(parents=True)
    assert main(["--course-dir", str(bad)]) == 1


def test_write_inventory_roundtrip(course_dir, data_root):
    inv = build_course_inventory(course_dir, "mytimes-47164", "x")
    out = data_root / "inventories" / "mytimes-47164" / "file_inventory.json"
    write_inventory(inv, out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["summary"]["total_files"] == 8
    assert len(loaded["documents"]) == 8


def test_load_course_metadata(course_dir):
    assert load_course_metadata(course_dir) == {}
    (course_dir / "course_metadata.json").write_text(
        json.dumps({"course_id": "mytimes-47164"}), encoding="utf-8")
    assert load_course_metadata(course_dir) == {"course_id": "mytimes-47164"}


def test_load_previous_course_inventory(course_dir, data_root):
    assert load_previous_course_inventory(data_root / "nope.json") == {}
    inv = build_course_inventory(course_dir, "mytimes-47164", "x")
    write_inventory(inv, data_root / "inventories" / "mytimes-47164" / "file_inventory.json")
    prev = load_previous_course_inventory(data_root / "inventories" / "mytimes-47164" / "file_inventory.json")
    assert len(prev) == 8
    assert all("content_hash" in v and "processing_status" in v for v in prev.values())
