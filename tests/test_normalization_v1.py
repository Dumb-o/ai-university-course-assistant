"""Tests for normalization/normalization_v1.py — content normalization pipeline.

Covers all supported formats (PDF, DOCX, PPTX, XLSX), legacy provenance
preservation, frontmatter generation, whitespace normalization, placeholder
cleanup, slide boundary preservation, table preservation, idempotency, and
error handling.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from normalization import normalization_v1 as nv1

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "course-data"


# ---------------------------------------------------------------------------
# Fixtures: minimal extraction envelopes for each format
# ---------------------------------------------------------------------------

@pytest.fixture
def pdf_envelope():
    """Minimal v2 PDF extraction envelope."""
    return {
        "document": {
            "document_id": "test-course-week-1-lecture-pdf",
            "course_id": "test-course",
            "filename": "Lecture.pdf",
            "relative_path": "Week 1/Lecture.pdf",
        },
        "source": {"format": "pdf", "mime_type": "application/pdf", "page_count": 2},
        "extraction": {"version": "2.0.0", "method": "pymupdf", "status": "success"},
        "structure": {
            "title": {"text": "Introduction to Testing", "page": 1, "bbox": [0, 0, 100, 20]},
            "sections": [
                {
                    "section_id": "preamble",
                    "heading": None,
                    "level": 0,
                    "page_start": 1,
                    "page_end": 1,
                    "content": [
                        {"type": "paragraph", "text": "This is the first paragraph.", "source": {"page": 1, "bbox": [0, 0, 100, 20]}},
                    ],
                },
                {
                    "section_id": "intro",
                    "heading": "Introduction",
                    "level": 1,
                    "page_start": 1,
                    "page_end": 2,
                    "content": [
                        {"type": "heading", "text": "Background", "level": 2, "source": {"page": 1, "bbox": [0, 0, 100, 20]}},
                        {"type": "paragraph", "text": "Some background text.", "source": {"page": 1, "bbox": [0, 0, 100, 20]}},
                        {"type": "table", "page": 2, "bbox": [0, 0, 100, 50], "columns": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]},
                    ],
                },
            ],
        },
    }


@pytest.fixture
def docx_envelope():
    """Minimal v2 DOCX extraction envelope."""
    return {
        "document": {
            "document_id": "test-course-week-1-notes-docx",
            "course_id": "test-course",
            "filename": "Notes.docx",
            "relative_path": "Week 1/Notes.docx",
        },
        "source": {"format": "docx", "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        "extraction": {"version": "2.0.0", "method": "python-docx", "status": "success"},
        "metadata": {"author": "Test Author", "title": "Test Notes"},
        "content": {
            "paragraphs": [
                {"index": 0, "text": "Title Text", "style": "Title", "type": "title"},
                {"index": 1, "text": "Introduction", "style": "Heading 1", "type": "heading", "level": 1},
                {"index": 2, "text": "This is a paragraph.", "style": "Normal", "type": "paragraph"},
                {"index": 3, "text": "First item", "style": "List Bullet", "type": "list", "list": "bullet", "ordered": False},
                {"index": 4, "text": "Second item", "style": "List Bullet", "type": "list", "list": "bullet", "ordered": False},
                {"index": 5, "text": "Step one", "style": "List Number", "type": "list", "list": "number", "ordered": True},
            ],
            "tables": [
                {"index": 6, "rows": [["Name", "Value"], ["X", "100"], ["Y", "200"]]},
            ],
            "images": {"inline_shapes": 2, "image_parts": []},
        },
    }


@pytest.fixture
def pptx_envelope():
    """Minimal v2 PPTX extraction envelope."""
    return {
        "document": {
            "document_id": "test-course-week-1-slides-pptx",
            "course_id": "test-course",
            "filename": "Slides.pptx",
            "relative_path": "Week 1/Slides.pptx",
        },
        "source": {"format": "pptx", "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "slide_count": 3},
        "extraction": {"version": "2.0.0", "method": "python-pptx", "status": "success"},
        "content": {
            "slides": [
                {
                    "slide_number": 1,
                    "layout_name": "Title Slide",
                    "title": "Welcome",
                    "shapes": [
                        {"type": "text", "name": "Title", "paragraphs": [{"text": "Welcome", "level": 0}]},
                    ],
                    "notes": "Opening remarks.",
                },
                {
                    "slide_number": 2,
                    "layout_name": "Content",
                    "title": None,
                    "shapes": [
                        {"type": "text", "name": "Text 1", "paragraphs": [
                            {"text": "Point one", "level": 0, "bullet": "bullet"},
                            {"text": "Point two", "level": 0, "bullet": "bullet"},
                        ]},
                        {"type": "table", "name": "Table 1", "rows": [["Col A", "Col B"], ["1", "2"]]},
                    ],
                    "notes": None,
                },
                {
                    "slide_number": 3,
                    "layout_name": "Content",
                    "title": "Summary",
                    "shapes": [
                        {"type": "picture", "name": "Image 0", "content_type": "image/png"},
                    ],
                    "notes": "Wrap up.",
                },
            ]
        },
    }


@pytest.fixture
def xlsx_envelope():
    """Minimal v2 XLSX extraction envelope."""
    return {
        "document": {
            "document_id": "test-course-week-1-data-xlsx",
            "course_id": "test-course",
            "filename": "Data.xlsx",
            "relative_path": "Week 1/Data.xlsx",
        },
        "source": {"format": "xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "sheet_count": 1},
        "extraction": {"version": "2.0.0", "method": "openpyxl", "status": "success"},
        "content": {
            "sheets": [
                {
                    "name": "Results",
                    "sheet_index": 0,
                    "dimensions": "A1:C3",
                    "max_row": 3,
                    "max_column": 3,
                    "rows": [
                        {"row_index": 1, "cells": [
                            {"coordinate": "A1", "column": 1, "row": 1, "value": "ID", "data_type": "s"},
                            {"coordinate": "B1", "column": 2, "row": 1, "value": "Name", "data_type": "s"},
                            {"coordinate": "C1", "column": 3, "row": 1, "value": "Score", "data_type": "s"},
                        ]},
                        {"row_index": 2, "cells": [
                            {"coordinate": "A2", "column": 1, "row": 2, "value": "1", "data_type": "n"},
                            {"coordinate": "B2", "column": 2, "row": 2, "value": "Alice", "data_type": "s"},
                            {"coordinate": "C2", "column": 3, "row": 2, "value": "95", "data_type": "n"},
                        ]},
                        {"row_index": 3, "cells": [
                            {"coordinate": "A3", "column": 1, "row": 3, "value": "2", "data_type": "n"},
                            {"coordinate": "B3", "column": 2, "row": 3, "value": "Bob", "data_type": "s"},
                            {"coordinate": "C3", "column": 3, "row": 3, "value": "87", "data_type": "n"},
                        ]},
                    ],
                    "merged_cells": ["A1:C1"],
                }
            ]
        },
    }


@pytest.fixture
def legacy_ppt_envelope():
    """PPT → PPTX legacy conversion envelope."""
    return {
        "document": {
            "document_id": "test-course-week-1-legacy-ppt",
            "course_id": "test-course",
            "filename": "Legacy.ppt",
            "relative_path": "Week 1/Legacy.ppt",
        },
        "source": {"format": "ppt", "mime_type": "application/vnd.ms-powerpoint", "extension": ".ppt", "extraction_format": "pptx"},
        "extraction": {"version": "2.0.0", "method": "python-pptx", "status": "success"},
        "conversion": {"performed": True, "tool": "LibreOffice", "source_format": ".ppt", "target_format": ".pptx", "status": "success"},
        "content": {
            "slides": [
                {"slide_number": 1, "layout_name": None, "title": "Legacy Slide", "shapes": [
                    {"type": "text", "name": "Text 0", "paragraphs": [{"text": "Content from legacy file", "level": 0}]},
                ], "notes": None},
            ]
        },
    }


@pytest.fixture
def legacy_doc_envelope():
    """DOC → DOCX legacy conversion envelope."""
    return {
        "document": {
            "document_id": "test-course-week-1-legacy-doc",
            "course_id": "test-course",
            "filename": "Legacy.doc",
            "relative_path": "Week 1/Legacy.doc",
        },
        "source": {"format": "doc", "mime_type": "application/msword", "extension": ".doc", "extraction_format": "docx"},
        "extraction": {"version": "2.0.0", "method": "python-docx", "status": "success"},
        "conversion": {"performed": True, "tool": "LibreOffice", "source_format": ".doc", "target_format": ".docx", "status": "success"},
        "content": {
            "paragraphs": [
                {"index": 0, "text": "Legacy Document Title", "style": "Title", "type": "title"},
                {"index": 1, "text": "Content from legacy DOC.", "style": "Normal", "type": "paragraph"},
            ],
            "tables": [],
            "images": {"inline_shapes": 0, "image_parts": []},
        },
    }


# ---------------------------------------------------------------------------
# Tests: PDF normalization
# ---------------------------------------------------------------------------

class TestPdfNormalization:
    def test_basic_pdf_to_markdown(self, pdf_envelope):
        result = nv1._normalize_pdf(pdf_envelope)
        assert "This is the first paragraph." in result
        assert "## Introduction" in result
        assert "### Background" in result
        assert "Some background text." in result

    def test_pdf_title_not_duplicated(self):
        """Title is skipped when it's a prefix of a section heading."""
        envelope = {
            "structure": {
                "title": {"text": "Introduction", "page": 1, "bbox": [0, 0, 100, 20]},
                "sections": [
                    {"section_id": "intro", "heading": "Introduction to Testing", "level": 1, "page_start": 1, "page_end": 1, "content": []},
                ],
            }
        }
        result = nv1._normalize_pdf(envelope)
        lines = result.strip().split("\n")
        first_content = next(l for l in lines if l.strip())
        # Title "Introduction" is a prefix of "Introduction to Testing" → skipped
        assert first_content.startswith("##")

    def test_pdf_table_preserved(self, pdf_envelope):
        result = nv1._normalize_pdf(pdf_envelope)
        assert "| A | B |" in result
        assert "| 1 | 2 |" in result

    def test_pdf_heading_merging(self):
        """Consecutive heading fragments at same level should merge if short."""
        envelope = {
            "structure": {
                "sections": [{
                    "section_id": "test",
                    "heading": None,
                    "level": 0,
                    "page_start": 1,
                    "page_end": 1,
                    "content": [
                        {"type": "heading", "text": "Software", "level": 2, "source": {"page": 1, "bbox": [0, 0, 50, 10]}},
                        {"type": "heading", "text": "Testing", "level": 2, "source": {"page": 1, "bbox": [50, 0, 100, 10]}},
                    ],
                }]
            }
        }
        result = nv1._normalize_pdf(envelope)
        assert "### Software Testing" in result

    def test_pdf_heading_no_merge_when_long(self):
        """Long consecutive headings should NOT merge (exceeds 80-char limit)."""
        envelope = {
            "structure": {
                "sections": [{
                    "section_id": "test",
                    "heading": None,
                    "level": 0,
                    "page_start": 1,
                    "page_end": 1,
                    "content": [
                        {"type": "heading", "text": "This Is a Very Long Section Heading That Should Not Merge", "level": 2, "source": {"page": 1, "bbox": [0, 0, 50, 10]}},
                        {"type": "heading", "text": "And Neither Should This One Because It Is Also Quite Long", "level": 2, "source": {"page": 1, "bbox": [50, 0, 100, 10]}},
                    ],
                }]
            }
        }
        result = nv1._normalize_pdf(envelope)
        assert "### This Is a Very Long Section Heading That Should Not Merge" in result
        assert "### And Neither Should This One Because It Is Also Quite Long" in result


