"""Browser-free doubles for the v4 download path.

Lets the pytest harness exercise download_link() (and its save core) without a
live MyTIMES login by faking just the Playwright surface v4 actually touches:
    - page.route / page.unroute / page.goto
    - route.fetch()  -> returns a FakeResponse built from a URL->response map
    - route.fulfill(response) / route.continue_()

MockPage also simulates the real browser behaviour of firing a SECOND request
for an embedded file after the HTML wrapper loads (via FakeResponse.subresources),
which is exactly the open-in-browser case v3/v4 must handle.
"""

from types import SimpleNamespace


class FakeResponse:
    """Minimal stand-in for a Playwright APIResponse."""

    def __init__(self, url, headers, body, subresources=None):
        self.url = url
        self.headers = headers  # plain dict (v4 reads .get("content-type") etc.)
        self._body = body
        self.subresources = subresources or []

    async def body(self):
        return self._body


class MockRoute:
    def __init__(self, url, responder):
        self.request = SimpleNamespace(url=url)
        self._responder = responder
        self.fulfilled = None
        self.continued = False

    async def fetch(self):
        return self._responder(self.request.url)

    async def fulfill(self, response):
        self.fulfilled = response

    async def continue_(self):
        self.continued = True


class MockPage:
    def __init__(self, responder):
        self._responder = responder
        self._handler = None
        self.visited = []

    async def route(self, pattern, handler):
        self._handler = handler

    async def unroute(self, pattern, handler):
        if self._handler is handler:
            self._handler = None

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        await self._dispatch(url)

    async def _dispatch(self, url):
        if self._handler is None:
            return
        route = MockRoute(url, self._responder)
        await self._handler(route)
        # After the handler runs, if this response is an HTML wrapper that
        # embeds a real file, fire the follow-up request (like a real browser).
        resp = self._responder(url)
        if resp is not None:
            for sub in resp.subresources:
                await self._dispatch(sub)


# --- Synthetic course files (no network, no live Moodle) -------------------- #
_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n"
_ZIP = b"PK\x03\x04docx-payload"          # zip container (docx/pptx/xlsx)
_OLE = b"\xd0\xcf\x11\xe0ole2-payload"    # legacy OLE2 (.doc/.xls/.ppt)
_HTML = b"<html><body>just a page</body></html>"

VIEW_FORCE = "https://mytimes.taylors.edu.my/mod/resource/view.php?id=201"
VIEW_OPEN = "https://mytimes.taylors.edu.my/mod/resource/view.php?id=301"
PLUGIN_INLINE = "https://mytimes.taylors.edu.my/pluginfile.php/7/mod_resource/content/1/Lecture9.pdf"
VIEW_PHP_BUG = "https://mytimes.taylors.edu.my/pluginfile.php/7/mod_resource/content/1/Lecture.pptx.php"
VIEW_HTML_PAGE = "https://mytimes.taylors.edu.my/mod/page/view.php?id=999"

MOCK_FILES = {
    # Force-download: the navigation response IS the file (attachment).
    VIEW_FORCE: FakeResponse(
        VIEW_FORCE,
        {"content-type": "application/pdf",
         "content-disposition": 'attachment; filename="Lecture 9.pdf"'},
        _PDF,
    ),
    # Open-in-browser: navigation returns an HTML wrapper that embeds the file.
    VIEW_OPEN: FakeResponse(
        VIEW_OPEN,
        {"content-type": "text/html; charset=utf-8"},
        _HTML,
        subresources=[PLUGIN_INLINE],
    ),
    PLUGIN_INLINE: FakeResponse(
        PLUGIN_INLINE,
        {"content-type": "application/pdf",
         "content-disposition": 'inline; filename="Lecture9.pdf"'},
        _PDF,
    ),
    # The v3 ".php bug": URL ends in .php but the body is a real PPTX (zip) and
    # the Content-Type names PowerPoint -> must resolve to .pptx, never .php.
    VIEW_PHP_BUG: FakeResponse(
        VIEW_PHP_BUG,
        {"content-type": "application/vnd.ms-powerpoint"},
        _ZIP,
    ),
    # A Page/link module whose response is plain HTML -> must be SKIPPED.
    VIEW_HTML_PAGE: FakeResponse(
        VIEW_HTML_PAGE,
        {"content-type": "text/html; charset=utf-8"},
        _HTML,
    ),
}


def MOCK_RESPONDER(url):
    return MOCK_FILES.get(url)


def make_link(section, name, url, download=True):
    return {"section": section, "name": name, "url": url, "download": download}
