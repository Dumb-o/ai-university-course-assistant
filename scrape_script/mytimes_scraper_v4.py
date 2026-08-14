#!/usr/bin/env python3
"""MyTIMES scraper v4 — behavior-identical consolidation of v3.

Same pipeline: manual Microsoft-OIDC login -> card-scoped enriched link
collection -> section-tree download with open-in-browser capture and extension
hardening. The messy extension-resolution, Content-Disposition parsing, and
de-duplication logic that was scattered inline inside v3's download_link now
lives in small PURE, unit-tested helpers: parse_cd_filename(), resolve_extension(),
_dedup_existing(), save_captured_file(). Every observable action of v3 is preserved
(verified by tests/test_scraper_v4.py).

Usage:
    python3 mytimes_scraper_v4.py <URL> [-o mytimes_links.json] [-d ./downloads]
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

# Moodle serves real activity content from a module component area, e.g.
# /pluginfile.php/<ctx>/mod_resource/content/1/Lecture.pptx
# Theme chrome (icons, logos) uses theme components instead — theme/image.php or
# /pluginfile.php/1/theme_boost/logo/... — so matching `mod_*` is what separates
# "the file the user wants" from "a picture the page happens to load".
MODULE_CONTENT_PATH = re.compile(r"/mod_[a-z0-9]+/", re.I)

# Module types that are NOT downloadable content (navigation / portals / pages).
SKIP_KINDS = {"forum", "assign", "turnitintooltwo", "subsection", "quiz", "choice",
              "url", "label", "feedback", "survey", "lesson", "workshop", "chat",
              "data", "glossary", "wiki", "book", "scorm", "lti", "page"}

# MIME prefixes that mean "here is a real document/file to keep" (not a webpage).
# NOTE: application/octet-stream is deliberately EXCLUDED via sanitize_doc_ext;
# it is only accepted when a document extension or magic-bytes confirm it.
DOC_CONTENT_TYPES = (
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument", "application/vnd.ms-",
    "application/vnd.oasis.opendocument", "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint", "application/vnd.ms-word",
    "image/", "application/zip",
    "text/csv", "text/plain", "application/vnd.google-apps",
)

# Extensions we are willing to write to disk. Script/executable extensions are
# NEVER accepted from a URL or guessed Content-Type.
DOC_EXT = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "png",
           "jpg", "jpeg", "gif", "zip", "txt", "md", "rtf", "odt", "ods", "odp"}

# Content-Type -> extension (fallback only). octet-stream/unknowns are omitted
# on purpose so we never guess a .php from a streamed script endpoint.
CT_EXT = (
    ("application/pdf", ".pdf"), ("application/msword", ".doc"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml", ".docx"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml", ".xlsx"),
    ("application/vnd.openxmlformats-officedocument.presentationml", ".pptx"),
    ("application/vnd.ms-powerpoint", ".ppt"), ("application/vnd.ms-excel", ".xls"),
    ("image/png", ".png"), ("image/jpeg", ".jpg"), ("image/gif", ".gif"),
    ("application/zip", ".zip"), ("text/csv", ".csv"), ("text/plain", ".txt"),
)

# Magic-byte signatures -> extension (final backstop when server gives nothing).
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

# Content-Disposition filename parser (DRY: used by is_file_response + save core).
CD_FILENAME = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.I)

# How to disambiguate a zip/OLE2 container when only magic-bytes are known.
# Keyed by a substring of the Content-Type; value = (zip_container_ext, ole2_ext).
CONTAINER_HINTS = {
    "word": (".docx", ".doc"),
    "presentation": (".pptx", ".ppt"),
    "powerpoint": (".pptx", ".ppt"),
    "excel": (".xlsx", ".xls"),
    "spreadsheet": (".xlsx", ".xls"),
}


# --------------------------------------------------------------------------- #
# Names / sanitization
# --------------------------------------------------------------------------- #
def safe_name(value):
    """Make a string safe to use as a file or directory name."""
    return UNSAFE.sub("-", (value or "").strip()).strip(".")


def course_name_from_title(page_title):
    """Extract a filesystem-safe course name from a page title.

    'Course: Software Testing (202605-Lecture) | myTimes' -> 'Software Testing (202605-Lecture)'
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
    for key, ext in CT_EXT:
        if ct.startswith(key):
            return ext
    # application/octet-stream and other unknowns: DO NOT guess (avoid .php).
    return ""


