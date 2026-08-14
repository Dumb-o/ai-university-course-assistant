"""Behavioral regression + equivalence tests for mytimes_scraper_v4.py.

No pytest-asyncio required: sync tests drive async scraper code via asyncio.run
(for browser-free paths) or a module-scoped loop (for the real-browser test).

Strategy:
  * EQUIVALENCE: every helper v4 kept from v3 is asserted to produce identical
    output to v3 on the same inputs (proves the refactor changed no behavior).
  * CARD EXTRACTOR: run a REAL headless Chromium against an offline Moodle card
    fixture and assert v4.scrape_links == v3.scrape_links (and the expected shape).
  * DOWNLOAD CORE: drive download_link with browser-free mocks (mock_responses)
    covering force-download, open-in-browser, the .php bug, and HTML-skip, plus
    direct unit tests of the pure save core and extension resolver.
"""

import asyncio
from pathlib import Path

import pytest

import mytimes_scraper_v3 as v3
import mytimes_scraper_v4 as v4
from mock_responses import (
    MOCK_RESPONDER,
    MockPage,
    make_link,
    VIEW_FORCE,
    VIEW_OPEN,
    VIEW_PHP_BUG,
    VIEW_HTML_PAGE,
)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 1. Equivalence of preserved helpers (v4 output must equal v3 output)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    ("Week 9/10", "Week 9-10"),
    ('a:b*c?|<d>', 'a-b-c---d-'),
    ("  .trailing. ", "trailing"),
])
def test_safe_name(value, expected):
    assert v4.safe_name(value) == expected == v3.safe_name(value)


@pytest.mark.parametrize("title,expected", [
    ("Course: Software Testing (202605-Lecture) | myTimes",
     "Software Testing (202605-Lecture)"),
    ("Course: Software Design SEP-2023 (202605-LLecture) | myTimes",
     "Software Design SEP-2023 (202605-LLecture)"),
    ("no course label here", "no course label here"),
])
def test_course_name_from_title(title, expected):
    assert v4.course_name_from_title(title) == expected == v3.course_name_from_title(title)


def test_is_file_response_equivalent_across_versions():
    class R:
        def __init__(self, url, ct, cd=""):
            self.url = url
            self.headers = {"content-type": ct, "content-disposition": cd}

    cases = [
        R("https://x/pluginfile.php/1/mod_resource/content/1/L.pdf", "application/pdf"),
        R("https://x/pluginfile.php/1/theme_boost/logo/logo.png", "image/png"),  # theme chrome
        R("https://x/pluginfile.php/1/mod_resource/content/1/p.png", "image/png"),  # module img
        R("https://x/mod/resource/view.php?id=1", "application/octet-stream"),     # .php-style stream
        R("https://x/Lecture.pptx", "text/html; charset=utf-8"),                   # direct file url
        R("https://x/mod/resource/view.php?id=1", "image/png",
          'attachment; filename="x.png"'),                                         # image attachment
    ]
    for r in cases:
        assert v4.is_file_response(r) == v3.is_file_response(r), r.url


def test_looks_like_login_equivalent():
    for u in ("https://login.microsoftonline.com/x", "https://x/auth/foo",
              "https://x/login/y", "https://mytimes.taylors.edu.my/course/view.php?id=1"):
        assert v4.looks_like_login(u) == v3.looks_like_login(u)


# --------------------------------------------------------------------------- #
# 2. Card extractor equivalence on a REAL browser (offline fixture)
# --------------------------------------------------------------------------- #
def test_scrape_links_identical_to_v3(course_page, loop):
    base = "https://mytimes.taylors.edu.my/course/view.php?id=47163"
    v3_links = loop.run_until_complete(v3.scrape_links(course_page, base))
    v4_links = loop.run_until_complete(v4.scrape_links(course_page, base))
    assert v4_links == v3_links

    assert len(v4_links) == 4
    by_name = {l["name"]: l for l in v4_links}
    assert by_name["Lecture 9 - Use Case Diagram"]["download"] is True
    assert by_name["Lecture 9 - Use Case Diagram"]["kind"] == "resource"
    assert by_name["Lecture 9 - Use Case Diagram"]["section"] == "Week 9"
    assert by_name["Week 9 Discussion"]["download"] is False   # forum
    assert by_name["External Reading"]["download"] is False    # url
    assert by_name["Welcome Note"]["download"] is False         # label


# --------------------------------------------------------------------------- #
# 3. Download core (browser-free) — covers every v3 action
# --------------------------------------------------------------------------- #
def test_force_download_saves_pdf(tmp_path):
    link = make_link("Week 9", "Lecture 9 - Use Case Diagram", VIEW_FORCE)
    res = _run(v4.download_link(MockPage(MOCK_RESPONDER), link, tmp_path))
    assert res["status"] == "downloaded"
    assert Path(res["path"]).name == "Lecture 9 - Use Case Diagram.pdf"
    assert Path(res["path"]).exists()