# ---------------------------------------------------------------------------
# Tests: DOCX normalization
# ---------------------------------------------------------------------------

class TestDocxNormalization:
    def test_basic_docx_to_markdown(self, docx_envelope):
        result = nv1._normalize_docx(docx_envelope)
        assert "# Title Text" in result
        assert "## Introduction" in result
        assert "This is a paragraph." in result

    def test_docx_bullets(self, docx_envelope):
        result = nv1._normalize_docx(docx_envelope)
        assert "- First item" in result
        assert "- Second item" in result

    def test_docx_numbered_list(self, docx_envelope):
        result = nv1._normalize_docx(docx_envelope)
        assert "1. Step one" in result

    def test_docx_table(self, docx_envelope):
        result = nv1._normalize_docx(docx_envelope)
        assert "| Name | Value |" in result
        assert "| X | 100 |" in result

    def test_docx_image_reference(self, docx_envelope):
        result = nv1._normalize_docx(docx_envelope)
        assert "inline image(s)" in result


# ---------------------------------------------------------------------------
# Tests: PPTX normalization
# ---------------------------------------------------------------------------

class TestPptxNormalization:
    def test_slide_boundaries(self, pptx_envelope):
        result = nv1._normalize_pptx(pptx_envelope)
        assert "## Slide 1" in result
        assert "## Slide 2" in result
        assert "## Slide 3" in result

    def test_slide_title_used(self, pptx_envelope):
        result = nv1._normalize_pptx(pptx_envelope)
        assert "## Slide 1 — Welcome" in result
        assert "## Slide 3 — Summary" in result

    def test_slide_no_title_fallback(self, pptx_envelope):
        result = nv1._normalize_pptx(pptx_envelope)
        # Slide 2 has no title
        assert "## Slide 2\n" in result

    def test_slide_bullets(self, pptx_envelope):
        result = nv1._normalize_pptx(pptx_envelope)
        assert "- Point one" in result
        assert "- Point two" in result

    def test_slide_table(self, pptx_envelope):
        result = nv1._normalize_pptx(pptx_envelope)
        assert "| Col A | Col B |" in result

    def test_slide_speaker_notes(self, pptx_envelope):
        result = nv1._normalize_pptx(pptx_envelope)
        assert "Speaker Notes:" in result
        assert "Opening remarks." in result

    def test_slide_image_reference(self, pptx_envelope):
        result = nv1._normalize_pptx(pptx_envelope)
        assert "Image" in result


