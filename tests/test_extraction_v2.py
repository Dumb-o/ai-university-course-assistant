"""Tests for the structured data extraction v2 pipeline.

Covers the v2 PDF extractor's semantic reconstruction (sections, headings,
merged paragraphs, lists, preserved tables, provenance, reading order, running
header/footer exclusion, and the retained low-level ``source_extraction``
layer) and the v2 orchestrator (course validation, inventory lookup, raw path
resolution, output under ``extracted_v2/`` mirroring the raw hierarchy,
processing independent of ``processing_status``, output-existence idempotency,
``--force`` reprocessing, and the guarantee that neither the inventory nor the
v1 output is ever touched).

Real PDFs are generated with PyMuPDF, the same library the extractors use.
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

from docx import Document as DocxDocument
from extraction_orchestratorv2 import main, run_extraction
from extractors.pdf_extractorv2 import VERSION, PdfExtractorV2
from openpyxl import Workbook
from pptx import Presentation

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


def make_semantic_pdf(path: Path) -> Path:
    """A two-page PDF exercising every semantic structure v2 must reconstruct.

    Page 1: a large title, numbered section heading 1.0, a two-line paragraph,
    an unordered bullet list, section 2.0 and an ordered numbered list.
    Page 2: section 3.0 and a drawn-grid table. Both pages carry a repeated
    bottom "footer" line that must be detected as running page furniture.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    try:
        page1 = doc.new_page()
        page1.insert_text((54, 80), "GROUP ASSIGNMENT", fontsize=22, fontname="hebo")
        page1.insert_text((54, 130), "1.0 Objective/Learning Outcomes:", fontsize=14, fontname="hebo")
        page1.insert_text(
            (54, 160),
            "The group assignment should be attempted by groups of 5 students. "
            "This assignment assesses your ability to design and plan",
            fontsize=11,
            fontname="helv",
        )
        page1.insert_text(
            (54, 178),
            "a complete software testing strategy including test cases and execution.",
            fontsize=11,
            fontname="helv",
        )
        page1.insert_text((54, 210), "- First deliverable: test plan.", fontsize=11, fontname="helv")
        page1.insert_text((54, 228), "- Second deliverable: execution report.", fontsize=11, fontname="helv")
        page1.insert_text((54, 260), "2.0 Information and Submission Instructions:", fontsize=14, fontname="hebo")
        page1.insert_text((54, 290), "1. Total marks: 100", fontsize=11, fontname="helv")
        page1.insert_text((54, 308), "2. Weightage: 30%", fontsize=11, fontname="helv")
        page1.insert_text((470, 750), "Software Testing", fontsize=9, fontname="helv")

        page2 = doc.new_page()
        page2.insert_text((54, 100), "3.0 Assignment Rubric:", fontsize=14, fontname="hebo")

        x0, y0, x1, y1 = 54, 150, 420, 260
        rows, cols = 4, 3
        for r in range(rows + 1):
            yy = y0 + r * (y1 - y0) / rows
            page2.draw_line((x0, yy), (x1, yy), color=(0, 0, 0), width=1)
        for c in range(cols + 1):
            xx = x0 + c * (x1 - x0) / cols
            page2.draw_line((xx, y0), (xx, y1), color=(0, 0, 0), width=1)
        cells = [
            ["Criteria", "Excellent", "Good"],
            ["Test Plan", "Thorough", "Partial"],
            ["Test Cases", "Complete", "Missing"],
        ]
        for r, row in enumerate(cells):
            for c, val in enumerate(row):
                cx = x0 + c * (x1 - x0) / cols + 4
                cy = y0 + r * (y1 - y0) / rows + 6
                page2.insert_text((cx, cy), val, fontsize=9, fontname="helv")
        page2.insert_text((470, 750), "Software Testing", fontsize=9, fontname="helv")

        doc.save(path)
    finally:
        doc.close()
    return path


