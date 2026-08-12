#!/usr/bin/env python3
"""MyTIMES scraper v1 — open a course URL in Chromium, log in manually, collect
links with RICH metadata (Option A: scoped to the activity cards, not all <a>).

Usage:
    python3 mytimes_scraper_v1.py <URL> [-o mytimes_links.json]

Function 1 (open_browser_at): start Chromium, go to the URL.
Function 2 (manual login): complete Microsoft email + password + MFA in the
    browser window. The scrape starts AUTOMATICALLY once you leave the login page
    (no Enter needed).
Function 3 (scrape_links): walk the Moodle activity cards and pull, per card:
    section / section_number (from the nearest li.section), kind (from the
    modtype_* class), name (from data-activityname), url (from a.aalink), and
    file_type (the hidden "File"/"Page" suffix). Writes mytimes_links.json.

This version is built on mytimes_scraper_v0.py but replaces the flat "grab every
<a href>" approach with a card-scoped extractor, so the JSON carries real names
and section organization instead of empty text/title fields.

MyTIMES is Moodle-based. Activity cards expose stable attributes:
    li.section[data-for="section"]        -> data-sectionname, data-number
    li.activity[data-for="cmitem"]        -> modtype_* class (the kind)
    div.activity-item[data-activityname]  -> the real activity name
    a.aalink                              -> the activity URL
    span.instancename > span.accesshide   -> hidden "File"/"Page" suffix
"""

import argparse  # Reading command line arguments
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# Direct file-download links (fallback signal when file_type suffix is missing).
FILE_EXT = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|csv|png|jpe?g|gif|zip|txt|html?)$", re.I)

# Module types that are NOT downloadable content (navigation / portals / pages).
SKIP_KINDS = {"forum", "assign", "turnitintooltwo", "subsection", "quiz", "choice",
              "url", "label", "feedback", "survey", "lesson", "workshop", "chat",
              "data", "glossary", "wiki", "book", "scorm", "lti", "page"}


def looks_like_login(url):
    """True if the current URL is a login / Microsoft-auth page."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    return (
        "login.microsoftonline.com" in parsed.netloc.lower()
        or "/auth/" in path
        or "/login/" in path
    )


async def scrape_links(page, base_url):
    """FUNCTION 3 (v1): walk Moodle activity cards and extract rich metadata.

    Scoped to `.course-content > li.activity[data-for="cmitem"]` (NOT every <a>),
    so we only collect course-content activities and each one carries its own
    name/type/section read from the card's own attributes. Returns a list of
    dicts with: section, section_number, kind, name, url, file_type, download.
    """
    try:
        await page.wait_for_selector(".course-content", timeout=30000)
    except Exception:
        print("[scrape] warning: could not find .course-content — the page may not be the course page")
    await page.wait_for_timeout(2000)

    # One in-browser pass over the real activity anchors (a.aalink). Climbing UP to
    # the owning li.activity gives the kind + name; climbing to the nearest
    # li.section gives the (possibly nested) section. This reliably captures
    # resources that live inside delegated subsections (e.g. MLO2 / MLO3).
    raw = await page.eval_on_selector_all(
        "a.aalink",
        """els => els.map(a => {
            const li = a.closest('li.activity[data-for="cmitem"]');
            if (!li) return null;
            const kindClass = [...li.classList].find(c => c.startsWith('modtype_'));
            const kind = kindClass ? kindClass.replace('modtype_', '') : 'other';
            const card = li.querySelector('[data-activityname]');
            const name = card ? (card.getAttribute('data-activityname') || '').trim() : '';
            const sec = li.closest('li.section');
            const section = sec ? (sec.getAttribute('data-sectionname') || '').trim() : '';
            const sectionNumber = sec ? (sec.getAttribute('data-number') || '') : '';
            const inst = li.querySelector('span.instancename');
            let fileType = '';
            if (inst) {
                const hidden = inst.querySelector('span.accesshide');
                fileType = (hidden ? hidden.textContent : inst.textContent || '').trim();
            }
            return { kind, name, url: a.href, section, sectionNumber, fileType };
        })""",
    )
    raw = [r for r in raw if r is not None]

    seen, links = set(), []
    for item in raw:
        url = (item.get("url") or "").strip()
        if not url or url.startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        if url in seen:
            continue
        seen.add(url)

        kind = item.get("kind") or "other"
        file_type = (item.get("fileType") or "").strip()
        name = (item.get("name") or "").strip()

        # Decide downloadability (Option B, now possible because we have evidence):
        # - skip clearly non-content module types
        # - for resources, only download when the hidden suffix says "File"
        #   (or the URL ends in a known file extension). "Page"/"URL"/ambiguous
        #   resources are recorded but NOT marked for download.
        if kind in SKIP_KINDS:
            download = False
        elif kind == "resource":
            is_file = (file_type.lower() == "file") or bool(FILE_EXT.search(urlparse(url).path))
            download = is_file
        else:
            download = False

        links.append({
            "section": item.get("section") or "Unknown",
            "section_number": item.get("sectionNumber") or "",
            "kind": kind,
            "name": name,
            "url": url,
            "file_type": file_type,
            "download": download,
        })
    return links


async def wait_for_authenticated(page, timeout=300):
    """Wait until the browser leaves the login flow (you finished logging in).

    Scraping then starts automatically — no Enter press required.
    """
    print(f"[login] waiting up to {timeout // 60} minutes for authentication to complete...")
    try:
        await page.wait_for_url(lambda url: not looks_like_login(url), timeout=timeout * 1000)
        print(f"[login] authenticated -> {page.url}")
    except Exception:
        print(f"[login] not authenticated after {timeout // 60} minutes; continuing anyway")


async def open_browser_at(url, output="mytimes_links.json"):
    """FUNCTION 1 (+ covers Function 2 manual login) then call Function 3."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            print(f"[open] navigating to {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            print(f"[page] {await page.title()} ({page.url})")
            if looks_like_login(page.url):
                print("[login] Please log in manually in the browser window (email + password + MFA).")
                print("[login] Scraping will start automatically once you are authenticated.")
                await wait_for_authenticated(page, timeout=300)
            else:
                print("[login] already authenticated (session restored) — scraping now.")

            # You are now authenticated (or timed out) — run Function 3.
            print(f"[scrape] collecting links from {page.url}")
            links = await scrape_links(page, page.url)

            output_data = {
                "course_url": url,
                "final_url": page.url,
                "page_title": await page.title(),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "total": len(links),
                "downloadable": sum(1 for l in links if l["download"]),
                "links": links,
            }
            Path(output).write_text(json.dumps(output_data, indent=2), encoding="utf-8")

            print(f"[done] found {len(links)} activities "
                  f"({output_data['downloadable']} downloadable) -> {output}")
            for link in links[:20]:
                flag = "DL" if link["download"] else "  "
                print(f"  [{flag}] [{link['kind']}] {link['section']} :: {link['name']}")
            if len(links) > 20:
                print(f"  ... and {len(links) - 20} more")
        finally:
            await browser.close()


# Command-line interface; reads what you wrote in terminal
# python3 mytimes_scraper_v1.py https://mytimes.taylors.edu.my/course/view.php?id=47163
def parse_args():
    # parser stores the parsed data bhanam na i.e. https://mytimes.taylors.edu.my/course/view.php?id=47163
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "url",
        help="URL to open, e.g. https://mytimes.taylors.edu.my/course/view.php?id=47163",
    )
    parser.add_argument(
        "-o", "--output", default="mytimes_links.json",
        help="output JSON file (default: mytimes_links.json)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(open_browser_at(args.url, args.output))
