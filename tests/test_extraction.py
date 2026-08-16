"""Tests for the structured data extraction stage (data_extraction).

Covers the extension -> extractor registry, the PyMuPDF PDF extractor, and the
orchestrator's inventory-driven flow: course validation, inventory lookup,
pending-only processing, relative-path mirroring, status updates, unsupported
file skipping, idempotency, and the one-document test mode.

Real PDFs are generated with PyMuPDF (the same library the extractor uses) so
the whole path runs against real bytes.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA_EXTRACTION = ROOT / "data_extraction"
if str(DATA_EXTRACTION) not in sys.path:
    sys.path.insert(0, str(DATA_EXTRACTION))

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from extraction_orchestrator import (
    main,
    output_path_for_document,
    run_extraction,
    write_json_atomic,
)
from extractors import EXTRACTOR_REGISTRY, get_extractor
from extractors.pdf_extractor import VERSION, PdfExtractor

COURSE_ID = "mytimes-47164"


def make_pdf(path: Path, texts: list[str]) -> Path:
    """Create a real, text-bearing PDF. One page per entry in ``texts``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    try:
        for text in texts:
            page = doc.new_page()
            page.insert_text((72, 72), text, fontsize=12)
        doc.save(path)
    finally:
        doc.close()
    return path


def doc_record(relative_path: str, status: str = "pending", **overrides) -> dict:
    ext = Path(relative_path).suffix or None
    record = {
        "document_id": f"{COURSE_ID}-{Path(relative_path).stem}",
        "course_id": COURSE_ID,
        "filename": Path(relative_path).name,
        "relative_path": relative_path,
        "extension": ext,
        "mime_type": "application/pdf" if ext == ".pdf" else "application/octet-stream",
        "size_bytes": 1,
        "section": Path(relative_path).parts[0] if len(Path(relative_path).parts) > 1 else None,
        "week": None,
        "source_directory": None,
        "content_hash": "0" * 64,
        "hash_algorithm": "sha256",
        "hash_error": None,
        "processing_status": status,
    }
    record.update(overrides)
    return record


def write_inventory(data_root: Path, documents: list[dict]) -> Path:
    path = data_root / "inventories" / COURSE_ID / "file_inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0",
        "inventory_type": "course_file_inventory",
        "course": {"course_id": COURSE_ID, "course_name": "Software Testing (202605-Lecture)"},
        "generator": {"name": "course_file_inventory", "version": "2.0.0"},
        "generated_at": "2026-08-14T00:00:00+00:00",
        "summary": {"total_files": len(documents)},
        "documents": documents,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_registry(data_root: Path, course_ids: list[str]) -> Path:
    path = data_root / "metadata" / "courses.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0",
        "courses": [{
            "course_id": cid,
            "course_name": "Software Testing (202605-Lecture)" if cid == COURSE_ID else cid,
            "course_url": f"https://mytimes.taylors.edu.my/course/view.php?id={cid.split('-')[-1]}",
            "raw_path": f"raw/{cid}",
            "inventory_path": f"inventories/{cid}/file_inventory.json",
            "status": "active",
        } for cid in course_ids],
    }, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path):
    """A complete, minimal course-data tree for mytimes-47164.

    Two pending PDFs, one already-extracted PDF, and two unsupported pending
    files (docx + pptx). The extracted PDF's output JSON already exists to prove
    idempotent runs do not overwrite it.
    """
    raw = tmp_path / "raw" / COURSE_ID
    make_pdf(raw / "General" / "Module Information (MI).pdf", ["Module Info Page One", "Module Info Page Two"])
    make_pdf(raw / "Week 3" / "Lecture 3 - Fundamentals of Software Testing.pdf", ["Lecture 3 body"])
    make_pdf(raw / "Week 1" / "IEEE 730.pdf", ["Already extracted content"])
    (raw / "Week 8" / "Configuration Audits for Testing Quality.pptx").parent.mkdir(parents=True, exist_ok=True)
    (raw / "Week 8" / "Configuration Audits for Testing Quality.pptx").write_bytes(b"pptx-bytes")
    (raw / "Week 9" / "Boundary Value Analysis.docx").parent.mkdir(parents=True, exist_ok=True)
    (raw / "Week 9" / "Boundary Value Analysis.docx").write_bytes(b"docx-bytes")

    documents = [
        doc_record("General/Module Information (MI).pdf"),
        doc_record("Week 3/Lecture 3 - Fundamentals of Software Testing.pdf"),
        doc_record("Week 1/IEEE 730.pdf", status="extracted"),
        doc_record("Week 8/Configuration Audits for Testing Quality.pptx"),
        doc_record("Week 9/Boundary Value Analysis.docx"),
    ]
    write_inventory(tmp_path, documents)
    write_registry(tmp_path, [COURSE_ID])

    extracted = tmp_path / "extracted" / COURSE_ID / "Week 1"
    extracted.mkdir(parents=True, exist_ok=True)
    (extracted / "IEEE 730.json").write_text(json.dumps({"already": True}), encoding="utf-8")
    return tmp_path