def make_docx(path: Path) -> Path:
    """A DOCX exercising headings, lists, a paragraph and a table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = DocxDocument()
    doc.add_heading("Test Case Design", level=1)
    doc.add_heading("Equivalence Partitioning", level=2)
    doc.add_paragraph("Divide input domain into classes that yield equivalent behaviour.")
    doc.add_paragraph("First partition.", style="List Bullet")
    doc.add_paragraph("Second partition.", style="List Bullet")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Class"
    table.cell(0, 1).text = "Example"
    table.cell(1, 0).text = "Valid"
    table.cell(1, 1).text = "Age 18-65"
    doc.save(path)
    return path


def make_pptx(path: Path) -> Path:
    """A PPTX with two slides, a title, a bulleted text box and a table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prs = Presentation()
    slide1 = prs.slides.add_slide(prs.slide_layouts[1])
    slide1.shapes.title.text = "Static Testing"
    body = slide1.shapes.placeholders[1].text_frame
    body.text = "First bullet"
    body.add_paragraph().text = "Second bullet"
    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    slide2.shapes.title.text = "Review Types"
    table = slide2.shapes.add_table(2, 2, 720000, 2000000, 4800000, 1600000).table
    table.cell(0, 0).text = "Type"
    table.cell(0, 1).text = "Goal"
    table.cell(1, 0).text = "Informal"
    table.cell(1, 1).text = "Feedback"
    try:
        slide2.notes_slide.notes_text_frame.text = "Speaker notes here."
    except Exception:  # noqa: BLE001, S110
        pass
    prs.save(path)
    return path