# ---------------------------------------------------------------------------
# Tests: XLSX normalization
# ---------------------------------------------------------------------------

class TestXlsxNormalization:
    def test_sheet_name_heading(self, xlsx_envelope):
        result = nv1._normalize_xlsx(xlsx_envelope)
        assert "## Sheet: Results" in result

    def test_table_structure(self, xlsx_envelope):
        result = nv1._normalize_xlsx(xlsx_envelope)
        assert "| ID | Name | Score |" in result
        assert "| 1 | Alice | 95 |" in result
        assert "| 2 | Bob | 87 |" in result

    def test_merged_cells_note(self, xlsx_envelope):
        result = nv1._normalize_xlsx(xlsx_envelope)
        assert "Merged ranges" in result

    def test_empty_sheet(self):
        envelope = {
            "content": {
                "sheets": [{"name": "Empty", "sheet_index": 0, "rows": []}]
            }
        }
        result = nv1._normalize_xlsx(envelope)
        assert "*(empty sheet)*" in result


# ---------------------------------------------------------------------------
# Tests: Legacy provenance
# ---------------------------------------------------------------------------

class TestLegacyProvenance:
    def test_ppt_provenance_in_frontmatter(self, legacy_ppt_envelope):
        fm = nv1._build_frontmatter(legacy_ppt_envelope, None)
        assert 'conversion_performed: true' in fm
        assert 'conversion_tool: "LibreOffice"' in fm
        assert 'conversion_source_format: ".ppt"' in fm
        assert 'conversion_target_format: ".pptx"' in fm
        assert 'source_format: "ppt"' in fm

    def test_doc_provenance_in_frontmatter(self, legacy_doc_envelope):
        fm = nv1._build_frontmatter(legacy_doc_envelope, None)
        assert 'conversion_performed: true' in fm
        assert 'conversion_source_format: ".doc"' in fm
        assert 'source_format: "doc"' in fm

    def test_ppt_not_treated_as_docx(self, legacy_ppt_envelope):
        """PPT legacy should normalize as PPTX (slide-based), not DOCX."""
        result = nv1._normalize_one(legacy_ppt_envelope)
        assert "## Slide 1" in result


