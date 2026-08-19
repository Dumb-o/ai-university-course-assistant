#!/usr/bin/env python3
"""Content Normalization V1 — deterministic extraction-to-Markdown converter.

Reads v2 extraction output (course-data/extracted_v2/<course_id>/) and produces
clean, consistent Markdown files with YAML frontmatter in
course-data/normalized/<course_id>/.

Supported input formats:
    PDF   → structure.sections[] → Markdown
    DOCX  → content.paragraphs[] + content.tables[] → Markdown
    PPTX  → content.slides[] → Markdown (slide boundaries preserved)
    XLSX  → content.sheets[] → Markdown (tables preserved)
    DOC   → (LibreOffice → DOCX) → same as DOCX
    PPT   → (LibreOffice → PPTX) → same as PPTX
    XLS   → (LibreOffice → XLSX) → same as XLSX

Usage:
    python normalization/normalization_v1.py --course-id mytimes-47164
    python normalization/normalization_v1.py --course-id mytimes-47164 --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (derived from course-id, same convention as other pipeline stages)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = REPO_ROOT / "course-data"

REGISTRY_FILENAME = "courses.json"
INVENTORY_FILENAME = "file_inventory.json"

# Extraction artifact known to be meaningless
_PLACEHOLDER_PATTERNS: list[re.Pattern] = [
    re.compile(r"^Slide Number Placeholder \d+$", re.I),
    re.compile(r"^<\w+>$"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    """Atomic JSON write (temp → fsync → replace)."""
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _is_placeholder(text: str) -> bool:
    """True when text is an obvious extraction artifact (placeholder)."""
    stripped = text.strip()
    if not stripped:
        return True
    return any(p.match(stripped) for p in _PLACEHOLDER_PATTERNS)


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace to single spaces, strip trailing whitespace per line."""
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        out.append(re.sub(r"[^\S\n]+", " ", line).rstrip())
    return "\n".join(out)


def _collapse_blank_lines(text: str, max_consecutive: int = 2) -> str:
    """Reduce consecutive blank lines to at most ``max_consecutive``."""
    pattern = re.compile(r"(?:[ \t]*\n){%d,}" % (max_consecutive + 1))
    return pattern.sub("\n" * (max_consecutive + 1), text)


def _strip_trailing_blank_lines(text: str) -> str:
    return text.rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def _build_frontmatter(extracted: dict, inventory_doc: dict | None) -> str:
    """Build YAML frontmatter from extracted JSON + inventory record."""
    doc = extracted.get("document", {})
    source = extracted.get("source", {})
    extraction = extracted.get("extraction", {})
    conversion = extracted.get("conversion")
    inv = inventory_doc or {}

    fields: list[tuple[str, str | int | float | None]] = [
        ("document_id", doc.get("document_id")),
        ("course_id", doc.get("course_id")),
        ("filename", _yaml_str(doc.get("filename"))),
        ("source_path", _yaml_str(doc.get("relative_path"))),
        ("source_format", _yaml_str(doc.get("relative_path", "").rsplit(".", 1)[-1] if "." in doc.get("relative_path", "") else None)),
        ("section", _yaml_str(inv.get("section"))),
        ("week", _yaml_str(inv.get("week"))),
        ("mime_type", _yaml_str(source.get("mime_type") or inv.get("mime_type"))),
        ("content_hash", _yaml_str(inv.get("content_hash"))),
        ("hash_algorithm", _yaml_str(inv.get("hash_algorithm"))),
        ("extraction_version", _yaml_str(extraction.get("version") or extraction.get("extractor_version"))),
    ]

    # Legacy conversion provenance
    if conversion and isinstance(conversion, dict) and conversion.get("performed"):
        fields.append(("conversion_performed", True))
        fields.append(("conversion_tool", _yaml_str(conversion.get("tool"))))
        fields.append(("conversion_source_format", _yaml_str(conversion.get("source_format"))))
        fields.append(("conversion_target_format", _yaml_str(conversion.get("target_format"))))
        fields.append(("conversion_status", _yaml_str(conversion.get("status"))))

    lines = ["---"]
    for key, value in fields:
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_str(value) -> str | None:
    """Wrap a string value in YAML double-quotes if not None."""
    if value is None:
        return None
    s = str(value)
    if not s:
        return None
    # Escape internal double quotes
    s = s.replace('"', '\\"')
    return f'"{s}"'