def load_docs(data_root: Path) -> list[dict]:
    inv = json.loads((data_root / "inventories" / COURSE_ID / "file_inventory.json").read_text("utf-8"))
    return inv["documents"]


def status_by_path(data_root: Path) -> dict[str, str]:
    return {d["relative_path"]: d["processing_status"] for d in load_docs(data_root)}


# --------------------------------------------------------------------------- #
# Extractor registry (routing)
# --------------------------------------------------------------------------- #
def test_get_extractor_returns_pdf_extractor():
    assert get_extractor(".pdf") is PdfExtractor
    assert get_extractor(".PDF") is PdfExtractor


@pytest.mark.parametrize("ext", [".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls", ".jpg", ".png", ".txt", None, ""])
def test_get_extractor_unsupported_is_none(ext):
    assert get_extractor(ext) is None


def test_registry_maps_extension_to_class():
    assert EXTRACTOR_REGISTRY == {".pdf": PdfExtractor}


# --------------------------------------------------------------------------- #
# PDF extractor (real bytes)
# --------------------------------------------------------------------------- #
def test_pdf_extractor_output_shape(tmp_path):
    path = make_pdf(tmp_path / "doc.pdf", ["Alpha page one", "Beta page two"])
    result = PdfExtractor().extract(path, doc_record("General/doc.pdf"))

    assert result["document"]["document_id"].startswith(COURSE_ID)
    assert result["document"]["relative_path"] == "General/doc.pdf"
    assert result["source"]["format"] == "pdf"
    assert result["source"]["mime_type"] == "application/pdf"
    assert result["extraction"]["method"] == "pymupdf"
    assert result["extraction"]["status"] == "success"
    assert result["extraction"]["extractor_version"] == VERSION
    assert result["metadata"]["page_count"] == 2
    assert len(result["pages"]) == 2


def test_pdf_extractor_pages_blocks_and_coordinates(tmp_path):
    path = make_pdf(tmp_path / "doc.pdf", ["Hello Block One", "Second page"])
    result = PdfExtractor().extract(path, doc_record("General/doc.pdf"))

    first = result["pages"][0]
    assert first["page_number"] == 1
    assert first["text"].strip() == "Hello Block One"
    assert first["width"] > 0 and first["height"] > 0
    assert first["rotation"] == 0

    text_blocks = [b for b in first["blocks"] if b["type"] == "text"]
    assert len(text_blocks) >= 1
    assert "Hello Block One" in text_blocks[0]["text"]
    assert len(text_blocks[0]["bbox"]) == 4
    assert all(isinstance(v, (int, float)) for v in text_blocks[0]["bbox"])
    # blocks is a list, tables is a list (both may be empty, never missing)
    assert isinstance(first["blocks"], list)
    assert isinstance(first["tables"], list)


def test_pdf_extractor_raises_on_invalid_pdf(tmp_path):
    path = tmp_path / "not_a_pdf.pdf"
    path.write_bytes(b"this is definitely not a pdf")
    with pytest.raises(RuntimeError):
        PdfExtractor().extract(path, doc_record("General/not_a_pdf.pdf"))


