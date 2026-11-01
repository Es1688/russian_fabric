"""SOCKS egress proxy for foreign API endpoints (ADR-005).

OpenRouter and fal.ai are unstable from RU IPs, so they egress through the
host-local socks5 inbound of the stelthVPN cluster. Its briefing mandates
``socks5h://`` semantics: DNS resolves on the proxy side (``rdns=True``),
so target domains never leak to RU resolvers. Everything else (MAX, the
Yandex fallback channel, RSS, weather) stays direct.

The accepted URL shape is exactly ``socks5h://host:port``: no other schemes
(``socks5://`` means ``rdns=False`` — a silent DNS leak) and no userinfo
(the loopback inbound has no authentication). Any deviation is a static
deploy error: ValueError fails the startup instead of surfacing later as
an obscure per-request failure.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from aiohttp import BaseConnector
from aiohttp_socks import ProxyConnector

from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

EGRESS_PROXY_ENV = "EGRESS_PROXY_URL"
_EXPECTED_URL = "socks5h://host:port"


def egress_connector(url: str) -> BaseConnector:
    """Build the SOCKS5 connector for a ``socks5h://host:port`` proxy URL."""
    parsed = urlsplit(url.strip())
    if parsed.scheme != "socks5h":
        raise ValueError(
            f"{EGRESS_PROXY_ENV} must be a {_EXPECTED_URL} URL, got {url!r}:"
            " socks5:// is rejected on purpose — it resolves DNS locally"
            " and leaks target domains to RU resolvers"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            f"{EGRESS_PROXY_ENV} must not carry credentials, got {url!r}:"
            " the loopback inbound has no authentication"
        )
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not parsed.hostname or port is None:
        raise ValueError(
            f"{EGRESS_PROXY_ENV} must be a {_EXPECTED_URL} URL,"
            f" got {url!r}"
        )
    return ProxyConnector(parsed.hostname, port, rdns=True)


def egress_connector_from_env() -> BaseConnector | None:
    """Read EGRESS_PROXY_URL and build the connector; None keeps direct egress.

    An unset or empty variable preserves the current behavior (everything
    goes direct), so local development does not need a proxy.
    """
    url = os.getenv(EGRESS_PROXY_ENV, "").strip()
    if not url:
        return None
    connector = egress_connector(url)
    logger.info("egress_proxy_enabled url=%s", url)
    return connector