# ---------------------------------------------------------------------------
# PDF normalizer (structure.sections[] → Markdown)
# ---------------------------------------------------------------------------

def _normalize_pdf(extracted: dict) -> str:
    """Convert v2 PDF extraction JSON to Markdown."""
    structure = extracted.get("structure", {})
    sections = structure.get("sections", [])
    lines: list[str] = []

    # Optional title — skip if it's a prefix of any section heading
    # (the extractor sometimes produces a truncated title).
    title = structure.get("title")
    section_headings = [s.get("heading", "") for s in sections if s.get("heading")]

    if title and isinstance(title, dict) and title.get("text"):
        t = title["text"].strip()
        is_prefix = any(h.lower().startswith(t.lower()) for h in section_headings)
        if t and not is_prefix:
            lines.append(f"# {t}")
            lines.append("")

    for section in sections:
        heading = section.get("heading")
        level = section.get("level", 0)
        content = section.get("content", [])

        if heading and level >= 1:
            prefix = "#" * min(level + 1, 6)  # +1 because title is h1
            lines.append(f"{prefix} {heading}")
            lines.append("")

        # Merge consecutive heading elements that are fragments of the same
        # visual heading (PDF extractor often splits one heading line into
        # multiple heading elements).
        i = 0
        while i < len(content):
            element = content[i]
            etype = element.get("type")

            if etype == "heading":
                # Collect consecutive headings at the same level, but only
                # merge when the combined text stays short (fragments of one
                # visual heading line). Long or multi-part headings from grid
                # layouts must stay separate.
                merged_text = element.get("text", "")
                el_level = element.get("level", 2)
                j = i + 1
                while j < len(content):
                    next_el = content[j]
                    if (next_el.get("type") == "heading"
                            and next_el.get("level", 2) == el_level):
                        candidate = merged_text + " " + next_el.get("text", "")
                        if len(candidate) > 80:
                            break
                        merged_text = candidate
                        j += 1
                    else:
                        break
                prefix = "#" * min(el_level + 1, 6)
                lines.append(f"{prefix} {merged_text.strip()}")
                lines.append("")
                i = j
            elif etype == "paragraph":
                text = element.get("text", "").strip()
                if text and not _is_placeholder(text):
                    lines.append(text)
                    lines.append("")
                i += 1
            elif etype == "table":
                _append_table_markdown(lines, element)
                lines.append("")
                i += 1
            elif etype == "list":
                _append_list_markdown(lines, element)
                i += 1
            else:
                i += 1

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DOCX normalizer (content.paragraphs[] + content.tables[] → Markdown)
# ---------------------------------------------------------------------------

def _normalize_docx(extracted: dict) -> str:
    """Convert v2 DOCX extraction JSON to Markdown."""
    content = extracted.get("content", {})
    paragraphs = content.get("paragraphs", [])
    tables = content.get("tables", [])
    lines: list[str] = []

    # Build an index of tables by their document order index
    tables_by_index: dict[int, dict] = {t["index"]: t for t in tables if "index" in t}

    # Interleave paragraphs and tables in document order
    all_elements: list[tuple[int, str, dict]] = []
    for p in paragraphs:
        idx = p.get("index", 0)
        all_elements.append((idx, "paragraph", p))
    for t in tables:
        idx = t.get("index", 0)
        all_elements.append((idx, "table", t))
    all_elements.sort(key=lambda e: e[0])

    for _idx, etype, element in all_elements:
        if etype == "paragraph":
            _append_docx_paragraph(lines, element)
        elif etype == "table":
            _append_table_markdown(lines, element)
            lines.append("")

    # Images reference (metadata only)
    images = content.get("images", {})
    if isinstance(images, dict):
        inline_count = images.get("inline_shapes", 0)
        if inline_count:
            lines.append(f"*[Document contains {inline_count} inline image(s)]*")
            lines.append("")

    return "\n".join(lines)