def make_xlsx(path: Path) -> Path:
    """An XLSX with two worksheets, a formula cell and a merged range."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Test Cases"
    ws["A1"] = "Step"
    ws["B1"] = "Expected"
    ws["A2"] = 1
    ws["B2"] = "System responds"
    ws["A3"] = 2
    ws["C3"] = "=SUM(A2:A3)"
    ws.merge_cells("D1:E1")
    ws["D1"] = "Merged"
    ws2 = wb.create_sheet("Checklist")
    ws2["A1"] = "Item"
    ws2["A2"] = "Done"
    wb.save(path)
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
    """A minimal course-data tree for mytimes-47164.

    A PDF inventory-extracted by "v1", a pending PDF, one DOCX, one PPTX, one
    XLSX and one genuinely unsupported .xls file. A pre-existing v1 extracted
    JSON proves v1 output is never touched by v2.
    """
    raw = tmp_path / "raw" / COURSE_ID
    make_semantic_pdf(raw / "Group Assignment" / "Group Assignment.pdf")
    make_pdf(raw / "General" / "Module Information (MI).pdf", ["Module Info Page One", "Module Info Page Two"])
    make_docx(raw / "Week 9" / "Boundary Value Analysis.docx")
    make_pptx(raw / "Week 8" / "Configuration Audits for Testing Quality.pptx")
    make_xlsx(raw / "Week 3" / "Test Case Example 1.xlsx")
    (raw / "Week 10" / "Legacy Lecture.xls").parent.mkdir(parents=True, exist_ok=True)
    (raw / "Week 10" / "Legacy Lecture.xls").write_bytes(b"xls-bytes")

    documents = [
        doc_record("Group Assignment/Group Assignment.pdf", status="extracted"),
        doc_record("General/Module Information (MI).pdf", status="pending"),
        doc_record("Week 3/Test Case Example 1.xlsx", status="pending"),
        doc_record("Week 8/Configuration Audits for Testing Quality.pptx", status="pending"),
        doc_record("Week 9/Boundary Value Analysis.docx", status="pending"),
        doc_record("Week 10/Legacy Lecture.xls", status="pending"),
    ]
    write_inventory(tmp_path, documents)
    write_registry(tmp_path, [COURSE_ID])

    v1_extracted = tmp_path / "extracted" / COURSE_ID / "Week 1"
    v1_extracted.mkdir(parents=True, exist_ok=True)
    (v1_extracted / "IEEE 730.json").write_text(json.dumps({"v1": True}), encoding="utf-8")
    return tmp_path


def load_inventory_docs(data_root: Path) -> list[dict]:
    inv = json.loads((data_root / "inventories" / COURSE_ID / "file_inventory.json").read_text("utf-8"))
    return inv["documents"]


def v2_json(data_root: Path, relative_path: str) -> dict:
    out = data_root / "extracted_v2" / COURSE_ID / Path(relative_path).with_suffix(".json")
    return json.loads(out.read_text("utf-8"))


def all_content(section: dict) -> list[dict]:
    return section["content"]


def elements_by_type(section: dict, element_type: str) -> list[dict]:
    return [e for e in section["content"] if e.get("type") == element_type]


# --------------------------------------------------------------------------- #
# v2 PDF extractor — output envelope and retained source layer
# --------------------------------------------------------------------------- #
def test_v2_extractor_output_shape(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    assert result["document"]["document_id"].startswith(COURSE_ID)
    assert result["document"]["relative_path"] == "Group Assignment/doc.pdf"
    assert result["source"]["format"] == "pdf"
    assert result["source"]["mime_type"] == "application/pdf"
    assert result["source"]["page_count"] == 2
    assert result["extraction"]["version"] == VERSION
    assert result["extraction"]["method"] == "pymupdf"
    assert result["extraction"]["status"] == "success"
    assert set(result.keys()) == {"document", "source", "extraction", "source_extraction", "structure"}


def test_v2_retains_low_level_source_extraction(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    layer = result["source_extraction"]
    assert "metadata" in layer and "pages" in layer
    assert layer["metadata"]["page_count"] == 2
    assert len(layer["pages"]) == 2
    page1 = layer["pages"][0]
    assert page1["page_number"] == 1
    assert "GROUP ASSIGNMENT" in page1["text"]
    # the running footer stays visible in the raw layer even though the
    # semantic layer excludes it
    assert "Software Testing" in page1["text"]


def test_v2_raises_on_invalid_pdf(tmp_path):
    path = tmp_path / "not_a_pdf.pdf"
    path.write_bytes(b"this is definitely not a pdf")
    with pytest.raises(RuntimeError):
        PdfExtractorV2().extract(path, doc_record("Group Assignment/not_a_pdf.pdf"))


# --------------------------------------------------------------------------- #
# v2 PDF extractor — semantic reconstruction
# --------------------------------------------------------------------------- #
def test_v2_detects_sections_and_headings(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    sections = result["structure"]["sections"]
    numbered = [s for s in sections if s["level"] == 1]
    assert [s["heading"] for s in numbered] == [
        "1.0 Objective/Learning Outcomes:",
        "2.0 Information and Submission Instructions:",
        "3.0 Assignment Rubric:",
    ]
    sec = numbered[0]
    assert sec["page_start"] == 1
    assert isinstance(sec["section_id"], str) and sec["section_id"]
    # cover-page content before the first numbered section is preserved in a
    # "preamble" section, not dropped
    assert sections[0]["section_id"] == "preamble"
    assert sections[0]["heading"] is None
    assert elements_by_type(sections[0], "heading")


def test_v2_detects_document_title(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))
    assert result["structure"]["title"]["text"] == "GROUP ASSIGNMENT"


def test_v2_merges_paragraph_lines(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    section = next(s for s in result["structure"]["sections"] if s["heading"] == "1.0 Objective/Learning Outcomes:")
    paragraphs = elements_by_type(section, "paragraph")
    assert len(paragraphs) >= 1
    para = next(p for p in paragraphs if "groups of 5 students" in p["text"])
    # both lines of the paragraph are merged into ONE paragraph
    assert "test cases and execution." in para["text"]
    assert para["text"].count("groups of 5 students") == 1


def test_v2_detects_unordered_list(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    all_lists = [e for s in result["structure"]["sections"] for e in elements_by_type(s, "list")]
    unordered = [lst for lst in all_lists if lst["ordered"] is False]
    assert unordered
    items = unordered[0]["items"]
    assert any("First deliverable" in i["text"] for i in items)
    assert any("Second deliverable" in i["text"] for i in items)


def test_v2_detects_ordered_list(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    all_lists = [e for s in result["structure"]["sections"] for e in elements_by_type(s, "list")]
    ordered = [lst for lst in all_lists if lst["ordered"] is True]
    assert ordered
    items = ordered[0]["items"]
    assert any(i["text"].startswith("1. Total marks: 100") for i in items)
    assert any(i["text"].startswith("2. Weightage: 30%") for i in items)


def test_v2_preserves_table_rows_and_columns(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    rubric = result["structure"]["sections"][-1]
    tables = elements_by_type(rubric, "table")
    assert tables
    table = tables[0]
    assert table["page"] == 2
    assert table["columns"] == ["Criteria", "Excellent", "Good"]
    assert len(table["rows"]) == 2
    assert table["rows"][0][0] == "Test Plan"
    assert table["rows"][1][2] == "Missing"


def test_v2_excludes_running_footer_from_sections(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    body = json.dumps(result["structure"]["sections"], ensure_ascii=False)
    assert "Software Testing" not in body
    furniture = result["structure"].get("furniture")
    assert furniture and any(f["text"] == "Software Testing" for f in furniture)


def test_v2_provenance_page_and_bbox(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    section = next(s for s in result["structure"]["sections"] if s["heading"] == "1.0 Objective/Learning Outcomes:")
    paragraph = next(
        p for p in elements_by_type(section, "paragraph")
        if "groups of 5 students" in p["text"]
    )
    assert paragraph["source"]["page"] == 1
    assert len(paragraph["source"]["bbox"]) == 4
    assert all(isinstance(v, (int, float)) for v in paragraph["source"]["bbox"])


def test_v2_reading_order_is_top_down(tmp_path):
    path = make_semantic_pdf(tmp_path / "doc.pdf")
    result = PdfExtractorV2().extract(path, doc_record("Group Assignment/doc.pdf"))

    numbered = [s for s in result["structure"]["sections"] if s["level"] == 1]
    assert [s["heading"] for s in numbered] == [
        "1.0 Objective/Learning Outcomes:",
        "2.0 Information and Submission Instructions:",
        "3.0 Assignment Rubric:",
    ]
    assert [s["page_start"] for s in numbered] == [1, 1, 2]


# --------------------------------------------------------------------------- #
# v2 orchestrator — course selection and output
# --------------------------------------------------------------------------- #
def test_v2_unknown_course_fails_without_side_effects(repo):
    before = (repo / "inventories" / COURSE_ID / "file_inventory.json").read_bytes()
    rc = run_extraction("mytimes-99999", data_root=repo)
    assert rc != 0
    assert not (repo / "extracted_v2" / "mytimes-99999").exists()
    assert (repo / "inventories" / COURSE_ID / "file_inventory.json").read_bytes() == before


def test_v2_missing_inventory_fails_clearly(tmp_path):
    make_pdf(tmp_path / "raw" / COURSE_ID / "General" / "Module Information (MI).pdf", ["x"])
    write_inventory(tmp_path, [doc_record("General/Module Information (MI).pdf")])
    write_registry(tmp_path, [COURSE_ID])
    (tmp_path / "inventories" / COURSE_ID / "file_inventory.json").unlink()

    assert run_extraction(COURSE_ID, data_root=tmp_path) != 0
    assert not (tmp_path / "extracted_v2").exists()


def test_v2_processes_pdfs_regardless_of_processing_status(repo):
    assert run_extraction(COURSE_ID, data_root=repo) == 0

    docs = load_inventory_docs(repo)
    statuses = {d["relative_path"]: d["processing_status"] for d in docs}
    assert statuses["Group Assignment/Group Assignment.pdf"] == "extracted"
    assert statuses["General/Module Information (MI).pdf"] == "pending"
    # ...but v2 output exists for BOTH PDFs.
    assert v2_json(repo, "Group Assignment/Group Assignment.pdf")["extraction"]["version"] == VERSION
    assert v2_json(repo, "General/Module Information (MI).pdf")["extraction"]["version"] == VERSION


def test_v2_output_mirrors_raw_hierarchy_under_extracted_v2(repo):
    assert run_extraction(COURSE_ID, data_root=repo) == 0
    out = repo / "extracted_v2" / COURSE_ID
    assert (out / "Group Assignment" / "Group Assignment.json").is_file()
    assert (out / "General" / "Module Information (MI).json").is_file()
    result = v2_json(repo, "Group Assignment/Group Assignment.pdf")
    assert result["document"]["relative_path"] == "Group Assignment/Group Assignment.pdf"
    assert result["source"]["page_count"] == 2


def test_v2_unsupported_files_untouched_and_inventory_unchanged(repo):
    inv_path = repo / "inventories" / COURSE_ID / "file_inventory.json"
    before = inv_path.read_bytes()
    assert run_extraction(COURSE_ID, data_root=repo) == 0

    # The unsupported .xls file is skipped (no v2 extractor) and never fails.
    assert not (repo / "extracted_v2" / COURSE_ID / "Week 10").exists()
    assert inv_path.read_bytes() == before
    statuses = {d["relative_path"]: d["processing_status"] for d in load_inventory_docs(repo)}
    assert statuses["Week 10/Legacy Lecture.xls"] == "pending"

    # The supported DOCX/PPTX/XLSX in the same inventory ARE extracted.
    v2_root = repo / "extracted_v2" / COURSE_ID
    assert (v2_root / "Week 9" / "Boundary Value Analysis.json").is_file()
    assert (v2_root / "Week 8" / "Configuration Audits for Testing Quality.json").is_file()
    assert (v2_root / "Week 3" / "Test Case Example 1.json").is_file()


def test_v2_never_touches_v1_output_or_statuses(repo):
    v1_json = repo / "extracted" / COURSE_ID / "Week 1" / "IEEE 730.json"
    before = v1_json.read_bytes()
    inv_before = (repo / "inventories" / COURSE_ID / "file_inventory.json").read_bytes()

    assert run_extraction(COURSE_ID, data_root=repo) == 0
    assert v1_json.read_bytes() == before
    assert (repo / "inventories" / COURSE_ID / "file_inventory.json").read_bytes() == inv_before


def test_v2_idempotent_second_run(repo):
    assert run_extraction(COURSE_ID, data_root=repo) == 0
    out = repo / "extracted_v2" / COURSE_ID / "Group Assignment" / "Group Assignment.json"
    first = out.read_bytes()

    assert run_extraction(COURSE_ID, data_root=repo) == 0
    assert out.read_bytes() == first


def test_v2_force_reprocesses_existing_output(repo):
    assert run_extraction(COURSE_ID, data_root=repo) == 0
    out = repo / "extracted_v2" / COURSE_ID / "Group Assignment" / "Group Assignment.json"
    out.write_text(json.dumps({"corrupted": True}), encoding="utf-8")

    assert run_extraction(COURSE_ID, data_root=repo, force=True) == 0
    result = json.loads(out.read_text("utf-8"))
    assert result.get("extraction", {}).get("version") == VERSION


def test_v2_document_id_selects_one_document(repo):
    doc_id = next(
        d["document_id"] for d in load_inventory_docs(repo)
        if d["relative_path"] == "Group Assignment/Group Assignment.pdf"
    )
    assert run_extraction(COURSE_ID, data_root=repo, document_id=doc_id) == 0
    out_root = repo / "extracted_v2" / COURSE_ID
    assert (out_root / "Group Assignment" / "Group Assignment.json").is_file()
    assert not (out_root / "General").exists()


def test_v2_document_id_unknown_errors(repo):
    assert run_extraction(COURSE_ID, data_root=repo, document_id="does-not-exist") != 0


def test_v2_limit_1(repo):
    assert run_extraction(COURSE_ID, data_root=repo, limit=1) == 0
    out_root = repo / "extracted_v2" / COURSE_ID
    files = sorted(str(p.relative_to(out_root)) for p in out_root.rglob("*.json"))
    assert len(files) == 1


def test_v2_main_returns_nonzero_for_unknown_course(repo, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["--course-id", "mytimes-99999"]) != 0
