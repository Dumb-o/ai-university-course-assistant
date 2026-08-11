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

MyTIMES authenticates via a **Taylor's Microsoft account** (MFA required). The script defaults to **manual login**: complete email + password + MFA in the browser window once — the authenticated session is saved to `mytimes_state/` and reused on later runs.

Optional auto-fill:

```bash
export TAYLORS_EMAIL="name@taylors.edu.my"
export TAYLORS_PASSWORD="your_password"
python3 scrape_script/mytimes_scraper.py --auto-fill "https://mytimes.taylors.edu.my/course/view.php?id=47163"
```

The scraper writes `mytimes_links.json` containing every discovered link, classified by type (`mod:resource`, `mod:assign`, `file`, `course`, `other`).

## Known facts & unknowns

| Item | Status |
| --- | --- |
| MyTIMES is Moodle-based | Confirmed |
| Authentication is required (OIDC / Microsoft account) | Confirmed |
| Basic `requests` scraping cannot access course content | Confirmed |
| Playwright is the chosen browser automation approach | Planned |
| Playwright authentication works | Not yet tested |
| Resource resolution mechanism is fully understood | Not yet tested |
| Complete course ingestion works end-to-end | Not yet implemented |
| Embeddings are required | No — future phase |

## Roadmap

- **Phase 1 — MVP Ingestion:** authenticated course access → resource discovery → resolution → download → extraction → basic course knowledge base. Tag `v0.1.0` when this works end-to-end.
- **Phase 2 — Retrieval:** chunking → embeddings → vector database → retrieval.
- **Phase 3 — Assignment Agent:** builds on retrieval to assist with assignments.