def sanitize_doc_ext(ext):
    """Return ext only if it is a known document extension; else ''.

    Guard that prevents writing a `.php`/`.asp`/`.bin` file.
    """
    return ext if (ext.lower().lstrip(".") in DOC_EXT) else ""


def magic_ext(body):
    """Best-guess a document extension from the first bytes of the body."""
    head = (body or b"")[:8]
    for sig, ext in MAGIC_EXT:
        if head.startswith(sig):
            return ext
    return ""


# --------------------------------------------------------------------------- #
# Consolidated pure helpers (the v3 inline logic, factored out)
# --------------------------------------------------------------------------- #
def parse_cd_filename(content_disposition):
    """Extract the filename from a Content-Disposition header, or ''."""
    if not content_disposition:
        return ""
    m = CD_FILENAME.search(content_disposition)
    return m.group(1).strip() if m else ""


def resolve_extension(cd_name, url, content_type, body):
    """Decide a file extension, REFUSING anything that isn't a real document.

    Order (mirrors v3 exactly):
      1) Content-Disposition filename extension (e.g. "Lecture.pptx") — sanitized
      2) URL path extension, ONLY if it's a real document (rejects .php/.asp)
      3) Content-Type -> extension table
      4) magic-bytes sniff of the body (pdf / zip / ole2 / png / jpg / gif)
         zip & OLE2 are disambiguated by a Content-Type hint when present.
    Returns '' when the response is not a recognizable document (e.g. an HTML
    link/Page wrapper) so the caller can SKIP rather than save a webpage.
    """
    # 1) Content-Disposition filename extension
    ext = sanitize_doc_ext(extension_from_filename(cd_name))
    if ext:
        return ext
    # 2) URL path extension, ONLY if it's a real document (rejects .php/.asp)
    url_ext = extension_from_filename(urlparse(url).path)
    if sanitize_doc_ext(url_ext):
        return url_ext
    # 3) Content-Type -> extension table
    ext = sanitize_doc_ext(content_type_ext(content_type))
    if ext:
        return ext
    # 4) magic-bytes sniff, with container disambiguation by Content-Type hint
    me = magic_ext(body)
    if not me:
        return ""
    if me in (".pdf", ".png", ".jpg", ".gif"):
        return me
    if me in (".zip", ".doc"):
        ct = (content_type or "").lower()
        for key, (zip_ext, ole_ext) in CONTAINER_HINTS.items():
            if key in ct:
                return zip_ext if me == ".zip" else ole_ext
        # Unknown container -> keep a real, openable, non-script extension.
        return me
    return sanitize_doc_ext(me) or ""


def _dedup_existing(dest_dir, base):
    """Return the first existing `<base>.*` file in dest_dir, or None."""
    matches = [p for p in dest_dir.glob(f"{base}.*") if p.is_file()]
    return matches[0] if matches else None


