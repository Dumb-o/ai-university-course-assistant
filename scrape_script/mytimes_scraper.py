#!/usr/bin/env python3
"""MyTIMES scraper — open a course URL in Chromium, log in manually, collect links.

Usage:
    python3 mytimes_scraper.py <URL> [-o mytimes_links.json]

Function 1 (open_browser_at): start Chromium, go to the URL.
Function 2 (manual login): you complete Microsoft email + password + MFA in the
    browser window. The scrape starts AUTOMATICALLY once you leave the login page
    (no Enter needed).
Function 3 (scrape_links): collect every <a href> on the page + its metadata and
    write mytimes_links.json so you can decide what to download.

MyTIMES is Moodle-based: resources appear as
    https://mytimes.taylors.edu.my/mod/resource/view.php?id=<id>
    https://mytimes.taylors.edu.my/mod/assign/view.php?id=<id>
classify() labels each link mod:resource / mod:assign / file / course / other.
"""

import argparse  # Reading command line arguments
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

# Direct file-download links we might want to grab later.
FILE_EXT = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|csv|png|jpe?g|gif|zip|txt|html?)$", re.I)


def classify(href):
    """Label a link by what it is on the Moodle site."""
    match = re.search(r"/mod/([a-z_]+)/", urlparse(href).path.lower())
    if match:
        return f"mod:{match.group(1)}"      # mod:resource, mod:assign, etc.
    if FILE_EXT.search(urlparse(href).path):
        return "file"
    if "/course/" in urlparse(href).path:
        return "course"
    return "other"


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
    """FUNCTION 3: collect every link on the page + its metadata.

    Waits for Moodle course content to render, then reads all <a href> elements
    (with their visible text + title), resolves relative URLs, de-duplicates,
    classifies, and returns the list.
    """
    try:
        await page.wait_for_selector("#page-content, .course-content", timeout=30000)
    except Exception:
        print("[scrape] warning: could not find course content — the page may not be the course page")
    await page.wait_for_timeout(2000)

    raw = await page.eval_on_selector_all(
        "a[href]",
        "els => els.map(a => ({"
        "  text: (a.innerText || '').trim(),"
        "  title: (a.title || '').trim(),"
        "  href: a.href"
        "}))",
    )

    seen, links = set(), []
    for item in raw:
        href = (item.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        links.append({
            "url": full,
            "text": item.get("text"),
            "title": item.get("title"),
            "kind": classify(full),
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
                "links": links,
            }
            Path(output).write_text(json.dumps(output_data, indent=2), encoding="utf-8")

            print(f"[done] found {len(links)} links -> {output}")
            for link in links[:20]:
                print(f"  [{link['kind']}] {link['url']}  ::  {link['text'][:60]}")
            if len(links) > 20:
                print(f"  ... and {len(links) - 20} more")
        finally:
            await browser.close()


# Command-line interface; reads what you wrote in terminal
# python3 mytimes_scraper.py https://mytimes.taylors.edu.my/course/view.php?id=47163
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
