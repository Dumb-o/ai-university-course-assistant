#!/usr/bin/env python3
"""Log into Taylor's MyTIMES Moodle with Playwright and dump all links to JSON.

MyTIMES logs in through your Taylor's Microsoft account (Microsoft Entra ID,
MFA required). Because MFA cannot be automated, the default mode is MANUAL
login: you complete email + password + MFA in the browser window once, and the
authenticated session is saved for future runs.

Setup:
    pip install playwright
    playwright install chromium

Usage (recommended - manual login, works with MFA):
    python mytimes_scraper.py "https://mytimes.taylors.edu.my/course/view.php?id=47163"
    -> complete email + password + MFA manually in the browser window (once)

Usage (optional auto-fill; MFA still manual):
    export TAYLORS_EMAIL="name@taylors.edu.my"
    export TAYLORS_PASSWORD="your_password"
    python mytimes_scraper.py --auto-fill "https://mytimes.taylors.edu.my/course/view.php?id=47163"

The persistent browser profile is kept in ./mytimes_state/, so after the first
successful login you can re-run the script without logging in again (until the
MyTIMES session expires). If an account picker appears, choose your Taylor's
Microsoft account.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

USERNAME_SELECTORS = [
    'input[name="username"]',        # plain Moodle / generic forms
    'input[name="loginfmt"]',        # Microsoft identity platform
    '#i0116',                        # Microsoft identity platform
    'input[type="email"]',
]

PASSWORD_SELECTORS = [
    'input[type="password"]',
    '#i0118',                        # Microsoft identity platform
]

SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    '#idSIButton9',                  # Microsoft "Next" / "Sign in" / "Yes"
    'button:has-text("Next")',
    'button:has-text("Sign in")',
]

FILE_EXT = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|csv|png|jpe?g|gif|zip|txt|html?)$", re.I)


def parse_args():
    parser = argparse.ArgumentParser(description="MyTIMES Moodle link scraper")
    parser.add_argument("course_url",
                        help="Moodle course URL, e.g. .../course/view.php?id=47163")
    parser.add_argument("-o", "--output", default="mytimes_links.json",
                        help="output JSON file (default: mytimes_links.json)")
    parser.add_argument("--auto-fill", action="store_true",
                        help="auto-fill email/password from TAYLORS_EMAIL and TAYLORS_PASSWORD env vars (MFA still manual)")
    parser.add_argument("--headless", action="store_true",
                        help="run without a visible browser window")
    parser.add_argument("--state-dir", default="mytimes_state",
                        help="persistent browser profile directory (default: mytimes_state)")
    return parser.parse_args()


def looks_like_login(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    return (
        "login.microsoftonline.com" in parsed.netloc.lower()
        or "/auth/" in path
        or "/login/" in path
    )


def classify(href):
    match = re.search(r"/mod/([a-z_]+)/", urlparse(href).path.lower())
    if match:
        return f"mod:{match.group(1)}"
    if FILE_EXT.search(urlparse(href).path):
        return "file"
    if "/course/" in urlparse(href).path:
        return "course"
    return "other"


async def fill_first(page, selectors, value):
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count():
            await locator.first.fill(value)
            return True
    return False


async def click_first(page, selectors):
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count():
            await locator.first.click()
            return True
    return False


async def wait_for_authenticated(page, timeout):
    """Wait until the browser leaves the login flow (authentication done)."""
    print(f"[login] waiting up to {timeout // 60} minutes for authentication to complete...")
    try:
        await page.wait_for_url(lambda url: not looks_like_login(url), timeout=timeout * 1000)
        print(f"[login] authenticated -> {page.url}")
    except Exception:
        print(f"[login] not authenticated after {timeout // 60} minutes; continuing anyway")


async def login_manual(page, timeout=300):
    """Manual login: the user completes email + password + MFA in the browser."""
    print("[login] Please log in manually in the browser window:")
    print("[login]   1. enter your Taylor's Microsoft account email + password")
    print("[login]   2. approve the MFA prompt (Microsoft Authenticator / number match)")
    print("[login]   (if an account picker appears, choose your Taylor's account)")
    await wait_for_authenticated(page, timeout)


async def login_auto(page, username, password):
    """Auto-fill Microsoft form fields; MFA is still completed manually."""
    print(f"[login] on auth page: {page.url}")
    await page.wait_for_timeout(2000)

    if not await fill_first(page, USERNAME_SELECTORS, username):
        print("[login] could not find the email field; finish login manually in the browser window")
    else:
        await click_first(page, SUBMIT_SELECTORS)
        try:
            await page.wait_for_selector('input[type="password"]', timeout=30000)
        except Exception:
            print("[login] no password field appeared after the email step")

    if await fill_first(page, PASSWORD_SELECTORS, password):
        await click_first(page, SUBMIT_SELECTORS)
        try:  # "Stay signed in?" -> click Yes
            await page.wait_for_selector("#idSIButton9", timeout=10000)
            await page.click("#idSIButton9")
        except Exception:
            pass
    else:
        print("[login] could not find the password field; finish login manually in the browser window")

    print("[login] approve the MFA prompt (Microsoft Authenticator / number match) in the browser...")
    await wait_for_authenticated(page, timeout=180)


async def scrape_links(page, course_url):
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
        full = urljoin(course_url, href)
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


async def run(args):
    username = os.environ.get("TAYLORS_EMAIL")
    password = os.environ.get("TAYLORS_PASSWORD")
    if args.auto_fill and (not username or not password):
        sys.exit("--auto-fill requires TAYLORS_EMAIL and TAYLORS_PASSWORD environment variables.")

    Path(args.state_dir).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=args.state_dir,
            headless=args.headless,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            print(f"[open] {args.course_url}")
            await page.goto(args.course_url, wait_until="domcontentloaded", timeout=60000)

            if looks_like_login(page.url):
                if args.auto_fill:
                    await login_auto(page, username, password)
                else:
                    await login_manual(page)

            await page.goto(args.course_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            print(f"[page] {await page.title()} ({page.url})")
            links = await scrape_links(page, args.course_url)

            output = {
                "course_url": args.course_url,
                "final_url": page.url,
                "page_title": await page.title(),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "total": len(links),
                "links": links,
            }
            Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")

            print(f"[done] found {len(links)} links -> {args.output}")
            for link in links[:20]:
                print(f"  [{link['kind']}] {link['url']}  ::  {link['text'][:60]}")
            if len(links) > 20:
                print(f"  ... and {len(links) - 20} more")
        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
