#!/usr/bin/env python3
"""MyTIMES scraper v3 — v2 plus a FIXED download step.

The v2 download used page.expect_download(), which only fires when Moodle
FORCES a download. Many resources are configured to OPEN in the browser
(Content-Disposition: inline / embedded PDF), so no download event fired and
those files were skipped. v3 instead INTERCEPTS the file response: whether
Moodle downloads or opens the file, the bytes are still transferred over HTTP,
so we capture that response and write it to disk. This handles both modes.

Usage:
    python3 mytimes_scraper_v3.py <URL> [-o mytimes_links.json] [-d ./downloads]

Functions 1-3 identical to v1/v2 (card-scoped enriched link collection with a
`download` flag). Function 4 (download_documents) saves each `download: true`
link to downloads/<Course Name>/<Section>/<name>.<ext> — the `<Course Name>`
folder is derived from the page title (e.g. 'Course: Software Testing
(202605-Lecture) | myTimes' -> 'Software Testing (202605-Lecture)'), the
`<Section>` tree mirrors the site's sections, filename is the scraped name,
and re-runs are deduplicated.
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

# MIME prefixes that mean "here is a real document/file to keep" (not a webpage).
# NOTE: application/octet-stream is deliberately EXCLUDED from the auto-accept
# list below via is_file_response; it is only accepted when a document extension
# or magic-bytes confirm it is actually a file.
DOC_CONTENT_TYPES = (
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument", "application/vnd.ms-",
    "application/vnd.oasis.opendocument", "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint", "application/vnd.ms-word",
    "image/", "application/zip",
    "text/csv", "text/plain", "application/vnd.google-apps",
)

# Extensions we are willing to write to disk. Script/executable extensions are
# NEVER accepted from a URL or guessed Content-Type — they mean "Moodle served
# the file via a .php/.aspx endpoint", not that the file IS code.
DOC_EXT = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "png",
           "jpg", "jpeg", "gif", "zip", "txt", "md", "rtf", "odt", "ods", "odp"}

# Magic-byte signatures -> extension (used as a final backstop when the server
# gives no usable filename/Content-Type). Covers the common office/pdf types.
MAGIC_EXT = (
    (b"%PDF", ".pdf"),
    (b"\x50\x4b\x03\x04", ".zip"),          # docx/pptx/xlsx are zip containers
    (b"\xd0\xcf\x11\xe0", ".doc"),           # legacy .doc/.xls/.ppt (OLE2)
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
)

# Characters that are unsafe in a file/dir name on common filesystems.
UNSAFE = re.compile(r'[\\/:*?"<>|]')


def safe_name(value):
    """Make a string safe to use as a file or directory name."""
    return UNSAFE.sub("-", (value or "").strip()).strip(".")


def course_name_from_title(page_title):
    """Extract a filesystem-safe course name from a page title.

    Input looks like:  'Course: Software Testing (202605-Lecture) | myTimes'
    Returns:            'Software Testing (202605-Lecture)'

    The leading 'Course:' label and the trailing '| myTimes' suffix are
    stripped, then the remainder is made filesystem-safe. If the title does
    not match the expected shape we still get a usable (if generic) folder.
    """
    title = (page_title or "").strip()
    head = title.split("|", 1)[0].strip()          # drop ' | myTimes' suffix
    m = re.match(r"^course:\s*(.+)$", head, re.I)   # drop 'Course:' label
    if m:
        head = m.group(1).strip()
    return safe_name(head) or "Unknown Course"


def extension_from_filename(filename):
    """Return the lowercase extension (with dot) from a filename, or ''."""
    m = re.search(r"\.([A-Za-z0-9]+)$", filename or "")
    return ("." + m.group(1).lower()) if m else ""


def content_type_ext(content_type):
    """Best-guess a file extension from a Content-Type (fallback only)."""
    ct = (content_type or "").lower()
    table = {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml": ".xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml": ".pptx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.ms-excel": ".xls",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "application/zip": ".zip",
        "text/csv": ".csv",
        "text/plain": ".txt",
    }
    for key, ext in table.items():
        if ct.startswith(key):
            return ext
    # application/octet-stream and other unknown types: DO NOT guess an extension
    # here (that is how we ended up with .php). Caller falls back to magic-bytes.
    return ""


def sanitize_doc_ext(ext):
    """Return ext only if it is a known document extension; else ''.

    This is the guard that prevents writing a `.php`/`.asp`/`.bin` file — those
    mean Moodle streamed the document through a script endpoint, not that the
    document IS code.
    """
    return ext if (ext.lower().lstrip(".") in DOC_EXT) else ""


def magic_ext(body):
    """Best-guess a document extension from the first bytes of the body."""
    head = body[:8]
    for sig, ext in MAGIC_EXT:
        if head.startswith(sig):
            # docx/pptx/xlsx are all zip containers; pick by caller context when
            # possible, but default to .zip only if nothing better is known.
            return ext
    return ""


def looks_like_login(url):
    """True if the current URL is a login / Microsoft-auth page."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    return (
        "login.microsoftonline.com" in parsed.netloc.lower()
        or "/auth/" in path
        or "/login/" in path
    )


