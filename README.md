# AI University Course Assistant

Automated ingestion of authenticated Moodle course content from Taylor's MyTIMES, built to run from n8n.

## Current milestone — Course Content Ingestion MVP

The goal of the first working system:

> Give the system an authenticated Moodle course URL → access the course → discover all accessible course resources → download the files → extract their contents → organize/store them as a basic course knowledge base.

The first version prioritizes **working reliably over sophistication**. Complex autonomous agents, advanced RAG, sophisticated reranking, embedding pipelines, automatic course discovery and search-engine crawling are intentionally **out of scope** for the MVP.

**Status:** Moodle acquisition is completed/prototyped; the architecture is now **course-centric** — each course is identified by its stable Moodle course id (`mytimes-<id>`), downloads land in `course-data/raw/<course_id>/`, a central registry (`course-data/metadata/courses.json`) tracks every course, and each course has its own inventory (`course-data/inventories/<course_id>/file_inventory.json`). Apache Tika and everything after extraction are **planned** (see `data_ingestion/TIKA_ARCHITECTURE_PLAN.md`).

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
Content Extraction (Apache Tika / OCR)
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
├── extracted/              # PLANNED (per-course, future Apache Tika output)
├── normalized/             # PLANNED (per-course)
└── chunks/                 # PLANNED (per-course)
data_ingestion/
├── generate_inventory.py   # course-specific inventory generator (IMPLEMENTED, one course per run)
└── TIKA_ARCHITECTURE_PLAN.md   # Apache Tika integration design (PLAN ONLY — not installed)
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

## Prerequisites

- Python 3.14+ and a virtual environment
- Playwright + Chromium

## Running the scraper (v4 — downloads into `course-data/raw/<course_id>/`)

```bash
source .venv/bin/activate
pip install playwright beautifulsoup4 requests fastapi uvicorn
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
| Apache Tika extraction | Not implemented — plan only (`data_ingestion/TIKA_ARCHITECTURE_PLAN.md`) |
| Complete course ingestion works end-to-end | Download step verified headlessly; live MyTIMES run pending display + credentials; extraction/KB pipeline planned |
| Embeddings are required | No — future phase |

## Roadmap

- **Phase 1 — MVP Ingestion:** authenticated course access → resource discovery → resolution → download → **file inventory (done)** → extraction → basic course knowledge base. Tag `v0.1.0` when this works end-to-end.
- **Phase 2 — Retrieval:** chunking → embeddings → vector database → retrieval.
- **Phase 3 — Assignment Agent:** builds on retrieval to assist with assignments.
