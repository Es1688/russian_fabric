from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

# Telegram HTML whitelist: <b>, <i>, <a href="http(s)://...">.
# Tag-like sequences only: "< 3" in plain text is not a tag.
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

_B_OPEN_RE = re.compile(r"^<b\s*>$", re.IGNORECASE)
_B_CLOSE_RE = re.compile(r"^</b\s*>$", re.IGNORECASE)
_I_OPEN_RE = re.compile(r"^<i\s*>$", re.IGNORECASE)
_I_CLOSE_RE = re.compile(r"^</i\s*>$", re.IGNORECASE)
_A_OPEN_RE = re.compile(
    r"""^<a\s+href\s*=\s*(?:"([^"]*)"|'([^']*)')\s*>$""", re.IGNORECASE
)
_A_CLOSE_RE = re.compile(r"^</a\s*>$", re.IGNORECASE)

_ALLOWED_SCHEMES = ("http", "https")


def sanitize_html(text: str) -> str:
    """Whitelist-sanitize a post for Telegram's HTML parse mode.

    - Only <b>, <i> and <a href="http(s)://..."> survive as markup;
    - text outside tags is html-escaped;
    - non-whitelisted tags are dropped (their content stays as text);
    - an <a> with a non-http(s) href is dropped as a pair, content stays;
    - unbalanced or misnested whitelisted tags (and nested <a>) trigger a
      full fallback: all tags stripped, everything escaped.
    """
    parts: list[str] = []
    # open pairs: (tag name, emitted opening markup); "" markup = a
    # suppressed <a> pair whose closing tag must also emit nothing.
    stack: list[tuple[str, str]] = []

    pos = 0
    for match in _TAG_RE.finditer(text):
        parts.append(_escape_text(text[pos : match.start()]))
        tag = match.group(0)
        kind, name, emit = _parse_tag(tag)
        if kind == "open":
            if name == "a" and any(entry[0] == "a" for entry in stack):
                return _fallback(text)  # nested anchors are broken markup
            stack.append((name, emit))
            parts.append(emit)
        elif kind == "close":
            if not stack or stack[-1][0] != name:
                return _fallback(text)
            _, open_emit = stack.pop()
            parts.append("" if not open_emit else f"</{name}>")
        # kind == "drop": tag removed, no stack effect
        pos = match.end()
    parts.append(_escape_text(text[pos:]))

    if stack:  # unclosed whitelisted tags
        return _fallback(text)
    return "".join(parts)


def _parse_tag(tag: str) -> tuple[str, str | None, str]:
    """Classify one tag -> (kind, name, markup to emit).

    kind: "open" | "close" | "drop". For opens with a bad/missing href the
    markup is "" (pair suppressed); for drops both name and markup are unused.
    """
    if _B_OPEN_RE.match(tag):
        return "open", "b", "<b>"
    if _I_OPEN_RE.match(tag):
        return "open", "i", "<i>"
    if _A_OPEN_RE.match(tag):
        href = _safe_href(_anchor_href(tag))
        if href is None:
            return "open", "a", ""  # pair suppressed, content stays as text
        escaped = html.escape(html.unescape(href), quote=True)
        return "open", "a", f'<a href="{escaped}">'
    if _B_CLOSE_RE.match(tag):
        return "close", "b", ""
    if _I_CLOSE_RE.match(tag):
        return "close", "i", ""
    if _A_CLOSE_RE.match(tag):
        return "close", "a", ""
    return "drop", None, ""


def _anchor_href(tag: str) -> str:
    match = _A_OPEN_RE.match(tag)
    assert match is not None, "caller checked the pattern"
    return match.group(1) if match.group(1) is not None else match.group(2)


def _safe_href(url: str) -> str | None:
    url = url.strip()
    if not url or any(ord(ch) < 0x20 for ch in url):
        return None
    parts = urlsplit(url)
    if parts.scheme.lower() in _ALLOWED_SCHEMES and parts.netloc:
        return url
    return None


def _escape_text(fragment: str) -> str:
    # unescape-then-escape keeps already-escaped entities single-escaped
    return html.escape(html.unescape(fragment), quote=False)


def _fallback(text: str) -> str:
    return _escape_text(_TAG_RE.sub("", text))