def _should_capture(response):
    """Router predicate: capture any response that is NOT an HTML webpage.

    A real file (pdf/zip/image/octet-stream) is captured; an HTML link/Page
    wrapper is NOT — so it is never saved. The final keep/skip + extension
    decision happens at save time (magic-bytes sniff), not here.
    """
    ct = (response.headers.get("content-type") or "").lower()
    return not ct.startswith("text/html")


def looks_like_document(body):
    """True if the bytes begin with a known document/office/image signature."""
    return bool(magic_ext(body))


def is_file_response(response):
    """True if this HTTP response is a real document/file body we should keep.

    We deliberately do NOT treat a bare `application/octet-stream` or any
    `.php`/`.asp` URL as a file on its own — Moodle streams documents through
    script endpoints, so that would save a `.php`. A response counts as a file
    only if: (a) it has a known document Content-Type, OR (b) it carries a
    Content-Disposition with a filename having a real document extension.
    """
    ct = (response.headers.get("content-type") or "").lower()
    cd = (response.headers.get("content-disposition") or "").lower()
    cd_name = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    cd_ext = extension_from_filename(cd_name.group(1)) if cd_name else ""

    if any(ct.startswith(t) for t in DOC_CONTENT_TYPES):
        return True
    # Content-Disposition naming a REAL document (not .php) -> accept.
    if cd_ext and sanitize_doc_ext(cd_ext):
        return True
    # Fallback: a direct file URL (image/doc/zip) ending in a real extension.
    if FILE_EXT.search(urlparse(response.url).path):
        return True
    return False