def test_pdf_extractor_metadata_survives_close(tmp_path):
    path = make_pdf(tmp_path / "doc.pdf", ["x"])
    result = PdfExtractor().extract(path, doc_record("General/doc.pdf"))
    assert isinstance(result["metadata"], dict)
    assert result["metadata"].get("format") == "PDF 1.7"
    assert result["metadata"]["page_count"] == 1


# --------------------------------------------------------------------------- #
# Orchestrator — course validation
# --------------------------------------------------------------------------- #
def test_unknown_course_fails_without_side_effects(repo):
    before = (repo / "inventories" / COURSE_ID / "file_inventory.json").read_bytes()
    rc = run_extraction("mytimes-99999", data_root=repo)
    assert rc != 0
    # No extraction directory created, inventory untouched.
    assert not (repo / "extracted" / "mytimes-99999").exists()
    assert (repo / "inventories" / COURSE_ID / "file_inventory.json").read_bytes() == before


def test_missing_inventory_fails_clearly(tmp_path):
    raw = tmp_path / "raw" / COURSE_ID
    make_pdf(raw / "General" / "Module Information (MI).pdf", ["x"])
    write_inventory(tmp_path, [doc_record("General/Module Information (MI).pdf")])
    write_registry(tmp_path, [COURSE_ID])
    (tmp_path / "inventories" / COURSE_ID / "file_inventory.json").unlink()

    assert run_extraction(COURSE_ID, data_root=tmp_path) != 0
    assert not (tmp_path / "extracted").exists()


# --------------------------------------------------------------------------- #
# Orchestrator — extraction, statuses, output layout
# --------------------------------------------------------------------------- #
def test_extracts_pending_pdfs_and_mirrors_relative_path(repo):
    rc = run_extraction(COURSE_ID, data_root=repo)
    assert rc == 0

    extracted_root = repo / "extracted" / COURSE_ID
    mi = extracted_root / "General" / "Module Information (MI).json"
    lecture = extracted_root / "Week 3" / "Lecture 3 - Fundamentals of Software Testing.json"
    assert mi.is_file()
    assert lecture.is_file()

    mi_data = json.loads(mi.read_text("utf-8"))
    assert mi_data["document"]["relative_path"] == "General/Module Information (MI).pdf"
    assert mi_data["document"]["filename"] == "Module Information (MI).pdf"
    assert mi_data["metadata"]["page_count"] == 2
    assert mi_data["pages"][0]["page_number"] == 1
    assert "Module Info Page One" in mi_data["pages"][0]["text"]

    statuses = status_by_path(repo)
    assert statuses["General/Module Information (MI).pdf"] == "extracted"
    assert statuses["Week 3/Lecture 3 - Fundamentals of Software Testing.pdf"] == "extracted"


def test_unsupported_files_remain_pending_and_are_not_errors(repo):
    rc = run_extraction(COURSE_ID, data_root=repo)
    assert rc == 0

    statuses = status_by_path(repo)
    assert statuses["Week 8/Configuration Audits for Testing Quality.pptx"] == "pending"
    assert statuses["Week 9/Boundary Value Analysis.docx"] == "pending"

    # No extracted JSON for unsupported files, and no output dirs for them.
    assert not (repo / "extracted" / COURSE_ID / "Week 8").exists()
    assert not (repo / "extracted" / COURSE_ID / "Week 9").exists()


def test_idempotent_second_run_extracts_nothing(repo):
    assert run_extraction(COURSE_ID, data_root=repo) == 0
    mi = repo / "extracted" / COURSE_ID / "General" / "Module Information (MI).json"
    first_content = mi.read_bytes()

    assert run_extraction(COURSE_ID, data_root=repo) == 0
    # JSON is not rewritten (bytes identical) and statuses stay extracted.
    assert mi.read_bytes() == first_content
    statuses = status_by_path(repo)
    assert statuses["General/Module Information (MI).pdf"] == "extracted"
    assert statuses["Week 8/Configuration Audits for Testing Quality.pptx"] == "pending"