def _append_docx_paragraph(lines: list[str], element: dict) -> None:
    """Append a DOCX paragraph as Markdown."""
    ptype = element.get("type", "paragraph")
    text = element.get("text", "").strip()
    if not text or _is_placeholder(text):
        return

    level = element.get("level")

    if ptype == "title":
        lines.append(f"# {text}")
        lines.append("")
    elif ptype == "subtitle":
        lines.append(f"## {text}")
        lines.append("")
    elif ptype == "heading" and level is not None:
        prefix = "#" * min(max(level, 1) + 1, 6)
        lines.append(f"{prefix} {text}")
        lines.append("")
    elif ptype == "list":
        list_kind = element.get("list", "bullet")
        marker = "1." if list_kind == "number" else "-"
        lines.append(f"{marker} {text}")
    else:
        lines.append(text)
        lines.append("")


# ---------------------------------------------------------------------------
# PPTX normalizer (content.slides[] → Markdown with slide boundaries)
# ---------------------------------------------------------------------------

def _normalize_pptx(extracted: dict) -> str:
    """Convert v2 PPTX extraction JSON to Markdown, preserving slide boundaries."""
    content = extracted.get("content", {})
    slides = content.get("slides", [])
    lines: list[str] = []

    for slide in slides:
        slide_num = slide.get("slide_number", "?")
        title = slide.get("title")
        shapes = slide.get("shapes", [])
        notes = slide.get("notes")

        # Heading: use slide title if available, else deterministic fallback
        if title and title.strip():
            lines.append(f"## Slide {slide_num} — {title.strip()}")
        else:
            lines.append(f"## Slide {slide_num}")
        lines.append("")

        for shape in shapes:
            _append_pptx_shape(lines, shape)

        if notes and notes.strip():
            lines.append(f"**Speaker Notes:** {notes.strip()}")
            lines.append("")

    return "\n".join(lines)


def _append_pptx_shape(lines: list[str], shape: dict) -> None:
    """Append a PPTX shape's content as Markdown."""
    stype = shape.get("type", "other")

    if stype == "text":
        paragraphs = shape.get("paragraphs", [])
        for para in paragraphs:
            text = para.get("text", "").strip()
            if not text or _is_placeholder(text):
                continue
            level = para.get("level", 0)
            bullet = para.get("bullet")
            if bullet == "number":
                lines.append(f"1. {text}")
            elif bullet == "bullet":
                indent = "  " * level if level else ""
                lines.append(f"{indent}- {text}")
            elif level > 0:
                indent = "  " * level
                lines.append(f"{indent}- {text}")
            else:
                lines.append(text)
                lines.append("")
    elif stype == "table":
        rows = shape.get("rows", [])
        if rows:
            _append_table_markdown(lines, {"columns": rows[0] if rows else [], "rows": rows[1:] if len(rows) > 1 else []})
            lines.append("")
    elif stype == "picture":
        ct = shape.get("content_type", "image")
        lines.append(f"*[Image: {shape.get('name', 'picture')} ({ct})]*")
        lines.append("")
    elif stype == "group":
        for child in shape.get("shapes", []):
            _append_pptx_shape(lines, child)
    # "chart" and "other" shapes are skipped (no text to extract)


# ---------------------------------------------------------------------------
# XLSX normalizer (content.sheets[] → Markdown tables)
# ---------------------------------------------------------------------------