async def scrape_links(page, base_url):
    """FUNCTION 3 (v1/v2/v3): walk Moodle activity cards and extract rich metadata."""
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

    FIX (v3): instead of waiting for a download EVENT (which only fires when
    Moodle forces a download), we intercept the file at the network layer. We
    route requests during the single resource navigation; the first request that
    returns a file body is fetched with route.fetch() (whose body stays readable
    independent of page navigation) and fulfilled back so the page still works.
    This captures BOTH "force download" and "open in browser" (inline) modes.
    DEDUP: if the target already exists, skip (re-runs are idempotent).
    """
    section = safe_name(link.get("section") or "Unknown")
    base = safe_name(link.get("name") or "file")
    if not base:
        base = "file"

    dest_dir = Path(downloads_dir) / section
    dest_dir.mkdir(parents=True, exist_ok=True)

    # DEDUP (skip-if-exists): if a file with this base name already exists, skip.
    existing = [p for p in dest_dir.glob(f"{base}.*") if p.is_file()]
    if existing:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "skipped", "reason": "already exists", "path": str(existing[0])}

    captured = {}

    async def on_route(route):
        # Fetch the request ourselves so we can read the body regardless of how
        # the page handles the response (download vs open/inline).
        try:
            fetched = await route.fetch()
        except Exception:
            await route.continue_()
            return
        if not captured and _should_capture(fetched):
            captured["resp"] = fetched
            captured["headers"] = dict(fetched.headers)
            captured["url"] = fetched.url
        # Fulfill with the fetched response so the page behaves normally.
        try:
            await route.fulfill(response=fetched)
        except Exception:
            pass

    await page.route("**/*", on_route)
    try:
        try:
            await page.goto(link["url"], wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        # Wait for the file response to be captured.
        for _ in range(100):
            if "resp" in captured:
                break
            await asyncio.sleep(0.1)
    finally:
        await page.unroute("**/*", on_route)

    if "resp" not in captured:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "error", "reason": "no file response captured", "path": None}

    try:
        body = await captured["resp"].body()
    except Exception as e:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "error", "reason": f"file body read failed: {e}", "path": None}

    if not body:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "error", "reason": "file response had empty body", "path": None}

    headers = captured.get("headers", {})
    cd = headers.get("content-disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    cd_name = m.group(1).strip() if m else ""

    # Decide the extension, REFUSING anything that isn't a real document type:
    #   1) Content-Disposition filename extension (e.g. "Lecture.pptx") — sanitized
    #   2) URL path extension, ONLY if it's a real document (rejects .php/.asp)
    #   3) Content-Type -> extension table
    #   4) magic-bytes sniff of the body (pdf / zip[docx,pptx,xlsx] / ole2 / png...)
    # The old behaviour fell through to the .php URL extension, which is why we
    # saved "name.php" for Week 10/Week 9. Now that can never happen.
    ext = sanitize_doc_ext(extension_from_filename(cd_name))
    if not ext:
        url_ext = extension_from_filename(urlparse(captured.get("url", "")).path)
        if sanitize_doc_ext(url_ext):
            ext = url_ext
    if not ext:
        ext = sanitize_doc_ext(content_type_ext(headers.get("content-type")))
    if not ext:
        me = magic_ext(body)
        ext = me if (me == ".pdf" or me == ".png" or me == ".jpg" or me == ".gif") else ""
    if not ext and magic_ext(body) in (".zip", ".doc"):
        # docx/pptx/xlsx are zip; .doc/.xls/.ppt are OLE2 — but we can't tell which
        # from bytes alone, so only use this when a document Content-Type hinted it.
        ct = (headers.get("content-type") or "").lower()
        if "word" in ct:
            ext = ".docx" if magic_ext(body) == ".zip" else ".doc"
        elif "presentation" in ct or "powerpoint" in ct:
            ext = ".pptx" if magic_ext(body) == ".zip" else ".ppt"
        elif "excel" in ct or "spreadsheet" in ct:
            ext = ".xlsx" if magic_ext(body) == ".zip" else ".xls"
        else:
            # Unknown container; keep .zip (a real, openable, non-script extension).
            ext = magic_ext(body) or ""
    if not ext:
        # Final backstop: sniff the bytes. Only accept genuine document signatures;
        # an HTML link/Page wrapper is NOT a document and must not be saved.
        if looks_like_document(body):
            ext = magic_ext(body)
    if not ext:
        # Not a recognizable document (e.g. an HTML webpage/link wrapper) -> skip.
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "error", "reason": "response is not a document (webpage/link wrapper) — skipped",
                "path": None}
    if not ext:
        ext = ".pdf"  # safe default for Moodle resources
    save_path = dest_dir / f"{base}{ext}"
    if save_path.exists():
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "skipped", "reason": "already exists", "path": str(save_path)}
    save_path.write_bytes(body)
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
                # Nest everything under downloads/<Course Name>/ so each run
                # lands in its own folder (extracted from the page title).
                course_dir = Path(downloads_dir) / course_name_from_title(output_data["page_title"])
                course_dir.mkdir(parents=True, exist_ok=True)
                print(f"[download] course folder: {course_dir}")
                download_results = await download_documents(page, links, course_dir)
                downloaded = sum(1 for r in download_results if r["status"] == "downloaded")
                skipped = sum(1 for r in download_results if r["status"] == "skipped")
                errored = sum(1 for r in download_results if r["status"] == "error")
                print(f"[download] {downloaded} downloaded, {skipped} skipped (existing), {errored} errors")
            elif not do_download:
                print("[download] skipped (--no-download)")
        finally:
            await browser.close()


# Command-line interface; reads what you wrote in terminal
# python3 mytimes_scraper_v3.py https://mytimes.taylors.edu.my/course/view.php?id=47163
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
