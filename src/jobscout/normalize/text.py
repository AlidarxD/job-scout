from __future__ import annotations

import html
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_BLOCK_TAGS = ("p", "li", "br", "div", "h1", "h2", "h3", "h4", "tr", "ul", "ol")
_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(value: str | None) -> str:
    """Convert (possibly entity-escaped) HTML to readable plain text, keeping line breaks."""
    if not value:
        return ""
    # Greenhouse returns entity-escaped HTML ("&lt;p&gt;"), so unescape first.
    if "&lt;" in value:
        value = html.unescape(value)
    if "<" not in value:
        return _clean(html.unescape(value))
    tree = HTMLParser(value)
    for tag in ("script", "style"):
        for node in tree.css(tag):
            node.decompose()
    for tag in _BLOCK_TAGS:
        for node in tree.css(tag):
            node.insert_before("\n")
    body = tree.body or tree.root
    text = body.text(separator="") if body is not None else ""
    return _clean(text)


def _clean(text: str) -> str:
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return _NL.sub("\n\n", "\n".join(lines)).strip()


def slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()