# ---------------------------------------------------------------------------
# Tests: Frontmatter
# ---------------------------------------------------------------------------

class TestFrontmatter:
    def test_frontmatter_structure(self, pdf_envelope):
        fm = nv1._build_frontmatter(pdf_envelope, None)
        assert fm.startswith("---")
        assert fm.endswith("---")
        assert "document_id: test-course-week-1-lecture-pdf" in fm
        assert "course_id: test-course" in fm
        assert 'source_format: "pdf"' in fm

    def test_frontmatter_with_inventory(self, pdf_envelope):
        inv_doc = {"section": "Week 1", "week": "Week 1", "content_hash": "abc123", "hash_algorithm": "sha256"}
        fm = nv1._build_frontmatter(pdf_envelope, inv_doc)
        assert 'section: "Week 1"' in fm
        assert 'week: "Week 1"' in fm
        assert 'content_hash: "abc123"' in fm

    def test_frontmatter_null_values(self):
        envelope = {"document": {}, "source": {}, "extraction": {}}
        fm = nv1._build_frontmatter(envelope, None)
        assert "document_id: null" in fm
        assert "course_id: null" in fm


# ---------------------------------------------------------------------------
# Tests: Whitespace normalization
# ---------------------------------------------------------------------------

class TestWhitespaceNormalization:
    def test_collapse_blank_lines(self):
        text = "a\n\n\n\n\nb"
        result = nv1._collapse_blank_lines(text)
        # Max 2 consecutive blank lines → exactly \n\n\n (3 newlines) between content
        assert result == "a\n\n\nb"

    def test_strip_trailing_blank_lines(self):
        text = "hello\n\n\n"
        result = nv1._strip_trailing_blank_lines(text)
        assert result == "hello\n"

    def test_normalize_whitespace(self):
        text = "hello   world\t\ttab"
        result = nv1._normalize_whitespace(text)
        assert "hello world tab" in result


