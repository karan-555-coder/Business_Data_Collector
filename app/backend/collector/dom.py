"""Fast HTML tree for extraction.

Pages are parsed with lxml directly - the same libxml2 parser BeautifulSoup's
"lxml" backend drives - without building BeautifulSoup's pure-Python object
tree on top. Parsing + extraction costs several times less CPU, which is what
bounds crawl speed on small CPU shares (Render free = 0.1 CPU).

The helpers mirror the BeautifulSoup calls the extractor used, with the same
results: get_text() skips <script>/<style>/<template> content and comments,
attribute matches are exact, and results come in document order.
"""

from __future__ import annotations

import re

from bs4.dammit import EncodingDetector
from lxml import etree

# BeautifulSoup's get_text() leaves out script/style/template strings (and
# ruby annotations); comments are not text nodes in XPath to begin with.
_SKIP_TAGS = ("script", "style", "template", "rt", "rp")
_SKIP = " or ".join(f"ancestor::{t}" for t in _SKIP_TAGS)
_TEXT = etree.XPath(f".//text()[not({_SKIP})]", smart_strings=False)
# Fast path: most elements have no skipped tag inside or around them, and
# then every text node counts - no per-node ancestor walk needed.
_TEXT_ALL = etree.XPath(".//text()", smart_strings=False)
_TITLE = etree.XPath("(//title)[1]")
_META_PROP = etree.XPath("(//meta[@property=$v])[1]")
_META_NAME = etree.XPath("(//meta[@name=$v])[1]")
_SCRIPTS_TYPED = etree.XPath("//script[@type]")
_ANCHORS = etree.XPath("//a[@href]")
_HREF_PREFIX = etree.XPath("//a[starts-with(@href, $p)]/@href", smart_strings=False)
_ADDRESS = etree.XPath("//address")
_ITEMPROP = etree.XPath("//*[@itemprop = $v]")
_WITH_CLASS = etree.XPath("//*[@class]")
_WITH_ID = etree.XPath("//*[@id]")
_H2_H3 = etree.XPath("//h2 | //h3")
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


def _decode(raw: bytes) -> str:
    """Bytes -> text, choosing the encoding the way BeautifulSoup does: BOM,
    then the page's declared charset, then detection, UTF-8, Windows-1252."""
    for enc in EncodingDetector(raw, is_html=True).encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def parse(raw: bytes):
    """Parse a page. Never raises; an empty or unparseable body gives an
    empty document."""
    text = _decode(raw) if raw else ""
    # Comments stay in the tree: dropping them would merge the text around
    # them into one string, unlike BeautifulSoup (text() skips them anyway).
    parser = etree.HTMLParser(recover=True)
    try:
        root = etree.fromstring(text, parser) if text.strip() else None
    except ValueError:        # an XML encoding declaration inside a str
        root = etree.fromstring(_XML_DECL.sub("", text, 1), parser)
    except etree.LxmlError:
        root = None
    if root is None:
        root = etree.fromstring("<html></html>", parser)
    return root


_ASCII_SPACES = "\x20\x0a\x09\x0c\x0d"


def text(el, sep: str = "") -> str:
    """BeautifulSoup get_text(sep) of an element. Like BeautifulSoup, a
    whitespace-only string collapses to one newline (if it had one) or one
    space (BeautifulSoup keeps them verbatim inside <pre>/<textarea>; that
    difference never changes what is extracted)."""
    skip = (next(el.iter(*_SKIP_TAGS), None) is not None       # self/inside
            or next(el.iterancestors(*_SKIP_TAGS), None) is not None)
    nodes = _TEXT(el) if skip else _TEXT_ALL(el)
    return sep.join(
        s if s.strip(_ASCII_SPACES) else ("\n" if "\n" in s else " ")
        for s in nodes)


def title(doc) -> str | None:
    """Text of the first <title>, or None when the page has none."""
    found = _TITLE(doc)
    return text(found[0]) if found else None


def meta(doc, *, prop: str = "", name: str = ""):
    """First <meta property=...> or <meta name=...> element, or None."""
    found = _META_PROP(doc, v=prop) if prop else _META_NAME(doc, v=name)
    return found[0] if found else None


def scripts_with_type(doc):
    return _SCRIPTS_TYPED(doc)


def anchors(doc):
    """Every <a href=...>, in document order."""
    return _ANCHORS(doc)


def hrefs_starting(doc, prefix: str) -> list[str]:
    return _HREF_PREFIX(doc, p=prefix)


def address_tags(doc):
    return _ADDRESS(doc)


def with_itemprop(doc, value: str):
    return _ITEMPROP(doc, v=value)


def with_class_or_id(doc, rx: re.Pattern, cap_class: int, cap_id: int) -> list:
    """Elements whose class (up to cap_class) then id (up to cap_id) matches
    rx - BeautifulSoup find_all(class_=rx)[:n] + find_all(id=rx)[:m]."""
    out = []
    for el in _WITH_CLASS(doc):
        if len(out) >= cap_class:
            break
        if rx.search(el.get("class") or ""):
            out.append(el)
    n = 0
    for el in _WITH_ID(doc):
        if n >= cap_id:
            break
        if rx.search(el.get("id") or ""):
            out.append(el)
            n += 1
    return out


def h2_h3(doc):
    return _H2_H3(doc)
