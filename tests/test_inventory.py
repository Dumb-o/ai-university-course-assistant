"""Tests for the file inventory generator (data_ingestion/inventory).

Runs without the scraper/browser harness — pure filesystem + logic tests.
Covers: schema shape, document_id uniqueness/collisions, SHA-256 correctness,
course/section/week inference, MIME detection, tricky filenames (spaces,
parentheses, unicode, HTML-encoded "&amp;"), determinism, and graceful handling
of unreadable entries.
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

from inventory.generate_inventory import (  # noqa: E402
    build_inventory,
    detect_mime,
    infer_week,
    make_document_id,
    sha256_of_file,
    slugify,
    write_inventory,
)

TRICKY = {
    "Course (202605-LLecture)": {
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
def corpus(tmp_path):
    return make_tree(tmp_path / "raw", TRICKY)


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


def test_make_document_id_collision_resolved():
    used: set[str] = set()
    first = make_document_id("Week 1/a&b.txt", used)
    second = make_document_id("Week 1/a b.txt", used)
    assert first != second
    assert first == "week-1-a-b-txt"
    assert second.startswith("week-1-a-b-txt-")
    assert len(used) == 2


def test_infer_week():
    assert infer_week("Week 3") == "Week 3"
    assert infer_week("week10") == "week10"
    assert infer_week("General") is None
    assert infer_week("MLO1 - Group Assignment 30%") is None
    assert infer_week(None) is None


def test_sha256_matches_known(corpus):
    p = corpus / "Course (202605-LLecture)" / "General" / "Module Information (MI).pdf"
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
# Inventory behaviour
# --------------------------------------------------------------------------- #
def test_build_inventory_shape_and_counts(corpus):
    inv = build_inventory(corpus)
    docs = inv["documents"]
    assert inv["schema_version"] == "1.0"
    assert inv["inventory_type"] == "file_inventory"
    assert len(docs) == 8  # 2 (Wk3) + 1 (General) + 1 (MLO2) + 3 (Wk2) + 1 (Wk10)
    assert inv["summary"]["total_files"] == 8
    assert inv["summary"]["total_bytes"] == sum(len(v) for d in TRICKY.values()
                                                for sub in d.values() for v in sub.values())
    assert len(inv["summary"]["courses"]) == 1
    assert set(inv["summary"]["file_types"]) == {".pdf", ".txt", ".xlsx", ".ppt"}


def test_build_inventory_metadata_and_hashes(corpus):
    docs = build_inventory(corpus)["documents"]
    by_path = {d["relative_path"]: d for d in docs}

    doc = by_path["Course (202605-LLecture)/Week 3/Lecture 3 - A &amp; B (Basics).pdf"]
    assert doc["course"] == "Course (202605-LLecture)"
    assert doc["section"] == "Week 3"
    assert doc["week"] == "Week 3"
    assert doc["source_directory"] == "Course (202605-LLecture)/Week 3"
    assert doc["mime_type"] == "application/pdf"
    assert doc["processing_status"] == "pending"
    assert doc["content_hash"] == hashlib.sha256(b"%PDF-1.4 trickypdf\n").hexdigest()
    assert doc["hash_error"] is None

    gen = by_path["Course (202605-LLecture)/General/Module Information (MI).pdf"]
    assert gen["week"] is None

    assignment = by_path["Course (202605-LLecture)/MLO2 - Individual Assignment 30%/Rubric & Marking.xlsx"]
    assert assignment["week"] is None
    assert assignment["section"] == "MLO2 - Individual Assignment 30%"


def test_document_ids_unique_and_slug_collisions_distinguished(corpus):
    docs = build_inventory(corpus)["documents"]
    ids = [d["document_id"] for d in docs]
    assert len(ids) == len(set(ids))

    by_id = {d["document_id"]: d["relative_path"] for d in docs}
    # "a&b.txt" and "a b.txt" both slugify to the same base; must not collide.
    colliding = [r for r in by_id.values() if r.endswith(("a&b.txt", "a b.txt"))]
    assert len(colliding) == 2
    suffix_ids = [doc_id for doc_id, rel in by_id.items() if rel.endswith(("a&b.txt", "a b.txt"))]
    assert len(suffix_ids) == 2


def test_every_relative_path_points_to_a_real_file(corpus):
    docs = build_inventory(corpus)["documents"]
    for d in docs:
        target = Path(d["relative_path"])
        assert (corpus / target).is_file()


def test_deterministic_output(corpus):
    a = build_inventory(corpus)
    b = build_inventory(corpus)
    a["generated_at"] = b["generated_at"] = "same"
    assert a == b


def test_unreadable_file_flagged_not_crashing(corpus, tmp_path):
    dangling = tmp_path / "raw" / "Course (202605-LLecture)" / "Week 3" / "broken-link.txt"
    dangling.parent.mkdir(parents=True, exist_ok=True)
    dangling.symlink_to(tmp_path / "does-not-exist.txt")
    inv = build_inventory(corpus)
    docs = inv["documents"]
    broken = [d for d in docs if d["filename"] == "broken-link.txt"]
    assert len(broken) == 1
    assert broken[0]["size_bytes"] is None
    assert broken[0]["content_hash"] is None
    assert broken[0]["hash_error"] is not None
    assert broken[0]["relative_path"] in inv["summary"]["unreadable_files"]


def test_write_inventory_roundtrip(corpus, tmp_path):
    inv = build_inventory(corpus)
    out = tmp_path / "metadata" / "file_inventory.json"
    write_inventory(inv, out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["summary"]["total_files"] == 8
    assert len(loaded["documents"]) == 8