# ---------------------------------------------------------------------------
# Tests: Placeholder cleanup
# ---------------------------------------------------------------------------

class TestPlaceholderCleanup:
    def test_slide_number_placeholder(self):
        assert nv1._is_placeholder("Slide Number Placeholder 3")

    def test_angle_bracket_placeholder(self):
        assert nv1._is_placeholder("<number>")

    def test_empty_is_placeholder(self):
        assert nv1._is_placeholder("")
        assert nv1._is_placeholder("   ")

    def test_normal_text_not_placeholder(self):
        assert not nv1._is_placeholder("Introduction")
        assert not nv1._is_placeholder("1. Overview")


# ---------------------------------------------------------------------------
# Tests: Table preservation
# ---------------------------------------------------------------------------

class TestTablePreservation:
    def test_table_markdown_format(self):
        lines: list[str] = []
        nv1._append_table_markdown(lines, {"columns": ["A", "B"], "rows": [["1", "2"]]})
        result = "\n".join(lines)
        assert "| A | B |" in result
        assert "|---|---|" in result
        assert "| 1 | 2 |" in result

    def test_table_pipe_escaping(self):
        lines: list[str] = []
        nv1._append_table_markdown(lines, {"columns": ["A|B"], "rows": []})
        result = "\n".join(lines)
        assert "A\\|B" in result


# ---------------------------------------------------------------------------
# Tests: Format detection
# ---------------------------------------------------------------------------

class TestFormatDetection:
    def test_pdf(self):
        assert nv1._detect_format({"source": {"format": "pdf"}}) == "pdf"

    def test_docx(self):
        assert nv1._detect_format({"source": {"format": "docx"}}) == "docx"

    def test_pptx(self):
        assert nv1._detect_format({"source": {"format": "pptx"}}) == "pptx"

    def test_xlsx(self):
        assert nv1._detect_format({"source": {"format": "xlsx"}}) == "xlsx"

    def test_legacy_doc(self):
        assert nv1._detect_format({"source": {"format": "doc"}}) == "doc"

    def test_fallback_from_filename(self):
        assert nv1._detect_format({"document": {"filename": "test.pdf"}}) == "pdf"


# ---------------------------------------------------------------------------
# Tests: Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_normalized_content_is_stable(self, pdf_envelope):
        """Running normalization twice on the same input produces identical output."""
        first = nv1._normalize_one(pdf_envelope)
        second = nv1._normalize_one(pdf_envelope)
        assert first == second

    def test_frontmatter_is_stable(self, pdf_envelope):
        first = nv1._build_frontmatter(pdf_envelope, None)
        second = nv1._build_frontmatter(pdf_envelope, None)
        assert first == second


# ---------------------------------------------------------------------------
# Tests: Missing/invalid input handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_empty_envelope(self):
        result = nv1._normalize_one({})
        assert result == ""

    def test_unknown_format(self):
        envelope = {"source": {"format": "unknown"}, "document": {"filename": "test.xyz"}}
        result = nv1._normalize_one(envelope)
        assert result == ""

    def test_none_values_handled(self):
        envelope = {
            "document": {"document_id": None, "course_id": None, "filename": None, "relative_path": None},
            "source": {"format": "pdf"},
            "extraction": {},
            "structure": {"sections": []},
        }
        # Should not raise
        result = nv1._normalize_pdf(envelope)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Tests: Real corpus (integration — only run when corpus is present)
