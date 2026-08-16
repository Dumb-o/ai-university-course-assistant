"""Semantic PDF extraction via PyMuPDF — v2.

V2 keeps the useful low-level source information from v1 and *additionally*
reconstructs a deterministic semantic document structure on top of it:

    source_extraction   the v1 layer (metadata, pages, blocks, bboxes, tables)
    structure           semantic reconstruction (title, sections, headings,
                        paragraphs, lists, tables, reading order, provenance)

The reconstruction is entirely deterministic — no LLM is involved. Heuristics:

* **Running furniture.** Text that repeats on ≥ 2 pages at the same size in the
  top/bottom margin band is treated as running headers/footers and kept out of
  the semantic body (still present in ``source_extraction``, recorded under
  ``structure.furniture``).
* **Tables.** Lines inside a detected native-table bbox are excluded from the
  body text; each table is emitted as a semantic ``table`` element with
  ``columns`` (first row) and ``rows`` (remaining rows).
* **Headings.** A line is a heading when it matches a numbered-section pattern
  (``1.0 ...``, ``1.1 ...``), or is bold and noticeably larger than the body
  font, or is a short bold same-size label. Levels are derived from the dotted
  number depth (``1.0`` → level 1) or default to 2.
* **Lists.** Lines starting with a bullet glyph (Symbol/Wingdings/ASCII),
  ``o <Capital>``, a number (``1.``/``1)``) or a lowercase letter
  (``a.``/``a)``) open list items. Continuation lines (same block or deeper
  indent, no paragraph gap) extend the current item, so multi-line items are
  not fragmented. ``ordered`` reflects the first item's marker.
* **Paragraphs.** Body lines that are not separated by a vertical gap greater
  than the median line height are joined into one paragraph instead of being
  preserved as tiny PDF spans.
* **Reading order.** Every page's content is ordered by (y, x) — top to
  bottom, left to right — rather than trusting raw span order.
* **Provenance.** Every semantic element carries ``page`` (or ``source.page``)
  and a bounding box so it can be traced back to the original PDF.

The canonical output is JSON, not Markdown. The downstream pipeline
(normalization → chunking → embeddings → knowledge base) consumes structured
JSON.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 (canonical import name)
except ImportError:
    import fitz  # PyMuPDF < 1.24 (legacy import name)

from extractors.pdf_extractor import PdfExtractor as V1PdfExtractor

METHOD = "pymupdf"
VERSION = "2.0.0"
FORMAT = "pdf"
MIME_TYPE = "application/pdf"

# Span flag bit used by PyMuPDF for bold text.
FONT_BOLD_BIT = 16

# Top/bottom margin bands (fraction of page height) where running headers and
# footers are expected.
FURNITURE_TOP_BAND = 0.08
FURNITURE_BOTTOM_BAND = 0.85

# Minimum size multiple of the body font that marks a bold line as a heading.
BIG_HEADING_RATIO = 1.1
# Minimum size multiple of the body font that promotes a heading to the title.
TITLE_RATIO = 1.5

# Vertical-gap factor: a gap larger than this × median line height is a break.
PARAGRAPH_GAP_FACTOR = 0.8

# Bullet glyphs used by the common PDF fonts in this corpus (Symbol bullet is
# U+F0B7, Wingdings bullet is U+F0A7) plus common ASCII markers.
BULLET_MARKERS = (
    "\uF0B7", "\uF0A7", "\u2022", "\u25AA", "\u25CF", "\u25E6",
    "\u2023", "\u2043", "\u2013", "\u2014", "\u00B7", "\u2027",
    "*", "-",
)

HEADING_RE = re.compile(r"^\d+(?:\.\d+)+")
LIST_ITEM_NUMBERED_RE = re.compile(r"^\d{1,3}[.)]\s")
LIST_ITEM_LETTERED_RE = re.compile(r"^[a-z][.)]\s")
LIST_ITEM_O_RE = re.compile(r"^o\s+[A-Z]")

MAX_HEADING_CHARS = 60


def _is_bold(span_flags: int) -> bool:
    return bool(span_flags & FONT_BOLD_BIT)


def _is_list_marker(text: str) -> bool:
    """True when ``text`` starts with a bullet, number, letter or ``o`` marker."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0] in BULLET_MARKERS:
        return True
    if LIST_ITEM_O_RE.match(stripped):
        return True
    if LIST_ITEM_NUMBERED_RE.match(stripped):
        return True
    return bool(LIST_ITEM_LETTERED_RE.match(stripped))


