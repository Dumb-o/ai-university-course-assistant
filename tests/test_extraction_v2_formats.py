"""Tests for the v2 DOCX / PPTX / XLSX extractors and multi-format orchestration.

Covers each new extractor's envelope and native-structure preservation, the
extension → v2 extractor registry routing, and the orchestrator's multi-format
behavior: routing, reporting, output-existence idempotency, ``--force``,
``--document-id`` on non-PDF documents, unsupported-format skipping, and the
raw / v1 / inventory-untouched guarantees.

Real DOCX/PPTX/XLSX files are generated with the same libraries the extractors
use (python-docx, python-pptx, openpyxl).
"""

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA_EXTRACTION = ROOT / "data_extraction"
if str(DATA_EXTRACTION) not in sys.path:
    sys.path.insert(0, str(DATA_EXTRACTION))

from docx import Document as DocxDocument
from docx.oxml.ns import qn
from extraction_orchestratorv2 import run_extraction
from extractors import EXTRACTOR_REGISTRY_V2, get_extractor_v2
from extractors.docx_extractorv2 import VERSION as DOCX_VERSION
from extractors.docx_extractorv2 import DocxExtractorV2
from extractors.pptx_extractorv2 import VERSION as PPTX_VERSION
from extractors.pptx_extractorv2 import PptxExtractorV2
from extractors.xlsx_extractorv2 import VERSION as XLSX_VERSION
from extractors.xlsx_extractorv2 import XlsxExtractorV2
from test_extraction_v2 import (
    COURSE_ID,
    doc_record,
    make_docx,
    make_pdf,
    make_pptx,
    make_xlsx,
    write_inventory,
    write_registry,
)


# --------------------------------------------------------------------------- #
# extractor factories
# --------------------------------------------------------------------------- #
def make_multi_format_repo(tmp_path, with_unsupported=True):
    """A minimal course-data tree with one file per supported format."""
    raw = tmp_path / "raw" / COURSE_ID
    make_pdf(raw / "General" / "Module Information (MI).pdf", ["Module Info Page One"])
    make_docx(raw / "Week 9" / "Boundary Value Analysis.docx")
    make_pptx(raw / "Week 6" / "Advanced Test Types and Test Levels.pptx")
    make_xlsx(raw / "Week 3" / "Test Case Example 1.xlsx")

    documents = [
        doc_record("General/Module Information (MI).pdf", status="extracted"),
        doc_record("Week 9/Boundary Value Analysis.docx", status="pending"),
        doc_record("Week 6/Advanced Test Types and Test Levels.pptx", status="pending"),
        doc_record("Week 3/Test Case Example 1.xlsx", status="pending"),
    ]
    if with_unsupported:
        (raw / "Week 10" / "Notes.txt").parent.mkdir(parents=True, exist_ok=True)
        (raw / "Week 10" / "Notes.txt").write_text("plain text notes", encoding="utf-8")
        documents.append(doc_record("Week 10/Notes.txt", status="pending"))

    write_inventory(tmp_path, documents)
    write_registry(tmp_path, [COURSE_ID])
    return tmp_path


def v2_json(data_root: Path, relative_path: str) -> dict:
    out = data_root / "extracted_v2" / COURSE_ID / Path(relative_path).with_suffix(".json")
    return json.loads(out.read_text("utf-8"))