# ---------------------------------------------------------------------------

class TestRealCorpus:
    """Integration tests against the real extracted corpus."""

    @pytest.mark.skipif(
        not (DATA_ROOT / "extracted_v2" / "mytimes-47164").is_dir(),
        reason="mytimes-47164 corpus not present",
    )
    def test_mytimes_47164_all_files_normalize(self, tmp_path):
        """All 37 extracted files for mytimes-47164 should normalize without error."""
        rc = nv1.normalize_course("mytimes-47164", data_root=DATA_ROOT, force=True)
        assert rc == 0
        normalized_dir = DATA_ROOT / "normalized" / "mytimes-47164"
        assert normalized_dir.is_dir()
        md_files = list(normalized_dir.rglob("*.md"))
        assert len(md_files) == 37

    @pytest.mark.skipif(
        not (DATA_ROOT / "extracted_v2" / "mytimes-47163").is_dir(),
        reason="mytimes-47163 corpus not present",
    )
    def test_mytimes_47163_all_files_normalize(self, tmp_path):
        """All 29 extracted files for mytimes-47163 should normalize without error."""
        rc = nv1.normalize_course("mytimes-47163", data_root=DATA_ROOT, force=True)
        assert rc == 0
        normalized_dir = DATA_ROOT / "normalized" / "mytimes-47163"
        assert normalized_dir.is_dir()
        md_files = list(normalized_dir.rglob("*.md"))
        assert len(md_files) == 29

    @pytest.mark.skipif(
        not (DATA_ROOT / "extracted_v2" / "mytimes-47164").is_dir(),
        reason="mytimes-47164 corpus not present",
    )
    def test_pdf_file_has_frontmatter(self):
        """A normalized PDF should have YAML frontmatter."""
        path = DATA_ROOT / "normalized" / "mytimes-47164" / "Week 3" / "Lecture 3 - Fundamentals of Software Testing.md"
        if not path.exists():
            pytest.skip("normalized file not found")
        content = path.read_text(encoding="utf-8")
        assert content.startswith("---")
        assert content.count("---") >= 2

    @pytest.mark.skipif(
        not (DATA_ROOT / "extracted_v2" / "mytimes-47164").is_dir(),
        reason="mytimes-47164 corpus not present",
    )
    def test_pptx_file_has_slide_headings(self):
        """A normalized PPTX should have slide boundary headings."""
        path = DATA_ROOT / "normalized" / "mytimes-47164" / "Week 6" / "Advanced Test Types and Test Levels.md"
        if not path.exists():
            pytest.skip("normalized file not found")
        content = path.read_text(encoding="utf-8")
        assert "## Slide 1" in content
        assert "## Slide 2" in content

    @pytest.mark.skipif(
        not (DATA_ROOT / "extracted_v2" / "mytimes-47164").is_dir(),
        reason="mytimes-47164 corpus not present",
    )
    def test_xlsx_file_has_table(self):
        """A normalized XLSX should have Markdown table structure."""
        path = DATA_ROOT / "normalized" / "mytimes-47164" / "Week 3" / "Test Case Example 1.md"
        if not path.exists():
            pytest.skip("normalized file not found")
        content = path.read_text(encoding="utf-8")
        assert "## Sheet:" in content
        assert "|" in content

    @pytest.mark.skipif(
        not (DATA_ROOT / "extracted_v2" / "mytimes-47163").is_dir(),
        reason="mytimes-47163 corpus not present",
    )
    def test_legacy_file_has_conversion_provenance(self):
        """A legacy-converted file should have conversion metadata in frontmatter."""
        path = DATA_ROOT / "normalized" / "mytimes-47163" / "Week 9" / "Lecture 9 - Use Case Diagram.md"
        if not path.exists():
            pytest.skip("normalized file not found")
        content = path.read_text(encoding="utf-8")
        assert "conversion_performed: true" in content
        assert 'conversion_source_format: ".ppt"' in content

    @pytest.mark.skipif(
        not (DATA_ROOT / "extracted_v2" / "mytimes-47164").is_dir(),
        reason="mytimes-47164 corpus not present",
    )
    def test_idempotent_repeated_run(self):
        """Running normalization twice without --force should skip all files."""
        # First run (already done)
        # Second run should skip
        rc = nv1.normalize_course("mytimes-47164", data_root=DATA_ROOT, force=False)
        assert rc == 0
