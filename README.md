# AI University Course Assistant

Automated ingestion of authenticated Moodle course content from Taylor's MyTIMES, built to run from n8n.

## Current milestone — Course Content Ingestion MVP

The goal of the first working system:

> Give the system an authenticated Moodle course URL → access the course → discover all accessible course resources → download the files → extract their contents → organize/store them as a basic course knowledge base.

The first version prioritizes **working reliably over sophistication**. Complex autonomous agents, advanced RAG, sophisticated reranking, embedding pipelines, automatic course discovery and search-engine crawling are intentionally **out of scope** for the MVP.

**Status:** Moodle acquisition is completed/prototyped; the architecture is now **course-centric** — each course is identified by its stable Moodle course id (`mytimes-<id>`), downloads land in `course-data/raw/<course_id>/`, a central registry (`course-data/metadata/courses.json`) tracks every course, and each course has its own inventory (`course-data/inventories/<course_id>/file_inventory.json`). A **structured data extraction stage** is now implemented (`data_extraction/`, PDF-first via PyMuPDF): the orchestrator takes only a course id, validates it against the registry, uses the inventory to pick pending documents, and writes per-document extraction JSON into `course-data/extracted/<course_id>/` (mirroring the raw layout). A **V2 semantic extraction pipeline** (`extraction_orchestratorv2.py` + `pdf_extractorv2.py`) now also runs alongside v1, writing a source-preserving + semantic representation into `course-data/extracted_v2/<course_id>/` for comparison. Apache Tika and everything after extraction are **planned** (see `data_ingestion/TIKA_ARCHITECTURE_PLAN.md`); the extraction implementation supersedes the Tika approach with a Python-native extractor.

## Planned pipeline

```text
Course URL
    ↓
Authenticated Moodle Access (Playwright + Taylor's Microsoft OIDC login)
    ↓
Moodle Resource Discovery
    ↓
Resource URL Collection
    ↓
Resource Resolution
    ↓
File Download
    ↓
File Type Detection
    ↓
Content Extraction (PyMuPDF — IMPLEMENTED, PDF-first)
                (v1 → `extracted/`, v2 semantic → `extracted_v2/` — IMPLEMENTED, parallel)
    ↓
Document Classification
    ↓
Metadata Generation
    ↓
Content Normalization
    ↓
Basic Course Knowledge Base
```

Embeddings / vector search are a **future phase** (Phase 2), not part of the MVP.

## Repository layout

```text
scrape_script/

├── mytimes_scraper_v4.py   # CURRENT: consolidated v3; course-id download dirs (course-data/raw/<course_id>/)
├── mytimes_scraper_v3.py   # ARCHIVED: v2 + FIXED download (response interception; open OR download mode)
├── mytimes_scraper_v2.py   # download via page.expect_download() (fails on "open in browser" resources)
├── mytimes_scraper_v1.py   # card-scoped enriched link scraper (Functions 1-3, no download)
├── mytimes_scraper_v0.py   # earlier baseline: flat "every <a href>" scraper
├── mytimes_scraper.py      # RETIRED 4-function build — slated for deletion, do not use
└── scrape_links_base.py     # prototype: unauthenticated requests scraper (does NOT work on Moodle)
course-data/
├── raw/                    # original downloaded Moodle files (READ-ONLY, canonical raw source)
│   ├── mytimes-47164/      # one folder per course, named by its stable Moodle course id
│   └── ...                 #   (plus course_metadata.json handoff written by the scraper)
├── metadata/
│   └── courses.json        # central course registry (course_id, name, url, raw/inventory paths, status)
├── inventories/
│   └── mytimes-47164/
│       └── file_inventory.json   # course-specific inventory (SHA-256, doc ids, status)
├── extracted/              # IMPLEMENTED (per-course, structured extraction JSON — v1)
├── extracted_v2/           # IMPLEMENTED (per-course, semantic extraction JSON — v2, parallel comparison)
├── normalized/             # PLANNED (per-course)
└── chunks/                 # PLANNED (per-course)
data_ingestion/
├── generate_inventory.py   # course-specific inventory generator (IMPLEMENTED, one course per run)
└── TIKA_ARCHITECTURE_PLAN.md   # Apache Tika integration design (PLAN ONLY — superseded by data_extraction/)
data_extraction/
├── extraction_orchestrator.py   # inventory-driven extraction orchestrator v1 (IMPLEMENTED, PDF-first)
├── extraction_orchestratorv2.py # semantic extraction orchestrator v2 (IMPLEMENTED, parallel to v1)
└── extractors/
    ├── __init__.py              # extension → extractor registry (PDF registered; future formats added here)
    ├── pdf_extractor.py         # structured PDF extraction via PyMuPDF — v1 (IMPLEMENTED)
    └── pdf_extractorv2.py       # semantic PDF extraction via PyMuPDF — v2 (IMPLEMENTED)
Block_example.html           # saved course-content HTML block used to validate the extractor
Links_and_their_meaning.md   # per-link download/skip annotations (the design source for v1/v2/v3)
```