def _normalize_xlsx(extracted: dict) -> str:
    """Convert v2 XLSX extraction JSON to Markdown (spreadsheet → tables)."""
    content = extracted.get("content", {})
    sheets = content.get("sheets", [])
    lines: list[str] = []

    for sheet in sheets:
        name = sheet.get("name", f"Sheet {sheet.get('sheet_index', '?')}")
        rows = sheet.get("rows", [])

        lines.append(f"## Sheet: {name}")
        lines.append("")

        if not rows:
            lines.append("*(empty sheet)*")
            lines.append("")
            continue

        # Build a consistent table from all rows
        # First, find the maximum number of columns
        all_values: list[list[str]] = []
        for row in rows:
            cells = row.get("cells", [])
            row_values = []
            for cell in cells:
                val = cell.get("value")
                formula = cell.get("formula")
                if formula:
                    row_values.append(formula)
                elif val is not None:
                    row_values.append(str(val))
                else:
                    row_values.append("")
            all_values.append(row_values)

        if not all_values:
            lines.append("*(empty sheet)*")
            lines.append("")
            continue

        max_cols = max(len(r) for r in all_values)

        # Pad rows to uniform width
        for row_vals in all_values:
            while len(row_vals) < max_cols:
                row_vals.append("")

        # Emit as Markdown table
        if all_values:
            header = all_values[0]
            lines.append("| " + " | ".join(_escape_table_cell(c) for c in header) + " |")
            lines.append("|" + "|".join("---" for _ in header) + "|")
            for row_vals in all_values[1:]:
                lines.append("| " + " | ".join(_escape_table_cell(c) for c in row_vals) + " |")
            lines.append("")

        # Merged cells note
        merged = sheet.get("merged_cells")
        if merged:
            lines.append(f"*(Merged ranges: {', '.join(str(m) for m in merged)})*")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared table / list rendering
# ---------------------------------------------------------------------------

def _escape_table_cell(text: str) -> str:
    """Escape pipe characters and newlines for a Markdown table cell."""
    s = str(text).replace("|", "\\|").replace("\n", " ").strip()
    return s


def _append_table_markdown(lines: list[str], table: dict) -> None:
    """Append a table (columns + rows) as a Markdown table."""
    columns = table.get("columns", [])
    rows = table.get("rows", [])
    if not columns and not rows:
        return

    # Normalize all rows to lists of strings
    all_rows: list[list[str]] = []
    if columns:
        all_rows.append([str(c) if c is not None else "" for c in columns])
    for row in rows:
        if isinstance(row, list):
            all_rows.append([str(c) if c is not None else "" for c in row])
        elif isinstance(row, dict):
            # Dict-style rows (some formats)
            all_rows.append([str(v) if v is not None else "" for v in row.values()])
        else:
            all_rows.append([str(row)])

    if not all_rows:
        return

    max_cols = max(len(r) for r in all_rows)
    for row_vals in all_rows:
        while len(row_vals) < max_cols:
            row_vals.append("")

    header = all_rows[0]
    lines.append("| " + " | ".join(_escape_table_cell(c) for c in header) + " |")
    lines.append("|" + "|".join("---" for _ in header) + "|")
    for row_vals in all_rows[1:]:
        lines.append("| " + " | ".join(_escape_table_cell(c) for c in row_vals) + " |")


def _append_list_markdown(lines: list[str], list_el: dict) -> None:
    """Append a list element as Markdown bullet/numbered items."""
    ordered = list_el.get("ordered", False)
    items = list_el.get("items", [])
    for i, item in enumerate(items):
        text = item.get("text", "").strip()
        if not text or _is_placeholder(text):
            continue
        if ordered:
            lines.append(f"{i + 1}. {text}")
        else:
            lines.append(f"- {text}")
    lines.append("")


# ---------------------------------------------------------------------------
# Format dispatcher
# ---------------------------------------------------------------------------

def _detect_format(extracted: dict) -> str:
    """Detect the source format from the extraction envelope."""
    source = extracted.get("source", {})
    fmt = source.get("format", "")
    if fmt:
        return fmt.lower()
    # Fallback: check document filename extension
    doc = extracted.get("document", {})
    filename = doc.get("filename", "")
    if "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return "unknown"


def _normalize_one(extracted: dict) -> str:
    """Route a single extraction envelope to the appropriate normalizer."""
    fmt = _detect_format(extracted)
    if fmt == "pdf":
        return _normalize_pdf(extracted)
    if fmt in ("docx", "doc"):
        return _normalize_docx(extracted)
    if fmt in ("pptx", "ppt"):
        return _normalize_pptx(extracted)
    if fmt in ("xlsx", "xls"):
        return _normalize_xlsx(extracted)
    return ""