def _ordered(marker_text: str) -> bool:
    """A list is ordered when its first item starts with a number or letter."""
    stripped = marker_text.strip()
    return bool(LIST_ITEM_NUMBERED_RE.match(stripped) or LIST_ITEM_LETTERED_RE.match(stripped))


def _heading_level(text: str) -> int:
    """Level from the numbering style: ``N`` / ``N.0`` → 1, deeper → 2+."""
    match = HEADING_RE.match(text.strip())
    if not match:
        return 2
    parts = match.group(0).split(".")
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "0"):
        return 1
    return max(2, len(parts))


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return slug.strip("-") or "section"


def _join_text(parts: list[str]) -> str:
    """Join wrapped lines into one logical text, handling hyphen word-splits."""
    joined = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if joined.endswith("-"):
            joined = joined[:-1] + part
        else:
            joined = f"{joined} {part}" if joined else part
    return re.sub(r"\s+", " ", joined).strip()


class PdfExtractorV2:
    """V2 semantic PDF extractor: low-level source layer + semantic structure."""

    format = FORMAT
    mime_type = MIME_TYPE
    method = METHOD
    version = VERSION

    def extract(self, file_path: Path, document: dict) -> dict:
        """Extract ``file_path`` and return the v2 extraction envelope.

        ``document`` is the inventory record for the file (identity fields only).
        Raises on any failure so the orchestrator can report it and keep going.
        """
        file_path = Path(file_path)
        v1 = V1PdfExtractor().extract(file_path, document)
        pages_low = v1["pages"]

        doc = fitz.open(file_path)
        try:
            table_layers = [_as_table_list(page.get("tables", [])) for page in pages_low]
            page_heights = [page["height"] for page in pages_low]
            lines = _collect_lines(doc, page_heights)
        finally:
            doc.close()

        structure = _build_structure(lines, table_layers, page_heights)

        return {
            "document": v1["document"],
            "source": {
                "format": FORMAT,
                "mime_type": MIME_TYPE,
                "page_count": len(pages_low),
            },
            "extraction": {
                "version": VERSION,
                "method": METHOD,
                "status": "success",
            },
            "source_extraction": {
                "metadata": v1["metadata"],
                "pages": pages_low,
            },
            "structure": structure,
        }


def _as_table_list(tables: list[dict]) -> list[dict]:
    return [{"bbox": [float(v) for v in table["bbox"]], "rows": table["rows"]} for table in tables]


def _collect_lines(doc, page_heights: list[float]) -> list[dict]:
    """All text lines with reading-order signals and font characteristics."""
    lines: list[dict] = []
    for page_index, page in enumerate(doc):
        page_no = page_index + 1
        try:
            blocks = page.get_text("dict")["blocks"]
        except RuntimeError:  # PyMuPDF errors are RuntimeError subclasses
            continue
        for block_index, block in enumerate(blocks):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(span.get("text", "") for span in spans)
                if not text.strip():
                    continue
                dominant = max(spans, key=lambda s: len(s.get("text", "")))
                x0, y0, x1, y1 = line["bbox"]
                lines.append({
                    "page": page_no,
                    "page_height": page_heights[page_index],
                    "block": block_index,
                    "text": text,
                    "bbox": [x0, y0, x1, y1],
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "size": float(dominant.get("size", 0.0)),
                    "bold": _is_bold(int(dominant.get("flags", 0))),
                    "font": dominant.get("font", ""),
                    "furniture": False,
                })
    return lines


