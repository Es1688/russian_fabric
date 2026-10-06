from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# query params stripped during URL canonicalization
_TRACKING_PARAMS = {"fbclid"}
_TRACKING_PARAM_PREFIXES = ("utm_",)

# unicode alphanumeric runs (no underscore, no punctuation)
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def canonical_url(url: str) -> str:
    """Canonical form for dedup keys: drop utm_*/fbclid query params and the
    fragment, strip the trailing slash, lowercase scheme and host."""
    url = url.strip()
    if not url:
        return url
    parts = urlsplit(url)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query)
        if not _is_tracking_param(name)
    ]
    path = parts.path.rstrip("/")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(
        _TRACKING_PARAM_PREFIXES
    )


def title_tokens(text: str) -> list[str]:
    """Lowercased punctuation-free tokens longer than 2 characters."""
    return [token for token in _TOKEN_RE.findall(text.lower()) if len(token) > 2]


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets; empty sets are never similar."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if intersection == 0:
        return 0.0
    return intersection / len(a | b)