# --------------------------------------------------------------------------- #
# Login / response classification
# --------------------------------------------------------------------------- #
def looks_like_login(url):
    """True if the current URL is a login / Microsoft-auth page."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    return (
        "login.microsoftonline.com" in parsed.netloc.lower()
        or "/auth/" in path
        or "/login/" in path
    )


def is_file_response(response):
    """True if this HTTP response is a real document/file body we should keep.

    A bare `application/octet-stream` or `.php`/`.asp` URL is NOT a file on its
    own. A response counts as a file only if: (a) it has a known document
    Content-Type, OR (b) it carries a Content-Disposition naming a real document
    extension, OR (c) it is a direct file URL ending in a real extension.

    IMAGES are a special case: a resource page embeds theme icons/logos that are
    real `image/*` responses but are NOT the resource. An image only counts when
    it comes from a module CONTENT area (`/mod_<something>/`) or carries an
    attachment Content-Disposition; a `theme*` component is UI chrome (ignored).
    """
    ct = (response.headers.get("content-type") or "").lower()
    cd = response.headers.get("content-disposition") or ""
    cd_ext = extension_from_filename(parse_cd_filename(cd))

    path = urlparse(response.url).path.lower()
    is_image = ct.startswith("image/")
    is_module_content = bool(MODULE_CONTENT_PATH.search(path))
    is_attachment = "attachment" in cd.lower()

    if is_image and not (is_module_content or is_attachment):
        return False

    if any(ct.startswith(t) for t in DOC_CONTENT_TYPES):
        return True
    if cd_ext and sanitize_doc_ext(cd_ext):
        return True
    if FILE_EXT.search(path):
        return True
    return False


# --------------------------------------------------------------------------- #
# FUNCTION 3: card-scoped enriched link collection
# --------------------------------------------------------------------------- #
async def scrape_links(page, base_url):
    """Walk Moodle activity cards and extract rich metadata (same as v1/v2/v3)."""
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


# --------------------------------------------------------------------------- #
# FUNCTION 4: download every `download: true` link into a section tree
# --------------------------------------------------------------------------- #
def save_captured_file(link, downloads_dir, captured):
    """Write one captured file response to downloads_dir/<section>/<name>.<ext>.

    Pure (no browser). Handles dedup + extension resolution + the HTML-wrapper
    skip. `captured` is {"url", "headers", "body"} with body already resolved to
    bytes. Returns a result dict (downloaded / skipped / error).
    """
    section = safe_name(link.get("section") or "Unknown")
    base = safe_name(link.get("name") or "file") or "file"

    dest_dir = Path(downloads_dir) / section
    dest_dir.mkdir(parents=True, exist_ok=True)

    # DEDUP (skip-if-exists): re-runs are idempotent.
    existing = _dedup_existing(dest_dir, base)
    if existing:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "skipped", "reason": "already exists", "path": str(existing)}

    headers = captured.get("headers", {})
    cd_name = parse_cd_filename(headers.get("content-disposition"))

    ext = resolve_extension(cd_name, captured.get("url", ""),
                            headers.get("content-type"), captured.get("body", b""))

    if not ext:
        # Not a recognizable document (e.g. an HTML webpage/link wrapper) -> skip.
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "error",
                "reason": "response is not a document (webpage/link wrapper) — skipped",
                "path": None}

    save_path = dest_dir / f"{base}{ext}"
    if _dedup_existing(dest_dir, base):
        # Belt-and-suspenders against a concurrent write between the two checks.
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "skipped", "reason": "already exists", "path": str(save_path)}
    save_path.write_bytes(captured["body"])
    return {"url": link["url"], "name": link["name"], "section": link.get("section"),
            "status": "downloaded", "reason": "", "path": str(save_path)}


async def download_link(page, link, downloads_dir):
    """Download ONE resource link via request routing (captures open OR download).

    FIX (v3, kept in v4): we intercept the file at the network layer so both
    "force download" and "open in browser" (inline) modes are captured. Dedup and
    extension hardening happen in save_captured_file().
    """
    captured = {}

    async def on_route(route):
        try:
            fetched = await route.fetch()
        except Exception:
            await route.continue_()
            return
        if not captured and is_file_response(fetched):
            captured["url"] = fetched.url
            captured["headers"] = dict(fetched.headers)
            try:
                captured["body"] = await fetched.body()
            except Exception:
                captured["body"] = b""
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
        # Wait for the file response to be captured. On an embedded/open-in-browser
        # resource the wrapper HTML loads assets FIRST; the real file comes later,
        # so keep waiting rather than latching onto the first non-HTML response.
        for _ in range(200):
            if "body" in captured:
                break
            await asyncio.sleep(0.1)
    finally:
        await page.unroute("**/*", on_route)

    if "body" not in captured:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "error", "reason": "no file response captured", "path": None}

    if not captured["body"]:
        return {"url": link["url"], "name": link["name"], "section": link.get("section"),
                "status": "error", "reason": "file response had empty body", "path": None}

    return save_captured_file(link, downloads_dir, captured)


async def download_documents(page, links, downloads_dir):
    """Download every link with download=True into a section tree (reuses auth)."""
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


# Command-line interface
def parse_args():
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