def make_docx_numbered(path: Path) -> Path:
    """A DOCX whose list items get their numbering via ``numPr`` (no List style)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = DocxDocument()
    for text in ("First step", "Second step"):
        paragraph = doc.add_paragraph(text)
        p_pr = paragraph._p.get_or_add_pPr()
        num_pr = p_pr.makeelement(qn("w:numPr"), {})
        ilvl = p_pr.makeelement(qn("w:ilvl"), {qn("w:val"): "0"})
        num_id = p_pr.makeelement(qn("w:numId"), {qn("w:val"): "1"})
        num_pr.append(ilvl)
        num_pr.append(num_id)
        p_pr.append(num_pr)
    doc.save(path)
    return path


# --------------------------------------------------------------------------- #
# registry routing
# --------------------------------------------------------------------------- #
def test_registry_routes_v2_extensions():
    assert EXTRACTOR_REGISTRY_V2[".pdf"] == ("pdf_extractorv2", "PdfExtractorV2")
    assert EXTRACTOR_REGISTRY_V2[".docx"] == ("docx_extractorv2", "DocxExtractorV2")
    assert EXTRACTOR_REGISTRY_V2[".pptx"] == ("pptx_extractorv2", "PptxExtractorV2")
    assert EXTRACTOR_REGISTRY_V2[".xlsx"] == ("xlsx_extractorv2", "XlsxExtractorV2")


def test_registry_routing_returns_classes():
    assert get_extractor_v2(".pdf") is not None
    assert get_extractor_v2(".DOCX") is DocxExtractorV2  # case-insensitive
    assert get_extractor_v2(".pptx") is PptxExtractorV2
    assert get_extractor_v2(".xlsx") is XlsxExtractorV2


@pytest.mark.parametrize("extension", [".doc", ".ppt", ".xls", ".jpg", ".png", ".txt", None])
def test_registry_extensions_without_direct_extractor_return_none(extension):
    # .doc/.ppt/.xls have no *direct* v2 extractor either: they are handled by
    # the orchestrator via LibreOffice conversion (see test_extraction_v2_legacy.py).
    assert get_extractor_v2(extension) is None


# --------------------------------------------------------------------------- #
# DOCX extractor
# --------------------------------------------------------------------------- #
def test_docx_extractor_output_shape(tmp_path):
    path = make_docx(tmp_path / "doc.docx")
    result = DocxExtractorV2().extract(path, doc_record("Week 9/doc.docx"))

    assert result["document"]["relative_path"] == "Week 9/doc.docx"
    assert result["source"]["format"] == "docx"
    assert result["extraction"]["method"] == "python-docx"
    assert result["extraction"]["version"] == DOCX_VERSION
    assert result["extraction"]["status"] == "success"
    assert set(result.keys()) == {"document", "source", "extraction", "metadata", "content"}


def test_docx_extractor_preserves_headings_and_levels(tmp_path):
    path = make_docx(tmp_path / "doc.docx")
    content = DocxExtractorV2().extract(path, doc_record("Week 9/doc.docx"))["content"]

    headings = [p for p in content["paragraphs"] if p["type"] == "heading"]
    assert [h["level"] for h in headings] == [1, 2]
    assert headings[0]["text"] == "Test Case Design"


def test_docx_extractor_detects_lists(tmp_path):
    path = make_docx(tmp_path / "doc.docx")
    content = DocxExtractorV2().extract(path, doc_record("Week 9/doc.docx"))["content"]

    lists = [p for p in content["paragraphs"] if p["type"] == "list"]
    assert [p["text"] for p in lists] == ["First partition.", "Second partition."]
    assert all(p["ordered"] is False for p in lists)
    assert all(p["list"] == "bullet" for p in lists)


def test_docx_extractor_detects_numpr_lists(tmp_path):
    path = make_docx_numbered(tmp_path / "numbered.docx")
    content = DocxExtractorV2().extract(path, doc_record("Week 9/numbered.docx"))["content"]

    lists = [p for p in content["paragraphs"] if p["type"] == "list"]
    assert [p["text"] for p in lists] == ["First step", "Second step"]
    assert all(p["ordered"] is not None for p in lists)


def test_docx_extractor_preserves_tables(tmp_path):
    path = make_docx(tmp_path / "doc.docx")
    content = DocxExtractorV2().extract(path, doc_record("Week 9/doc.docx"))["content"]

    assert len(content["tables"]) == 1
    table = content["tables"][0]
    assert table["rows"][0] == ["Class", "Example"]
    assert table["rows"][1] == ["Valid", "Age 18-65"]


def test_docx_extractor_paragraph_order(tmp_path):
    path = make_docx(tmp_path / "doc.docx")
    paragraphs = DocxExtractorV2().extract(path, doc_record("Week 9/doc.docx"))["content"]["paragraphs"]

    indexes = [p["index"] for p in paragraphs]
    assert indexes == sorted(indexes)
    # heading before its body paragraph; the document order is preserved
    texts = [p["text"] for p in paragraphs]
    assert texts.index("Test Case Design") < texts.index("Equivalence Partitioning")
    assert texts.index("Equivalence Partitioning") < texts.index(
        "Divide input domain into classes that yield equivalent behaviour."
    )


def test_docx_extractor_raises_on_invalid(tmp_path):
    path = tmp_path / "bad.docx"
    path.write_bytes(b"not a docx zip")
    with pytest.raises(zipfile.BadZipFile):
        DocxExtractorV2().extract(path, doc_record("Week 9/bad.docx"))


# --------------------------------------------------------------------------- #
# PPTX extractor
# --------------------------------------------------------------------------- #
def test_pptx_extractor_output_shape(tmp_path):
    path = make_pptx(tmp_path / "deck.pptx")
    result = PptxExtractorV2().extract(path, doc_record("Week 6/deck.pptx"))

    assert result["source"]["format"] == "pptx"
    assert result["source"]["slide_count"] == 2
    assert result["extraction"]["method"] == "python-pptx"
    assert result["extraction"]["version"] == PPTX_VERSION
    assert set(result.keys()) == {"document", "source", "extraction", "metadata", "content"}


def test_pptx_extractor_preserves_slides_and_titles(tmp_path):
    path = make_pptx(tmp_path / "deck.pptx")
    slides = PptxExtractorV2().extract(path, doc_record("Week 6/deck.pptx"))["content"]["slides"]

    assert len(slides) == 2
    assert [s["slide_number"] for s in slides] == [1, 2]
    assert [s["title"] for s in slides] == ["Static Testing", "Review Types"]


def test_pptx_extractor_preserves_text_and_tables(tmp_path):
    path = make_pptx(tmp_path / "deck.pptx")
    slides = PptxExtractorV2().extract(path, doc_record("Week 6/deck.pptx"))["content"]["slides"]

    slide1_texts = [
        p["text"]
        for shape in slides[0]["shapes"]
        for p in shape.get("paragraphs", [])
    ]
    assert "First bullet" in slide1_texts
    assert "Second bullet" in slide1_texts

    tables = [s for s in slides[1]["shapes"] if s["type"] == "table"]
    assert tables
    assert tables[0]["rows"][0] == ["Type", "Goal"]


def test_pptx_extractor_notes(tmp_path):
    path = make_pptx(tmp_path / "deck.pptx")
    slides = PptxExtractorV2().extract(path, doc_record("Week 6/deck.pptx"))["content"]["slides"]
    assert slides[1]["notes"] == "Speaker notes here."


def test_pptx_extractor_raises_on_invalid(tmp_path):
    path = tmp_path / "bad.pptx"
    path.write_bytes(b"not a pptx zip")
    with pytest.raises(zipfile.BadZipFile):
        PptxExtractorV2().extract(path, doc_record("Week 6/bad.pptx"))


# --------------------------------------------------------------------------- #
# XLSX extractor
# --------------------------------------------------------------------------- #
def test_xlsx_extractor_output_shape(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    result = XlsxExtractorV2().extract(path, doc_record("Week 3/book.xlsx"))

    assert result["source"]["format"] == "xlsx"
    assert result["source"]["sheet_count"] == 2
    assert result["extraction"]["method"] == "openpyxl"
    assert result["extraction"]["version"] == XLSX_VERSION
    assert set(result.keys()) == {"document", "source", "extraction", "metadata", "content"}


def test_xlsx_extractor_preserves_sheets_and_cells(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    sheets = XlsxExtractorV2().extract(path, doc_record("Week 3/book.xlsx"))["content"]["sheets"]

    assert [s["name"] for s in sheets] == ["Test Cases", "Checklist"]
    assert [s["sheet_index"] for s in sheets] == [0, 1]

    first = sheets[0]
    row_indexes = [r["row_index"] for r in first["rows"]]
    assert row_indexes == sorted(row_indexes)
    cells = {c["coordinate"]: c for r in first["rows"] for c in r["cells"]}
    assert cells["A1"]["value"] == "Step"
    assert cells["A1"]["data_type"] == "s"
    assert cells["A2"]["value"] == 1
    assert cells["A2"]["data_type"] == "n"


def test_xlsx_extractor_preserves_formulas(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    sheets = XlsxExtractorV2().extract(path, doc_record("Week 3/book.xlsx"))["content"]["sheets"]
    cells = {c["coordinate"]: c for r in sheets[0]["rows"] for c in r["cells"]}

    assert cells["C3"]["formula"] == "=SUM(A2:A3)"
    assert cells["C3"]["data_type"] == "f"


def test_xlsx_extractor_merged_cells(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx")
    sheets = XlsxExtractorV2().extract(path, doc_record("Week 3/book.xlsx"))["content"]["sheets"]
    assert "D1:E1" in sheets[0]["merged_cells"]


def test_xlsx_extractor_raises_on_invalid(tmp_path):
    path = tmp_path / "bad.xlsx"
    path.write_bytes(b"not an xlsx zip")
    with pytest.raises(zipfile.BadZipFile):
        XlsxExtractorV2().extract(path, doc_record("Week 3/bad.xlsx"))


# --------------------------------------------------------------------------- #
# multi-format orchestration
# --------------------------------------------------------------------------- #
def test_orchestrator_routes_and_extracts_all_supported_formats(tmp_path):
    repo = make_multi_format_repo(tmp_path)
    assert run_extraction(COURSE_ID, data_root=repo) == 0

    root = repo / "extracted_v2" / COURSE_ID
    assert (root / "General" / "Module Information (MI).json").is_file()
    assert (root / "Week 9" / "Boundary Value Analysis.json").is_file()
    assert (root / "Week 6" / "Advanced Test Types and Test Levels.json").is_file()
    assert (root / "Week 3" / "Test Case Example 1.json").is_file()

    assert v2_json(repo, "Week 9/Boundary Value Analysis.docx")["source"]["format"] == "docx"
    assert v2_json(repo, "Week 6/Advanced Test Types and Test Levels.pptx")["source"]["format"] == "pptx"
    assert v2_json(repo, "Week 3/Test Case Example 1.xlsx")["source"]["format"] == "xlsx"


def test_orchestrator_preserves_raw_hierarchy_in_v2_output(tmp_path):
    repo = make_multi_format_repo(tmp_path)
    assert run_extraction(COURSE_ID, data_root=repo) == 0

    result = v2_json(repo, "Week 9/Boundary Value Analysis.docx")
    assert result["document"]["relative_path"] == "Week 9/Boundary Value Analysis.docx"
    assert (repo / "extracted_v2" / COURSE_ID / "Week 9" / "Boundary Value Analysis.json").is_file()


def test_orchestrator_skips_unsupported_formats(tmp_path):
    repo = make_multi_format_repo(tmp_path, with_unsupported=True)
    inv_path = repo / "inventories" / COURSE_ID / "file_inventory.json"
    before = inv_path.read_bytes()

    assert run_extraction(COURSE_ID, data_root=repo) == 0
    assert not (repo / "extracted_v2" / COURSE_ID / "Week 10").exists()
    assert inv_path.read_bytes() == before


def test_orchestrator_reports_counts(capsys, tmp_path):
    repo = make_multi_format_repo(tmp_path, with_unsupported=True)
    assert run_extraction(COURSE_ID, data_root=repo) == 0
    output = capsys.readouterr().out

    assert "PDF: 1" in output
    assert "DOCX: 1" in output
    assert "PPTX: 1" in output
    assert "XLSX: 1" in output
    assert "TXT: 1" in output
    assert "Documents extracted now: 4" in output
    assert "Unsupported files skipped: 1" in output


def test_orchestrator_idempotent_second_run_for_new_formats(tmp_path):
    repo = make_multi_format_repo(tmp_path)
    assert run_extraction(COURSE_ID, data_root=repo) == 0

    out = repo / "extracted_v2" / COURSE_ID / "Week 9" / "Boundary Value Analysis.json"
    first = out.read_bytes()
    assert run_extraction(COURSE_ID, data_root=repo) == 0
    assert out.read_bytes() == first


def test_orchestrator_force_reprocesses_docx(tmp_path):
    repo = make_multi_format_repo(tmp_path)
    assert run_extraction(COURSE_ID, data_root=repo) == 0

    out = repo / "extracted_v2" / COURSE_ID / "Week 9" / "Boundary Value Analysis.json"
    out.write_text(json.dumps({"corrupted": True}), encoding="utf-8")
    assert run_extraction(COURSE_ID, data_root=repo, force=True) == 0
    result = json.loads(out.read_text("utf-8"))
    assert result.get("source", {}).get("format") == "docx"


def test_orchestrator_document_id_targets_non_pdf(tmp_path):
    repo = make_multi_format_repo(tmp_path)
    docx_id = next(
        d["document_id"] for d in json.loads(
            (repo / "inventories" / COURSE_ID / "file_inventory.json").read_text("utf-8")
        )["documents"]
        if d["relative_path"] == "Week 9/Boundary Value Analysis.docx"
    )
    assert run_extraction(COURSE_ID, data_root=repo, document_id=docx_id) == 0
    out_root = repo / "extracted_v2" / COURSE_ID
    assert (out_root / "Week 9" / "Boundary Value Analysis.json").is_file()
    assert not (out_root / "General").exists()
    assert not (out_root / "Week 6").exists()
    assert not (out_root / "Week 3").exists()


def test_orchestrator_document_id_unsupported_type_reports_nothing(tmp_path, capsys):
    repo = make_multi_format_repo(tmp_path, with_unsupported=True)
    txt_id = next(
        d["document_id"] for d in json.loads(
            (repo / "inventories" / COURSE_ID / "file_inventory.json").read_text("utf-8")
        )["documents"]
        if d["relative_path"] == "Week 10/Notes.txt"
    )
    assert run_extraction(COURSE_ID, data_root=repo, document_id=txt_id) == 0
    assert "has no v2 extractor" in capsys.readouterr().out
    assert not (repo / "extracted_v2" / COURSE_ID / "Week 10").exists()


def test_orchestrator_limit_counts_documents_across_formats(tmp_path):
    repo = make_multi_format_repo(tmp_path)
    assert run_extraction(COURSE_ID, data_root=repo, limit=2) == 0
    files = sorted(str(p.relative_to(repo / "extracted_v2" / COURSE_ID))
                   for p in (repo / "extracted_v2" / COURSE_ID).rglob("*.json"))
    assert len(files) == 2


def test_orchestrator_never_touches_raw_or_v1_output(tmp_path):
    repo = make_multi_format_repo(tmp_path)
    v1_dir = repo / "extracted" / COURSE_ID / "Week 1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    v1_json = v1_dir / "IEEE 730.json"
    v1_json.write_text(json.dumps({"v1": True}), encoding="utf-8")
    v1_before = v1_json.read_bytes()

    raw_before = {}
    for path in (repo / "raw" / COURSE_ID).rglob("*"):
        if path.is_file():
            raw_before[str(path.relative_to(repo))] = path.read_bytes()

    assert run_extraction(COURSE_ID, data_root=repo) == 0

    for path in (repo / "raw" / COURSE_ID).rglob("*"):
        if path.is_file():
            assert path.read_bytes() == raw_before[str(path.relative_to(repo))]
    assert v1_json.read_bytes() == v1_before