def _inside_table(line: dict, table_layers: list[dict]) -> bool:
    cx, cy = (line["x0"] + line["x1"]) / 2, (line["y0"] + line["y1"]) / 2
    for table in table_layers:
        x0, y0, x1, y1 = table["bbox"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return True
    return False


def _mark_furniture(lines: list[dict]) -> None:
    """Mark lines that repeat on ≥ 2 pages in the margin band as furniture."""
    counts: Counter[tuple] = Counter((line["text"].strip(), round(line["size"], 1)) for line in lines)
    repeated = {key for key, count in counts.items() if count >= 2}
    for line in lines:
        height = line["page_height"] or 1.0
        in_band = line["y0"] < FURNITURE_TOP_BAND * height or line["y0"] > FURNITURE_BOTTOM_BAND * height
        if in_band and (line["text"].strip(), round(line["size"], 1)) in repeated:
            line["furniture"] = True


def _body_font_size(lines: list[dict]) -> float:
    sizes = [
        line["size"] for line in lines
        if not line["furniture"] and not line["bold"] and line["size"] > 0
    ]
    if not sizes:
        sizes = [line["size"] for line in lines if line["size"] > 0]
    if not sizes:
        return 12.0
    return Counter(sizes).most_common(1)[0][0]


def _is_heading(line: dict, body_size: float) -> bool:
    text = line["text"].strip()
    if not text or line["furniture"] or _is_list_marker(text):
        return False
    if HEADING_RE.match(text):
        return True
    if line["bold"] and line["size"] >= body_size * BIG_HEADING_RATIO:
        return True
    return bool(line["bold"] and line["size"] >= body_size and len(text) <= MAX_HEADING_CHARS)


def _median_height(lines: list[dict]) -> float:
    heights = sorted(line["y1"] - line["y0"] for line in lines if not line["furniture"])
    if not heights:
        return 14.0
    middle = len(heights) // 2
    return heights[middle] if len(heights) % 2 else (heights[middle - 1] + heights[middle]) / 2


def _build_structure(lines: list[dict], table_layers: list[list[dict]],
                     page_heights: list[float]) -> dict:
    _mark_furniture(lines)
    body_size = _body_font_size(lines)
    median_height = _median_height(lines)

    furniture: list[dict] = [
        {"text": line["text"].strip(), "page": line["page"], "bbox": line["bbox"]}
        for line in lines if line["furniture"]
    ]

    events: list[dict] = []
    for page_no in range(1, len(page_heights) + 1):
        page_lines = [
            line for line in lines
            if line["page"] == page_no and not line["furniture"]
            and not _inside_table(line, table_layers[page_no - 1])
        ]
        page_lines.sort(key=lambda l: (l["y0"], l["x0"]))
        events.extend(_build_page_events(page_lines, body_size, median_height,
                                         page_no, table_layers[page_no - 1]))

    title = _detect_title(events, body_size)
    sections = _build_sections(events)

    structure: dict = {"sections": sections}
    if title is not None:
        structure["title"] = title
    if furniture:
        structure["furniture"] = furniture
    return structure


def _build_page_events(page_lines: list[dict], body_size: float, median_height: float,
                       page_no: int, tables: list[dict]) -> list[dict]:
    elements: list[dict] = []
    paragraph: list[dict] = []
    list_el: dict | None = None

    def close_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            event = _paragraph_event(paragraph, page_no)
            if event is not None:
                elements.append(event)
            paragraph = []

    def close_list() -> None:
        nonlocal list_el
        if list_el:
            elements.append(list_el)
            list_el = None

    for line in page_lines:
        if _is_heading(line, body_size):
            close_list()
            close_paragraph()
            elements.append(_heading_event(line, page_no))
        elif _is_list_marker(line["text"]):
            close_paragraph()
            if list_el is None:
                list_el = {
                    "type": "list",
                    "ordered": _ordered(line["text"]),
                    "items": [],
                    "page": page_no,
                    "_y": line["y0"],
                }
            list_el["items"].append({
                "text": line["text"].strip(),
                "_bbox": list(line["bbox"]),
                "_x0": line["x0"],
                "_y1": line["y1"],
                "_block": line["block"],
            })
        elif list_el is not None and _is_continuation(line, list_el, median_height):
            last = list_el["items"][-1]
            last["text"] = _join_text([last["text"], line["text"]])
            last["_bbox"][0] = min(last["_bbox"][0], line["x0"])
            last["_bbox"][1] = min(last["_bbox"][1], line["y0"])
            last["_bbox"][2] = max(last["_bbox"][2], line["x1"])
            last["_bbox"][3] = max(last["_bbox"][3], line["y1"])
            last["_y1"] = max(last["_y1"], line["y1"])
        else:
            close_list()
            if paragraph and line["y0"] - paragraph[-1]["y1"] > PARAGRAPH_GAP_FACTOR * median_height:
                close_paragraph()
            paragraph.append(line)

    close_list()
    close_paragraph()

    for table in tables:
        elements.append(_table_event(page_no, table))

    elements.sort(key=lambda e: e["_y"])
    for element in elements:
        element.pop("_y", None)
        if element["type"] == "list":
            for item in element["items"]:
                bbox = item.pop("_bbox")
                item["source"] = {"page": page_no, "bbox": bbox}
                item.pop("_x0", None)
                item.pop("_y1", None)
                item.pop("_block", None)
    return elements


def _paragraph_event(lines: list[dict], page_no: int) -> dict | None:
    text = _join_text([line["text"] for line in lines])
    if len(text) < 2:  # drop stray single-glyph noise (e.g. a lone "o" span)
        return None
    x0 = min(l["x0"] for l in lines)
    y0 = min(l["y0"] for l in lines)
    x1 = max(l["x1"] for l in lines)
    y1 = max(l["y1"] for l in lines)
    return {
        "type": "paragraph",
        "text": text,
        "source": {"page": page_no, "bbox": [x0, y0, x1, y1]},
        "_y": y0,
    }


def _heading_event(line: dict, page_no: int) -> dict:
    return {
        "type": "heading",
        "text": line["text"].strip(),
        "level": _heading_level(line["text"]),
        "source": {"page": page_no, "bbox": list(line["bbox"])},
        "_y": line["y0"],
        "_size": line["size"],
    }


def _is_continuation(line: dict, list_el: dict, median_height: float) -> bool:
    if not list_el["items"]:
        return False
    last = list_el["items"][-1]
    gap = line["y0"] - last["_y1"]
    if gap > PARAGRAPH_GAP_FACTOR * median_height:
        return False
    same_block = last["_block"] == line["block"]
    deeper_indent = line["x0"] > last["_x0"] + 8
    return same_block or deeper_indent


def _table_event(page_no: int, table: dict) -> dict:
    rows = [
        [str(cell) if cell is not None else "" for cell in row]
        for row in table["rows"]
        if any(cell is not None and str(cell).strip() for cell in row)
    ]
    return {
        "type": "table",
        "page": page_no,
        "bbox": table["bbox"],
        "columns": rows[0] if rows else [],
        "rows": rows[1:] if rows else [],
        "_y": table["bbox"][1],
    }


def _detect_title(events: list[dict], body_size: float) -> dict | None:
    for event in events:
        if event["type"] != "heading":
            continue
        if event["level"] == 1:
            break
        if event["_size"] >= body_size * TITLE_RATIO:
            return {"text": event["text"], "page": event["source"]["page"], "bbox": event["source"]["bbox"]}
    return None


def _build_sections(events: list[dict]) -> list[dict]:
    sections: list[dict] = []
    current: dict | None = None

    def flush_current() -> None:
        nonlocal current
        if current:
            sections.append(current)
            current = None

    for event in events:
        page = event.get("source", {}).get("page", event.get("page"))
        if event["type"] == "heading" and event["level"] == 1:
            flush_current()
            current = {
                "section_id": _slugify(event["text"]),
                "heading": event["text"],
                "level": 1,
                "page_start": page,
                "page_end": page,
                "content": [_public_event(event)],
            }
        else:
            if current is None:
                current = {
                    "section_id": "preamble",
                    "heading": None,
                    "level": 0,
                    "page_start": page,
                    "page_end": page,
                    "content": [],
                }
            current["page_end"] = max(current["page_end"], page)
            current["content"].append(_public_event(event))

    flush_current()
    return sections


def _public_event(event: dict) -> dict:
    public = dict(event)
    public.pop("_y", None)
    public.pop("_size", None)
    return public
