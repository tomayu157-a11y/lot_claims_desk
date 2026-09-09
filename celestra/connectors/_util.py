"""Small text helpers shared by the connectors.

Kept private (leading underscore) because it is not part of the connector
contract: every public entry point is still a Connector class in its own
module. The extraction service reads `snippet`/`raw`, so everything here is
about turning source payloads into clean, quotable text.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from selectolax.parser import HTMLParser

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")

# Words that carry no discriminating power when we check whether an open-web
# result actually answers the query.
STOPWORDS = frozenset(
    """a an and are as at be by for from has have how in is it its of on or that
    the to was were what when where which who why with without into over under
    between about across during per than then this these those you your our""".split()
)


def clean(value: Any) -> str:
    """Collapse whitespace, strip markup entities, and coerce to a plain str."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v)
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("&nbsp;", " ").replace(" ", " ")
    return _WS.sub(" ", value).strip()


def clip(text: str, limit: int) -> str:
    """Trim to `limit` characters on a word boundary. Never mid-word: a quote
    cut mid-word is not verbatim-usable downstream."""
    text = clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def strip_tags(html: str) -> str:
    return clean(_TAG.sub(" ", html or ""))


def html_text(html: str, selector: str | None = None, separator: str = " ") -> str:
    """Extract readable text from an HTML document, dropping script/style."""
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
    except Exception:  # selectolax refuses only truly malformed input
        return strip_tags(html)
    tree.strip_tags(["script", "style", "noscript", "svg", "iframe", "form"])
    node = tree.css_first(selector) if selector else (tree.body or tree.root)
    if node is None:
        node = tree.body or tree.root
    if node is None:
        return strip_tags(html)
    return clean(node.text(separator=separator, strip=True))


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def first_str(*values: Any) -> str:
    for v in values:
        s = clean(v)
        if s:
            return s
    return ""


def tokens(text: str) -> set[str]:
    """Content tokens used for cheap relevance checks."""
    return {
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 2 and t not in STOPWORDS
    }


def matches_any(haystack: str, terms: Iterable[str]) -> bool:
    low = (haystack or "").lower()
    return any(t.lower() in low for t in terms if t)


def year_of(value: str) -> str:
    m = re.search(r"(19|20)\d{2}", clean(value))
    return m.group(0) if m else ""


def join_sections(sections: dict[str, str], limit: int = 6000) -> str:
    """Flatten named sections into one quotable block, longest first so the
    most substantive text survives the clip."""
    parts = [f"{k}: {clean(v)}" for k, v in sections.items() if clean(v)]
    parts.sort(key=len, reverse=True)
    out = ""
    for p in parts:
        if len(out) + len(p) > limit:
            break
        out = f"{out}\n\n{p}" if out else p
    return out.strip()