The scraper (`scrape_script/`) handles **course acquisition**; the ingestion
layer (`data_ingestion/`) handles **processing downloaded documents**. The
architecture is **course-centric**: the Moodle course id is the stable machine
identity, the human-readable course name is only descriptive metadata. v1–v3
are read-only archives; v4 is the maintained current version.


## Data ingestion (file inventory)

One `generate_inventory.py` invocation processes **exactly one course**. The
course id is derived from the directory name and the course metadata is read
from the scraper's `course_metadata.json` handoff — the user supplies only the
course directory:

```bash
python3 data_ingestion/generate_inventory.py --course-dir course-data/raw/mytimes-47164
# writes course-data/inventories/mytimes-47164/file_inventory.json
# registers/updates the course in course-data/metadata/courses.json
```

Each document entry records the course id, filename, path relative to the
course directory, extension, MIME type, size, section/week, source directory, a
deterministic `document_id` (`<course_id>-<slug>`), a SHA-256 content hash, and
`processing_status: pending`. Re-running is safe: unchanged files keep their
status (matched by hash), new files are added as `pending`, changed files are
reset to `pending` for reprocessing, and the registry never gets duplicates.

## Structured data extraction (`data_extraction/`)

The **structured data extraction** stage turns inventoried raw files into
machine-readable JSON, one file per source document. It is a **separate
pipeline stage** on purpose: acquisition, inventory and extraction are each
independently maintainable, testable and re-runnable, and none of them ever
rewrites another stage's output.

**Inventory-driven.** The inventory (`file_inventory.json`) is the source of
truth for *what* exists in a course. The orchestrator never scans
`course-data/raw/` on its own — it reads the inventory's `documents` list and
considers only entries whose `processing_status` is `pending`. The decision
chain per document is:

```text
inventory
    ↓
pending?
    ↓
extension
    ↓
registered extractor?
    ↓
YES → extract        NO → skip, keep "pending"
```

**Only the course id is required.** The orchestrator takes the course id
(`--course-id mytimes-47164`); it does not accept a URL, inventory path or raw
path, because those are all derived from the architecture:

```bash
python3 data_extraction/extraction_orchestrator.py --course-id mytimes-47164
```

- **Registry check first.** The course id is validated against
  `course-data/metadata/courses.json`. An unknown course stops with a clear
  error, a non-zero exit, and **no** extraction directory and **no** inventory
  change. `generate_inventory.py` remains responsible for registering courses.
- **Inventory lookup.** The inventory is loaded from
  `course-data/inventories/<course_id>/file_inventory.json`. If it is missing
  the run fails clearly; it is never auto-generated here.
- **Raw path resolution.** Each document's `relative_path` is authoritative.
  The raw file is `course-data/raw/<course_id>/<relative_path>` — never
  reconstructed from the filename, section or week. The file must exist and be
  a file, or the document is marked failed. Raw files are **never modified**.
- **Output mirrors raw.** Extracted JSON is written to
  `course-data/extracted/<course_id>/<relative_path with a .json suffix>`, so
  `Week 3/Lecture 3 - Fundamentals of Software Testing.pdf` becomes
  `extracted/<course_id>/Week 3/Lecture 3 - Fundamentals of Software
  Testing.json`. Directory parents are created only when a supported document
  is actually written there, so no empty directories are created.
- **PDF is the only extractor today.** The `extractors/` package holds an
  extension → extractor registry. `.pdf` is registered (PyMuPDF: per-page
  text, text/image blocks with bounding boxes, document metadata, and
  best-effort native tables). DOCX/PPTX/XLSX/images have **no registered
  extractor yet**, so they are skipped and stay `pending` — they are waiting
  for a future extractor, not errors. Future formats are added by creating
  `extractors/<format>_extractor.py` (same `extract(file_path, document)`
  interface) and registering the extension in `extractors/__init__.py`.
