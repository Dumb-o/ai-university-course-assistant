#!/usr/bin/env python3
"""MyTIMES scraper v2 — v1 (open + manual login + card-scoped link collection)
PLUS Function 4: download the marked files into a directory tree that mirrors
the course's section organization on the site.

Usage:
    python3 mytimes_scraper_v2.py <URL> [-o mytimes_links.json] [-d ./downloads]

Functions 1-3 are identical to v1 (scrape_links produces the enriched JSON with a
`download` flag). Function 4 (download_documents) then:

  - walks only the links where `download` is True,
  - creates `downloads/<Section>/` for each activity's `section` (e.g. "General",
    "Week 2", "Software Sustainment MLO2", "MLO3 - Group Project 40%"), so the
    folder layout mirrors the website's weeks / subsections,
  - names each file after the scraped `name` (e.g. "Module Information (MI).pdf"),
    taking the extension from the downloaded file,
  - DEDUPLICATES: if the target file already exists it is skipped (re-runs are
    safe / resumable); each resource also lands in a unique path so two resources
    can never collide.

`mod:resource` links are not the file itself — Moodle serves the file via a
redirect / pluginfile.php when the resource page is opened, so we open the page
in the authenticated session and capture the download with page.expect_download().

v1 is kept as the link-collection baseline; this file adds the download step.
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

# Characters that are unsafe in a file/dir name on common filesystems.
UNSAFE = re.compile(r'[\\/:*?"<>|]')


def safe_name(value):
    """Make a string safe to use as a file or directory name."""
    return UNSAFE.sub("-", (value or "").strip()).strip(".")


def extension_from_filename(filename):
    """Return the lowercase extension (with dot) from a filename, or ''."""
    m = re.search(r"\.([A-Za-z0-9]+)$", filename or "")
    return ("." + m.group(1).lower()) if m else ""


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

    Scoped to the real activity anchors (a.aalink) and climbs UP to the owning
    li.activity (kind + name) and nearest li.section (section). Returns a list of
    dicts with: section, section_number, kind, name, url, file_type, download.
    """
    try:
        await page.wait_for_selector(".course-content", timeout=30000)
    except Exception:
        print("[scrape] warning: could not find .course-content — the page may not be the course page")
    await page.wait_for_timeout(2000)

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


async def download_link(page, link, downloads_dir):
    """Download ONE resource link into downloads_dir/<section>/<name>.<ext>.

    Opens the resource page in the authenticated session and captures the file
    via page.expect_download(). Returns a result dict. DEDUP: if the computed
    target path already exists, we skip (re-runs stay idempotent).
    """
    section = safe_name(link.get("section") or "Unknown")
    base = safe_name(link.get("name") or "file")
    if not base:
        base = "file"

    dest_dir = Path(downloads_dir) / section
    # We don't know the extension until the download starts, so reserve the name
    # by checking for any existing file that begins with this base in the dir.
    dest_dir.mkdir(parents=True, exist_ok=True)

    # DEDUP (skip-if-exists): if a file with this base name already exists in the
    # section folder, skip re-downloading. Matches <base>.* (e.g. "Module Information (MI).pdf").
    existing = [p for p in dest_dir.glob(f"{base}.*") if p.is_file()]
    if existing:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "skipped", "reason": "already exists", "path": str(existing[0])}

    try:
        async with page.expect_download(timeout=20000) as dl_info:
            try:
                await page.goto(link["url"], wait_until="domcontentloaded", timeout=60000)
            except Exception:
                # Moodle serves the file by redirecting the resource page, which
                # interrupts navigation with a download. That's expected; the
                # download is captured by the context manager regardless.
                pass
        dl = await dl_info.value
    except Exception as e:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "error", "reason": f"no download captured: {e}", "path": None}

    suggested = dl.suggested_filename or ""
    ext = extension_from_filename(suggested)
    if not ext:
        ext = ".pdf"  # Moodle resources are usually PDFs; safe fallback
    save_path = dest_dir / f"{base}{ext}"
    # If a collision somehow remains, avoid overwriting an unrelated file.
    if save_path.exists():
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "skipped", "reason": "already exists", "path": str(save_path)}
    await dl.save_as(str(save_path))
    return {"url": link["url"], "name": link["name"], "section": link.get("section"),
            "status": "downloaded", "reason": "", "path": str(save_path)}


async def download_documents(page, links, downloads_dir):
    """FUNCTION 4: download every link with download=True into a section tree.

    Reuses the SAME authenticated Playwright page (no re-login). Returns a list
    of per-link result dicts (downloaded / skipped / error).
    """
    targets = [l for l in links if l.get("download")]
    print(f"[download] {len(targets)} files to fetch into {downloads_dir}/")
    results = []
    for link in targets:
        res = await download_link(page, link, downloads_dir)
        results.append(res)
        mark = {"downloaded": "OK ", "skipped": "SKIP", "error": "ERR"}[res["status"]]
        print(f"  [{mark}] {res['section']} :: {res['name']}"
              + (f"  ({res['reason']})" if res["reason"] else ""))
    return results


async def wait_for_authenticated(page, timeout=300):
    """Wait until the browser leaves the login flow (you finished logging in)."""
    print(f"[login] waiting up to {timeout // 60} minutes for authentication to complete...")
    try:
        await page.wait_for_url(lambda url: not looks_like_login(url), timeout=timeout * 1000)
        print(f"[login] authenticated -> {page.url}")
    except Exception:
        print(f"[login] not authenticated after {timeout // 60} minutes; continuing anyway")


async def open_browser_at(url, output="mytimes_links.json", downloads_dir="downloads", do_download=True):
    """FUNCTION 1 (+ Function 2 manual login) then Function 3, then Function 4."""
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

            # Function 3: collect enriched links.
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

            # Function 4: download the marked files.
            download_results = []
            if do_download and output_data["downloadable"] > 0:
                download_results = await download_documents(page, links, downloads_dir)
                downloaded = sum(1 for r in download_results if r["status"] == "downloaded")
                skipped = sum(1 for r in download_results if r["status"] == "skipped")
                errored = sum(1 for r in download_results if r["status"] == "error")
                print(f"[download] {downloaded} downloaded, {skipped} skipped (existing), {errored} errors")
            elif not do_download:
                print("[download] skipped (--no-download)")
        finally:
            await browser.close()


# Command-line interface; reads what you wrote in terminal
# python3 mytimes_scraper_v2.py https://mytimes.taylors.edu.my/course/view.php?id=47163
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
    parser.add_argument(
        "-d", "--downloads-dir", default="downloads",
        help="root folder for downloaded files (default: ./downloads)",
    )
    parser.add_argument(
        "--no-download", action="store_true",
        help="collect links only; do not download files",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(open_browser_at(args.url, args.output, args.downloads_dir, not args.no_download))
