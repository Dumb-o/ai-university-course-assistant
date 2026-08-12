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
├── mytimes_scraper.py   # Playwright login + course link scraper
└── scrape_links.py      # prototype: unauthenticated requests scraper (does NOT work on Moodle)
```

## Prerequisites

- Python 3.14+ and a virtual environment
- Playwright + Chromium

## Running the scraper

```bash
source .venv/bin/activate
pip install playwright beautifulsoup4 requests fastapi uvicorn
playwright install chromium
python3 scrape_script/mytimes_scraper.py "https://mytimes.taylors.edu.my/course/view.php?id=47163"
```

MyTIMES authenticates via a **Taylor's Microsoft account** (MFA required). The script opens a visible Chromium window; complete email + password + MFA in the browser. **No Enter press is needed** — once you finish login and the URL leaves the Microsoft login page, the scraper automatically collects every link on the course page and writes `mytimes_links.json`.

Optional output / download flags:
```bash
python3 scrape_script/mytimes_scraper.py "URL" -o custom_links.json
python3 scrape_script/mytimes_scraper.py "URL" -d ./my_downloads   # set download folder
python3 scrape_script/mytimes_scraper.py "URL" --no-download       # collect links only, skip downloads
```

The scraper writes `mytimes_links.json` containing every discovered link, classified by type (`mod:resource`, `mod:assign`, `mod:quiz`, `file`, `course`, `other`), with each link's visible text, title, and full URL. It then **automatically downloads the essential documents** (`mod:resource` + `file` kinds) into a `downloads/` folder next to the JSON, reusing your authenticated session — no re-login. Use the `kind` field to understand what was fetched; `--no-download` disables the download step if you only want the link map.

### Script structure (`scrape_script/mytimes_scraper.py`)
- `open_browser_at(url, output, downloads_dir, do_download)` — Function 1 + 2: launch Chromium, navigate, wait for manual auth to finish automatically, then run 3 + 4.
- `scrape_links(page, base_url)` — Function 3: collect + classify all `<a href>` into JSON.
- `download_documents(page, links, downloads_dir)` / `download_link(...)` — Function 4: download `mod:resource` + `file` links into the temp folder.
- `classify(href)` — label a URL as `mod:*`, `file`, `course`, or `other`.
- `looks_like_login(url)` / `wait_for_authenticated(page)` — detect when login is done (no Enter needed).

## Known facts & unknowns

| Item | Status |
| --- | --- |
| MyTIMES is Moodle-based | Confirmed |
| Authentication is required (OIDC / Microsoft account) | Confirmed |
| Basic `requests` scraping cannot access course content | Confirmed |
| Playwright is the chosen browser automation approach | Implemented (Functions 1-4) |
| Playwright authentication works | Verified (manual login + auto-detect, headless logic tested) |
| Resource resolution mechanism is fully understood | Mostly (mod:resource → pluginfile.php anchor followed; may need tweaks on live site) |
| Complete course ingestion works end-to-end | Download mechanism verified headlessly; live MyTIMES run pending display + credentials |
| Embeddings are required | No — future phase |

## Roadmap

- **Phase 1 — MVP Ingestion:** authenticated course access → resource discovery → resolution → download → extraction → basic course knowledge base. Tag `v0.1.0` when this works end-to-end.
- **Phase 2 — Retrieval:** chunking → embeddings → vector database → retrieval.
- **Phase 3 — Assignment Agent:** builds on retrieval to assist with assignments.
