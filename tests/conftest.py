"""Pytest configuration + shared fixtures for the v4 harness.

Deliberately AVOIDS pytest-asyncio: fixtures and tests are synchronous and drive
Playwright on a single module-scoped event loop. This keeps the harness runnable
with only `pytest` installed (no extra plugins).

Adds the repo roots to sys.path so the scraper modules and the mock doubles are
importable, and provides a real headless Chromium page for the browser-backed
(card-extractor) equivalence test, plus the offline fixture path.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRAPE = ROOT / "scrape_script"
TESTS = ROOT / "tests"

for p in (SCRAPE, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import mytimes_scraper_v3 as v3  # noqa: E402  (v3 = ground truth for equivalence)
import mytimes_scraper_v4 as v4  # noqa: E402

FIXTURE_HTML = TESTS / "fixtures" / "Block_example.html"


@pytest.fixture(scope="module")
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def browser(loop):
    from playwright.async_api import async_playwright
    pw = loop.run_until_complete(async_playwright().start())
    b = loop.run_until_complete(pw.chromium.launch(headless=True))
    yield b
    loop.run_until_complete(b.close())
    loop.run_until_complete(pw.stop())


@pytest.fixture
def course_page(browser, loop):
    """A real Chromium page loaded with the offline Moodle card fixture."""
    page = loop.run_until_complete(browser.new_page())
    loop.run_until_complete(page.set_content(FIXTURE_HTML.read_text(encoding="utf-8")))
    yield page
    loop.run_until_complete(page.close())
