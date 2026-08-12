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
├── mytimes_scraper_v1.py   # CURRENT: Playwright login + card-scoped enriched link scraper (Functions 1-3)
├── mytimes_scraper_v0.py   # earlier baseline: flat "every <a href>" scraper
├── mytimes_scraper.py      # RETIRED 4-function build (auto-download) — slated for deletion, do not use
└── scrape_links_base.py     # prototype: unauthenticated requests scraper (does NOT work on Moodle)
Block_example.html           # saved course-content HTML block used to validate the extractor
Links_and_their_meaning.md   # per-link download/skip annotations (the design source for v1)
```

## Prerequisites

- Python 3.14+ and a virtual environment
- Playwright + Chromium

## Running the scraper (v1)

```bash
source .venv/bin/activate
pip install playwright beautifulsoup4 requests fastapi uvicorn
playwright install chromium
python3 scrape_script/mytimes_scraper_v1.py "https://mytimes.taylors.edu.my/course/view.php?id=47163"
```

MyTIMES authenticates via a **Taylor's Microsoft account** (MFA required). The script opens a visible Chromium window; complete email + password + MFA in the browser. **No Enter press is needed** — once you finish login and the URL leaves the Microsoft login page, the scraper automatically collects the course's activity links (scoped to the Moodle activity cards, NOT every `<a href>`) and writes `mytimes_links.json`.

Output flag:
```bash
python3 scrape_script/mytimes_scraper_v1.py "URL" -o custom_links.json
```

> **Scope of v1:** Functions 1-3 only — open Chromium, manual login, enriched link collection. There is **no download step** in v1 (the earlier auto-download build in `mytimes_scraper.py` was retired). The JSON carries a `download` boolean (true only for `resource` links whose hidden suffix says "File") so you can decide what to fetch next. Downloading is a planned Function 4, not yet built into v1.

### Script structure (`scrape_script/mytimes_scraper_v1.py`)
- `open_browser_at(url, output)` — Function 1 + 2: launch Chromium (visible), navigate, wait for manual auth to finish automatically, then run Function 3.
- `scrape_links(page, base_url)` — Function 3: walk the Moodle activity cards (`.course-content` → `a.aalink` → owning `li.activity`, nearest `li.section`) and read each card's `modtype_*` class (kind), `data-activityname` (name), `a.aalink` href (url), and section. Writes enriched JSON.
- `looks_like_login(url)` / `wait_for_authenticated(page)` — detect when login is done (no Enter needed).
- `SKIP_KINDS` — module types that are NOT downloads (forum, assign, turnitintooltwo, subsection, quiz, ...); used to set the `download` flag.

The old `classify()` (URL-pattern labeling) is no longer the primary classifier in v1; `kind` now comes from the authoritative `modtype_*` CSS class on each card.

## Known facts & unknowns

| Item | Status |
| --- | --- |
| MyTIMES is Moodle-based | Confirmed |
| Authentication is required (OIDC / Microsoft account) | Confirmed |
| Basic `requests` scraping cannot access course content | Confirmed |
| Playwright is the chosen browser automation approach | Implemented (Functions 1-3 in v1) |
| Playwright authentication works | Verified (manual login + auto-detect, headless logic tested) |
| Card-scoped extractor reads real names + sections (Option A) | Implemented + verified against `Block_example.html` |
| `kind` from `modtype_*` class; `download` flag set from "File" suffix | Implemented + verified |
| Resource resolution mechanism is fully understood | Mostly (mod:resource → file served via pluginfile.php; download step not yet built) |
| Complete course ingestion works end-to-end (incl. download) | NOT yet — v1 stops at enriched link JSON; live MyTIMES run pending display + credentials |
| Embeddings are required | No — future phase |

## Roadmap

- **Phase 1 — MVP Ingestion:** authenticated course access → resource discovery → resolution → download → extraction → basic course knowledge base. Tag `v0.1.0` when this works end-to-end.
- **Phase 2 — Retrieval:** chunking → embeddings → vector database → retrieval.
- **Phase 3 — Assignment Agent:** builds on retrieval to assist with assignments.