- **Status lifecycle.** `pending` → `extracted` on success; `pending` →
  `extraction_failed` (with an `extraction_error`) on failure. Unsupported
  files remain `pending`. The inventory is written back **atomically** (temp
  file → fsync → replace) after every document, so a crash never corrupts it.
- **Idempotent.** Only `pending` documents are ever processed, so re-running
  does nothing to already-`extracted` files. Unsupported files stay `pending`
  on every run.
- **One-document test mode.** `--limit 1` processes a single pending PDF, and
  `--document-id <id>` targets one specific document — useful for inspecting
  one extracted JSON before processing a whole course:

```bash
python3 data_extraction/extraction_orchestrator.py --course-id mytimes-47164 --limit 1
python3 data_extraction/extraction_orchestrator.py \
    --course-id mytimes-47164 --document-id mytimes-47164-general-module-information-mi-pdf
```

**IMPLEMENTED NOW:** PDF extraction (PyMuPDF), PDF routing, inventory-driven
processing, extracted output mirroring `raw/`, atomic `processing_status`
updates, idempotent re-runs, single-document test mode.

**FUTURE:** DOCX, PPTX, XLSX and image/OCR extractors, richer table extraction,
then normalization, classification, chunking, embeddings and the knowledge
base.

## Structured extraction v2 (`data_extraction/` — parallel pipeline)

Alongside v1, a **second, parallel extraction pipeline** runs with the same
interface and conventions so the two can be compared on the same course:

```text
v1: extraction_orchestrator.py  + extractors/pdf_extractor.py    → extracted/<course_id>/
v2: extraction_orchestratorv2.py + extractors/pdf_extractorv2.py → extracted_v2/<course_id>/
```

- **Same contract.** Same CLI (`--course-id`, `--limit`, `--document-id`,
  `--force`), same inventory-driven logic, same `pending`/`extracted`/
  `extraction_failed` lifecycle, same atomic inventory writes, same course-id
  validation against `courses.json`, same read-from-raw/write-to-output
  separation, same `--force` rebuild semantics (re-hashes from raw, never
  reads `extracted/` output). V1 and v2 share the same inventory
  (`file_inventory.json`) — both update `processing_status` identically.
