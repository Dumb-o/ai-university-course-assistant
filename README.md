# AI University Course Assistant

Automated ingestion of authenticated Moodle course content from Taylor's MyTIMES, built to run from n8n.

## Current milestone — Course Content Ingestion MVP

The goal of the first working system:

> Give the system an authenticated Moodle course URL → access the course → discover all accessible course resources → download the files → extract their contents → organize/store them as a basic course knowledge base.

The first version prioritizes **working reliably over sophistication**. Complex autonomous agents, advanced RAG, sophisticated reranking, embedding pipelines, automatic course discovery and search-engine crawling are intentionally **out of scope** for the MVP.

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
├── mytimes_scraper_v3.py   # CURRENT: v2 + FIXED download (response interception; open OR download mode)
├── mytimes_scraper_v4.py   # CONSOLIDATED v3: same behavior, factored pure helpers + tests/ harness
├── mytimes_scraper_v2.py   # download via page.expect_download() (fails on "open in browser" resources)
├── mytimes_scraper_v1.py   # card-scoped enriched link scraper (Functions 1-3, no download)
├── mytimes_scraper_v0.py   # earlier baseline: flat "every <a href>" scraper
├── mytimes_scraper.py      # RETIRED 4-function build — slated for deletion, do not use
└── scrape_links_base.py     # prototype: unauthenticated requests scraper (does NOT work on Moodle)
Block_example.html           # saved course-content HTML block used to validate the extractor
Links_and_their_meaning.md   # per-link download/skip annotations (the design source for v1/v2/v3)
```

## Prerequisites

- Python 3.14+ and a virtual environment
- Playwright + Chromium

## Running the scraper (v3 — includes the fixed download)

```bash
source .venv/bin/activate
pip install playwright beautifulsoup4 requests fastapi uvicorn
playwright install chromium
python3 scrape_script/mytimes_scraper_v3.py "https://mytimes.taylors.edu.my/course/view.php?id=47163"
```

MyTIMES authenticates via a **Taylor's Microsoft account** (MFA required). The script opens a visible Chromium window; complete email + password + MFA in the browser. **No Enter press is needed** — once you finish login and the URL leaves the Microsoft login page, the scraper automatically collects the course's activity links (scoped to the Moodle activity cards) and then downloads the marked files into a section folder tree.

### Command-line options

Positional argument:
- `url` — the MyTIMES course URL to open (required).

Flags:
```bash
python3 scrape_script/mytimes_scraper_v3.py "URL"
python3 scrape_script/mytimes_scraper_v3.py "URL" -o custom_links.json      # JSON output path (default: mytimes_links.json)
python3 scrape_script/mytimes_scraper_v3.py "URL" -d ./my_downloads        # download root folder (default: ./downloads)
python3 scrape_script/mytimes_scraper_v3.py "URL" --no-download            # collect links only, skip downloading
```

- Each file is saved to `downloads/<Course Name>/<Section>/<name>.<ext>` — the `<Course Name>` folder is taken from the page title (e.g. `Course: Software Testing (202605-Lecture) | myTimes` → `Software Testing (202605-Lecture)`), the `<Section>` folder mirrors the site's week/subsection organization (e.g. `General/`, `Week 2/`, `Software Sustainment MLO2/`, `MLO3 - Group Project 40%/`), and the filename is the scraped `name`. **Deduplication:** if the target file already exists it is skipped, so re-runs are safe and never create duplicates.
>
> **v3 download fix (open-in-browser + `.php`):**
> - Open-in-browser resources (file renders instead of downloading) are captured via request routing (`page.route` + `route.fetch()`), so both force-download and open modes save correctly.
> - Extension hardening: files are NEVER saved with a script/`.php` extension. The extension is taken from the Content-Disposition filename, then a real document URL extension, then Content-Type, then **magic-bytes sniff** of the file body. This fixes the Week 10 (ppt → was `.php`) and Week 9 cases. An HTML link/Page wrapper (not a real document) is detected and skipped, not saved as a file.

### Script structure (`scrape_script/mytimes_scraper_v3.py`)
- `open_browser_at(url, output, downloads_dir, do_download)` — Functions 1 + 2: launch Chromium (visible), navigate, wait for manual auth, then run 3 + 4.
- `scrape_links(page, base_url)` — Function 3: card-scoped extractor (same as v1/v2): `modtype_*` class (kind), `data-activityname` (name), `a.aalink` href (url), nearest `li.section` (section), hidden "File"/"Page" suffix (file_type), and the `download` flag.
- `download_link(page, link, downloads_dir)` — Function 4: open the `mod/resource` page in the authenticated session, capture the file via request routing (`route.fetch()`), save into the section folder with the scraped name + extension. Works for both force-download and open-in-browser resources.
- `download_documents(page, links, downloads_dir)` — Function 4: loops over `download: true` links, returns per-link results (downloaded / skipped / error).
- `looks_like_login(url)` / `wait_for_authenticated(page)` — detect when login is done (no Enter needed).
- `SKIP_KINDS` — module types that are NOT downloads.

v1 (`mytimes_scraper_v1.py`) is the scrape-only variant; v2 (`mytimes_scraper_v2.py`) is the pre-fix downloader (fails on open-in-browser resources).

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
| Complete course ingestion works end-to-end | Download step verified headlessly; live MyTIMES run pending display + credentials |
| Embeddings are required | No — future phase |

## Roadmap

- **Phase 1 — MVP Ingestion:** authenticated course access → resource discovery → resolution → download → extraction → basic course knowledge base. Tag `v0.1.0` when this works end-to-end.
- **Phase 2 — Retrieval:** chunking → embeddings → vector database → retrieval.
- **Phase 3 — Assignment Agent:** builds on retrieval to assist with assignments.