def test_open_in_browser_capture(tmp_path):
    link = make_link("Week 9", "Lecture 9 Inline", VIEW_OPEN)
    res = _run(v4.download_link(MockPage(MOCK_RESPONDER), link, tmp_path))
    assert res["status"] == "downloaded"
    assert Path(res["path"]).name == "Lecture 9 Inline.pdf"


def test_dedup_is_idempotent(tmp_path):
    link = make_link("Week 9", "Lecture 9 - Use Case Diagram", VIEW_FORCE)
    page = MockPage(MOCK_RESPONDER)
    first = _run(v4.download_link(page, link, tmp_path))
    second = _run(v4.download_link(page, link, tmp_path))
    assert first["status"] == "downloaded"
    assert second["status"] == "skipped"
    assert second["reason"] == "already exists"
    assert len(list((tmp_path / "Week 9").glob("*"))) == 1


def test_extension_hardening_rejects_php(tmp_path):
    """The v3 Week-10 bug: a .php URL serving a real PowerPoint must NOT save as
    .php. With a PowerPoint Content-Type it resolves to .ppt (matches v3)."""
    link = make_link("Week 10", "Lecture 10", VIEW_PHP_BUG)
    res = _run(v4.download_link(MockPage(MOCK_RESPONDER), link, tmp_path))
    assert res["status"] == "downloaded"
    assert res["path"].endswith(".ppt")
    assert not res["path"].endswith(".php")


def test_html_link_wrapper_is_skipped(tmp_path):
    link = make_link("General", "Some Page", VIEW_HTML_PAGE)
    res = _run(v4.download_link(MockPage(MOCK_RESPONDER), link, tmp_path))
    assert res["status"] == "error"
    assert "no file response captured" in res["reason"]


# --------------------------------------------------------------------------- #
# 4. Pure resolver / save-core unit tests
# --------------------------------------------------------------------------- #
def test_resolve_extension_order_and_no_php():
    # 1) Content-Disposition wins
    assert v4.resolve_extension("Lecture.pptx", "x.php",
                                 "application/octet-stream", b"") == ".pptx"
    # 2) URL doc ext accepted (.php rejected)
    assert v4.resolve_extension("", "https://x/Lecture.pdf", "text/html", b"") == ".pdf"
    assert v4.resolve_extension("", "https://x/Lecture.php", "application/octet-stream", b"") == ""
    # 3) Content-Type table (ms-powerpoint -> .ppt, NOT .pptx)
    assert v4.resolve_extension("", "x", "application/vnd.ms-powerpoint", b"") == ".ppt"
    # 4) magic-bytes: HTML body => not a document
    assert v4.resolve_extension("", "x", "text/html", b"<html>doc</html>") == ""
    # 4) magic-bytes: PDF signature
    assert v4.resolve_extension("", "x", "application/octet-stream", b"%PDF-1.4") == ".pdf"
    # 4) magic-bytes: zip + PowerPoint hint => .pptx
    assert v4.resolve_extension("", "x", "application/vnd.openxmlformats-officedocument.presentationml",
                                b"PK\x03\x04payload") == ".pptx"
    # unknown container (zip, no CT hint) -> keep real .zip, not a script ext
    assert v4.resolve_extension("", "x", "application/octet-stream",
                                b"PK\x03\x04payload") == ".zip"


def test_save_captured_file_unit(tmp_path):
    link = make_link("S", "X", "https://x/Lecture.pptx")
    cap = {"url": "https://x/Lecture.pptx",
           "headers": {"content-type": "application/vnd.ms-powerpoint"},
           "body": b"PK\x03\x04"}
    res = v4.save_captured_file(link, tmp_path, cap)
    assert res["status"] == "downloaded"
    assert Path(res["path"]).name == "X.pptx"

    # second save => skipped (dedup)
    again = v4.save_captured_file(link, tmp_path, cap)
    assert again["status"] == "skipped"

    # HTML body => error/skip, not written (neutral content-type + URL so the
    # magic-byte path is the one under test, not the Content-Type/URL table)
    bad = {"url": "https://x/page", "headers": {"content-type": "text/html"},
           "body": b"<html>not a file</html>"}
    bad_link = make_link("S", "Y", "https://x/page")
    err = v4.save_captured_file(bad_link, tmp_path, bad)
    assert err["status"] == "error"
    assert not (tmp_path / "S" / "Y").exists()
