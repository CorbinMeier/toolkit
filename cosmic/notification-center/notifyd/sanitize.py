"""Body-markup allow-list: freedesktop body markup -> plain-text runs.

Allowed: <b> <i> <u> <a href> (http/https/mailto only). <img> is dropped. Every other tag is
stripped and its text kept (so `<script>x</script>` yields the text "x", which is inert because
the web layer only ever makes text nodes). <br> becomes a newline. Entities are decoded.
Malformed markup never raises and never loses text.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})
_FLAG_TAGS = {"b": "b", "i": "i", "u": "u"}
# Browsers ignore tabs, newlines and other C0 controls inside a URL, so "java\tscript:" is
# a javascript: URL. Strip them before reading the scheme.
_CONTROL = re.compile(r"[\x00-\x20\x7f]+")


def safe_href(href: str | None) -> str | None:
    """The href if its scheme is http/https/mailto, else None."""
    if not href:
        return None
    cleaned = _CONTROL.sub("", href)
    try:
        scheme = urlsplit(cleaned).scheme.lower()
    except ValueError:
        return None
    return cleaned if scheme in ALLOWED_SCHEMES else None


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.runs: list[dict] = []
        self._counts = {"b": 0, "i": 0, "u": 0}
        self._hrefs: list[str | None] = []  # one entry per open <a>; None = disallowed link

    def _emit(self, text: str) -> None:
        if not text:
            return
        run: dict = {"text": text}
        for name, count in self._counts.items():
            if count > 0:
                run[name] = True
        href = self._hrefs[-1] if self._hrefs else None  # innermost link wins
        if href:
            run["href"] = href
        last = self.runs[-1] if self.runs else None
        if last is not None and {k: v for k, v in last.items() if k != "text"} == {
            k: v for k, v in run.items() if k != "text"
        }:
            last["text"] += text
        else:
            self.runs.append(run)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _FLAG_TAGS:
            self._counts[tag] += 1
        elif tag == "a":
            self._hrefs.append(safe_href(dict(attrs).get("href")))
        elif tag == "br":
            self._emit("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._emit("\n")
        # Any other self-closing tag (including <b/>, <a/>, <img/>) opens nothing.

    def handle_endtag(self, tag: str) -> None:
        if tag in _FLAG_TAGS:
            self._counts[tag] = max(0, self._counts[tag] - 1)
        elif tag == "a" and self._hrefs:
            self._hrefs.pop()

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def handle_comment(self, data: str) -> None:
        pass  # comments carry no visible text

    def handle_decl(self, decl: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass

    def unknown_decl(self, data: str) -> None:
        pass


def _close_dangling(body: str) -> str:
    """Escape a final `<` that never closes (`x<y`, `<a href`, `<!-- x`).

    HTMLParser silently discards an unterminated tag or comment at end of input, which would
    lose text; showing it literally is the safe reading.
    """
    end = body.rfind("-->")
    start = body.find("<!--", end + 3 if end != -1 else 0)
    while start != -1:  # a comment that never closes would swallow the rest of the body
        body = body[:start] + "&lt;" + body[start + 1 :]
        start = body.find("<!--", start)
    last = body.rfind("<")
    if last != -1 and body.find(">", last) == -1:
        return body[:last] + "&lt;" + body[last + 1 :]
    return body


def body_runs(body: str) -> list[dict]:
    """Turn body markup into runs `{text, b?, i?, u?, href?}`; flags only when set."""
    if not body:
        return []
    parser = _Collector()
    try:
        parser.feed(_close_dangling(body))
        parser.close()
    except Exception:
        # The parser should not raise, but a notification must never be lost to it.
        return [{"text": body}]
    return parser.runs