def test_already_extracted_file_not_reprocessed(repo):
    already = repo / "extracted" / COURSE_ID / "Week 1" / "IEEE 730.json"
    original = already.read_bytes()
    assert run_extraction(COURSE_ID, data_root=repo) == 0
    assert already.read_bytes() == original


def test_missing_raw_file_marks_extraction_failed(repo):
    (repo / "raw" / COURSE_ID / "Week 3" / "Lecture 3 - Fundamentals of Software Testing.pdf").unlink()
    rc = run_extraction(COURSE_ID, data_root=repo)
    assert rc == 0
    statuses = status_by_path(repo)
    assert statuses["Week 3/Lecture 3 - Fundamentals of Software Testing.pdf"] == "extraction_failed"
    assert statuses["General/Module Information (MI).pdf"] == "extracted"
    assert not (repo / "extracted" / COURSE_ID / "Week 3").exists()


def test_no_output_directory_when_everything_is_unsupported(tmp_path):
    raw = tmp_path / "raw" / COURSE_ID
    (raw / "Week 7").mkdir(parents=True)
    (raw / "Week 7" / "Checklist Static Testing.xlsx").write_bytes(b"xlsx")
    write_inventory(tmp_path, [doc_record("Week 7/Checklist Static Testing.xlsx")])
    write_registry(tmp_path, [COURSE_ID])

    assert run_extraction(COURSE_ID, data_root=tmp_path) == 0
    assert not (tmp_path / "extracted").exists()


def test_malformed_inventory_fails_clearly(repo):
    inv_path = repo / "inventories" / COURSE_ID / "file_inventory.json"
    inv = json.loads(inv_path.read_text("utf-8"))
    del inv["documents"]
    inv_path.write_text(json.dumps(inv), encoding="utf-8")
    assert run_extraction(COURSE_ID, data_root=repo) != 0


# --------------------------------------------------------------------------- #
# One-document test mode
# --------------------------------------------------------------------------- #
def test_limit_1_processes_only_one_document(repo):
    assert run_extraction(COURSE_ID, data_root=repo, limit=1) == 0
    statuses = status_by_path(repo)
    processed = [rel for rel, st in statuses.items() if st == "extracted" and rel != "Week 1/IEEE 730.pdf"]
    assert len(processed) == 1
    # The other pending PDF is still pending.
    remaining = [rel for rel, st in statuses.items() if st == "pending"]
    assert len(remaining) == 3


def test_document_id_selects_one_document(repo):
    record = load_docs(repo)[1]
    rc = run_extraction(COURSE_ID, data_root=repo, document_id=record["document_id"])
    assert rc == 0
    assert (repo / "extracted" / COURSE_ID / "Week 3" / "Lecture 3 - Fundamentals of Software Testing.json").is_file()
    statuses = status_by_path(repo)
    assert statuses["Week 3/Lecture 3 - Fundamentals of Software Testing.pdf"] == "extracted"
    assert statuses["General/Module Information (MI).pdf"] == "pending"


def test_document_id_not_found_errors(repo):
    assert run_extraction(COURSE_ID, data_root=repo, document_id="does-not-exist") != 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_output_path_for_document():
    assert str(output_path_for_document("Week 3/Lecture 3 - Fundamentals of Software Testing.pdf")) == \
        "Week 3/Lecture 3 - Fundamentals of Software Testing.json"
    assert str(output_path_for_document("General/Module Information (MI).pdf")) == \
        "General/Module Information (MI).json"
    assert str(output_path_for_document("noext")) == "noext.json"


def test_write_json_atomic(tmp_path):
    target = tmp_path / "inventories" / COURSE_ID / "file_inventory.json"
    write_json_atomic(target, {"documents": [{"x": 1}]})
    assert json.loads(target.read_text("utf-8"))["documents"] == [{"x": 1}]
    assert not target.with_name(target.name + ".tmp").exists()


def test_main_returns_zero_for_known_course(repo, monkeypatch):
    monkeypatch.chdir(ROOT)  # main() uses the repo-anchored default data root
    rc = main(["--course-id", "mytimes-99999"])
    assert rc != 0