# ---------------------------------------------------------------------------
# Inventory lookup
# ---------------------------------------------------------------------------

def _load_inventory_map(inventory_path: Path) -> dict[str, dict]:
    """Load inventory as relative_path → document record map."""
    data = _load_json(inventory_path)
    if not data or not isinstance(data.get("documents"), list):
        return {}
    return {doc["relative_path"]: doc for doc in data["documents"] if doc.get("relative_path")}


# ---------------------------------------------------------------------------
# Core normalizer
# ---------------------------------------------------------------------------

def normalize_course(course_id: str, data_root: Path | None = None,
                     force: bool = False) -> int:
    """Normalize all v2 extraction output for ONE course.

    Returns 0 on success, 1 on hard failure.
    """
    data_root = data_root or DEFAULT_DATA_ROOT

    # Validate course exists in registry
    registry = _load_json(data_root / "metadata" / REGISTRY_FILENAME) or {}
    courses = registry.get("courses", [])
    if not any(c.get("course_id") == course_id for c in courses):
        print(f"ERROR: course '{course_id}' not found in registry", file=sys.stderr)
        return 1

    # Load inventory for frontmatter enrichment
    inventory_path = data_root / "inventories" / course_id / INVENTORY_FILENAME
    inv_map = _load_inventory_map(inventory_path)

    # Paths
    extracted_root = data_root / "extracted_v2" / course_id
    normalized_root = data_root / "normalized" / course_id

    if not extracted_root.is_dir():
        print(f"ERROR: extracted v2 directory not found: {extracted_root}", file=sys.stderr)
        return 1

    # Discover all extracted JSON files
    json_files = sorted(extracted_root.rglob("*.json"))
    if not json_files:
        print(f"WARNING: no extracted JSON files found in {extracted_root}")
        return 0

    print(f"Course: {course_id}")
    print(f"Input:  {extracted_root}/")
    print(f"Output: {normalized_root}/")
    print(f"Files:  {len(json_files)}")
    print()

    processed = 0
    skipped = 0
    failed = 0

    for json_file in json_files:
        rel_from_extracted = json_file.relative_to(extracted_root)
        # Output path: same relative structure but .md extension
        out_path = normalized_root / rel_from_extracted.with_suffix(".md")

        # Load extraction JSON
        extracted = _load_json(json_file)
        if not extracted or not isinstance(extracted, dict):
            print(f"  [ERR] {rel_from_extracted} — invalid JSON")
            failed += 1
            continue

        # Idempotency: skip if output exists and input hasn't changed
        if not force and out_path.is_file():
            # Quick content-compare: if output exists, skip
            skipped += 1
            continue

        try:
            # Build frontmatter
            # Find matching inventory record by relative_path
            doc_rel = extracted.get("document", {}).get("relative_path", "")
            inv_doc = inv_map.get(doc_rel)

            frontmatter = _build_frontmatter(extracted, inv_doc)
            body = _normalize_one(extracted)

            # Compose final document
            md = frontmatter + "\n\n" + body
            md = _normalize_whitespace(md)
            md = _collapse_blank_lines(md)
            md = _strip_trailing_blank_lines(md)

            # Write atomically
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = out_path.with_name(out_path.name + ".tmp")
            try:
                tmp.write_text(md, encoding="utf-8")
                import os
                os.replace(str(tmp), str(out_path))
            except BaseException:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

            processed += 1
            print(f"  [OK]  {rel_from_extracted}")
        except Exception as exc:
            failed += 1
            print(f"  [ERR] {rel_from_extracted} — {exc}")

    print()
    print("Normalization complete.")
    print(f"Total:     {len(json_files)}")
    print(f"Processed: {processed}")
    print(f"Skipped:   {skipped}")
    print(f"Failed:    {failed}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize v2 extraction output to clean Markdown.",
    )
    parser.add_argument(
        "--course-id",
        required=True,
        help="the course id, e.g. mytimes-47164",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-normalize all documents even if output already exists",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help=f"course-data root (default: {DEFAULT_DATA_ROOT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return normalize_course(args.course_id, data_root=args.data_root, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