- **Different representation.** v2 builds a **semantic** document model from the
  same PyMuPDF pass: `structure` (headings/paragraphs/lists/tables with roles
  and levels), `content` (a linear markdown document with headings, lists,
  fenced code blocks and ASCII-art tables — no line numbers or bbox noise),
  `tables` (heading row + body rows, dropped empty/footnote-only tables),
  and `source` (per-page `items` = cleaned text runs/images with 1-based page
  numbers and filename, mirroring v1's `pages`). `source_extraction` reports
  the exact provenance of every v2 field. V1 keeps its literal
  per-page-blocks-with-bboxes view; the two are complementary.
- **Why two.** v1 is the lossless raw view; v2 is the RAG-friendly semantic
  view. Running both on the same course lets later stages pick whichever is
  better and gives an A/B corpus for extraction-quality validation — without
  touching v1's tested code.
- **Inventory safety rule (applies to both).** Neither pipeline ever writes to
  the inventory or to any other pipeline's directory. `--force` re-hashes raw
  files and re-writes only that pipeline's own output tree; the shared
  inventory's `processing_status` transitions are the only cross-pipeline
  effect, and they are applied atomically.

Run it the same way as v1:

```bash
python3 data_extraction/extraction_orchestratorv2.py --course-id mytimes-47164
```

**IMPLEMENTED NOW:** parallel v2 pipeline, semantic `structure`/`content`/
`tables`/`source`, `source_extraction` provenance, same lifecycle and safety
rules, 24 unit/integration tests in `tests/test_extraction_v2.py`.

**FUTURE:** DOCX/PPTX/XLSX v2 extractors, `--format` selection (v1|v2|both),
then the v2 semantic model feeds normalization/classification.

## Prerequisites

- Python 3.14+ and a virtual environment
- Playwright + Chromium (scraper)
- PyMuPDF (`pymupdf`) for the PDF extractor


## Running the scraper (v4 — downloads into `course-data/raw/<course_id>/`)

```bash
source .venv/bin/activate
pip install playwright beautifulsoup4 requests fastapi uvicorn pymupdf
playwright install chromium
python3 scrape_script/mytimes_scraper_v4.py "https://mytimes.taylors.edu.my/course/view.php?id=47164"
```

MyTIMES authenticates via a **Taylor's Microsoft account** (MFA required). The script opens a visible Chromium window; complete email + password + MFA in the browser. **No Enter press is needed** — once you finish login and the URL leaves the Microsoft login page, the scraper automatically collects the course's activity links (scoped to the Moodle activity cards) and then downloads the marked files into a section folder tree.

**Stable course id:** the scraper reads the Moodle course id from the URL
(`?id=47164` → `mytimes-47164`) and uses it as the download folder name. Only
the course URL is required; the user never types the id or the course name.

**Download destination:** files are saved to
`course-data/raw/<course_id>/<Section>/<name>.<ext>` (anchored to the repo, not
the working directory). The human-readable course name is preserved as metadata
in `course-data/raw/<course_id>/course_metadata.json` (the handoff the
inventory generator reads). Repeated scrapes always update the same
`<course_id>/` folder — the folder is never duplicated or renamed — and existing
files are skipped (dedup), so new weekly materials are added incrementally.
`-d` remains available to override the root with a custom destination.

### Command-line options

Positional argument:
- `url` — the MyTIMES course URL to open (required).

Flags:
```bash
python3 scrape_script/mytimes_scraper_v4.py "URL"
python3 scrape_script/mytimes_scraper_v4.py "URL" -o custom_links.json      # JSON output path (default: mytimes_links.json)
python3 scrape_script/mytimes_scraper_v4.py "URL" -d ./somewhere_else       # override download root (default: course-data/raw)
python3 scrape_script/mytimes_scraper_v4.py "URL" --no-download            # collect links only, skip downloading
```

- Each file is saved to `<downloads_root>/<course_id>/<Section>/<name>.<ext>` — the `<course_id>` folder comes from the Moodle URL (e.g. `mytimes-47164`), the `<Section>` folder mirrors the site's week/subsection organization (e.g. `General/`, `Week 2/`, `Software Sustainment MLO2/`, `MLO3 - Group Project 40%/`), and the filename is the scraped `name`. The default `<downloads_root>` is `course-data/raw/`. **Deduplication:** if the target file already exists it is skipped, so re-runs are safe and never create duplicates.
>
> **v3/v4 download fix (open-in-browser + `.php`):**
> - Open-in-browser resources (file renders instead of downloading) are captured via request routing (`page.route` + `route.fetch()`), so both force-download and open modes save correctly.
> - Extension hardening: files are NEVER saved with a script/`.php` extension. The extension is taken from the Content-Disposition filename, then a real document URL extension, then Content-Type, then **magic-bytes sniff** of the file body. This fixes the Week 10 (ppt → was `.php`) and Week 9 cases. An HTML link/Page wrapper (not a real document) is detected and skipped, not saved as a file.

### Script structure (`scrape_script/mytimes_scraper_v4.py`)
- `course_id_from_url(url)` — extract `mytimes-<id>` from the Moodle course URL (the stable course identity).
- `open_browser_at(url, output, downloads_dir=DEFAULT_DOWNLOADS_ROOT, do_download)` — Functions 1 + 2: launch Chromium (visible), navigate, wait for manual auth, then run 3 + 4. `DEFAULT_DOWNLOADS_ROOT` is the repo-anchored `course-data/raw/`; files are nested under `<course_id>/` and `course_metadata.json` is written for the inventory handoff.
- `scrape_links(page, base_url)` — Function 3: card-scoped extractor (same as v1/v2): `modtype_*` class (kind), `data-activityname` (name), `a.aalink` href (url), nearest `li.section` (section), hidden "File"/"Page" suffix (file_type), and the `download` flag.
- `download_link(page, link, downloads_dir)` — Function 4: open the `mod/resource` page in the authenticated session, capture the file via request routing (`route.fetch()`), save into the section folder with the scraped name + extension. Works for both force-download and open-in-browser resources.
- `download_documents(page, links, downloads_dir)` — Function 4: loops over `download: true` links, returns per-link results (downloaded / skipped / error).
- `looks_like_login(url)` / `wait_for_authenticated(page)` — detect when login is done (no Enter needed).
- `SKIP_KINDS` — module types that are NOT downloads.

v1 (`mytimes_scraper_v1.py`) is the scrape-only variant; v2 (`mytimes_scraper_v2.py`) is the pre-fix downloader (fails on open-in-browser resources). v3 was the first fixed-downloader; v4 consolidates it and is the current version.

## Known facts & unknowns

| Item | Status |
| --- | --- |
| MyTIMES is Moodle-based | Confirmed |
| Authentication is required (OIDC / Microsoft account) | Confirmed |
| Basic `requests` scraping cannot access course content | Confirmed |
| Playwright is the chosen browser automation approach | Implemented (Functions 1-4 in v2) |
| Playwright authentication works | Verified (manual login + auto-detect, headless logic tested) |
| Card-scoped extractor reads real names + sections (Option A) | Implemented + verified against `Block_example.html` |
| `kind` from `modtype_*` class; `download` flag set from "File" suffix | Implemented + verified |
| Download into section folder tree + filename from scraped name | Implemented + verified (Function 4 in v2/v3) |
| Default download root = `course-data/raw/` (repo-anchored) | Implemented in v4 (`DEFAULT_DOWNLOADS_ROOT`); `-d` still overrides |
| Deduplication (skip-if-exists on re-run) | Implemented + verified |
| Open-in-browser resources captured (v3 fix: route.fetch) | Implemented + verified (was the v2 gap) |
| Extension hardening (no `.php`/script ext; magic-bytes sniff) | Implemented + verified (Week 10/9 `.php` bug fixed) |
| HTML link/Page wrapper skipped (not saved as file) | Implemented + verified |
| Resource resolution mechanism is fully understood | Mostly (mod:resource → file served via pluginfile.php; live run may need tweaks) |
| Raw corpus stored under `course-data/raw/<course_id>/` | Implemented (course-centric: one folder per course, named by Moodle course id) |
| Scraper derives `course_id` from the Moodle URL | Implemented in v4 (`course_id_from_url`) + verified |
| `course_metadata.json` handoff (course_id/name/url) | Implemented in v4 (written into the course dir) |
| Central course registry (`course-data/metadata/courses.json`) | Implemented (`generate_inventory.py`; never duplicates) |
| Course-specific inventory (`course-data/inventories/<course_id>/file_inventory.json`) | Implemented (`data_ingestion/generate_inventory.py --course-dir …`; one course per run) |
| Incremental inventory (hash-based add/unchanged/changed) | Implemented + verified (unchanged files keep status; new = pending; changed = pending) |
| Legacy global inventory migrated + archived | Implemented (`metadata/archive/file_inventory.global.json`, 31 hashes carried over) |
| Structured data extraction (`data_extraction/`, inventory-driven) | Implemented (PDF-first via PyMuPDF; per-page text/blocks/bboxes/metadata + best-effort tables) |
| Structured extraction output in `course-data/extracted/<course_id>/` | Implemented (mirrors `raw/` relative structure; statuses updated atomically) |
| Structured extraction **v2** (semantic model, parallel pipeline) | Implemented (`extraction_orchestratorv2.py` + `pdf_extractorv2.py` → `course-data/extracted_v2/<course_id>/`; structure/content/tables/source + provenance) |
| Inventory safety: pipelines never write to `generate_inventory.py` inputs | Corrected — v1/v2 both re-hash raw and write only their own output tree (see design doc §12) |
| Apache Tika extraction | Not used — plan only (`data_ingestion/TIKA_ARCHITECTURE_PLAN.md`); superseded by `data_extraction/` (PyMuPDF) |
| Complete course ingestion works end-to-end | Download step verified headlessly; live MyTIMES run pending display + credentials; extraction/KB pipeline planned |
| Embeddings are required | No — future phase |

## Roadmap

- **Phase 1 — MVP Ingestion:** authenticated course access → resource discovery → resolution → download → **file inventory (done)** → **structured extraction (done, PDF-first; v2 semantic pipeline also done, parallel)** → basic course knowledge base. Tag `v0.1.0` when this works end-to-end.
- **Phase 2 — Retrieval:** chunking → embeddings → vector database → retrieval.
- **Phase 3 — Assignment Agent:** builds on retrieval to assist with assignments.
