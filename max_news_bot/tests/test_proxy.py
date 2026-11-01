"""Tests of the socks5h egress connector (ADR-005).

The connector is the contour's single proxy seam: strictly
``socks5h://host:port`` (DNS resolves on the foreign side, ``rdns=True``),
fail-fast on anything else — ``socks5://`` is a silent DNS leak to RU
resolvers and credentials mean a misconfigured URL (the loopback inbound
has no authentication).
"""

from __future__ import annotations

import pytest

from max_news_bot.utils.proxy import egress_connector, egress_connector_from_env


async def test_socks5h_url_builds_rdns_connector() -> None:
    connector = egress_connector("socks5h://127.0.0.1:1080")
    try:
        assert connector._proxy_host == "127.0.0.1"
        assert connector._proxy_port == 1080
        # socks5h semantics: the proxy side resolves target domains
        assert connector._rdns is True
    finally:
        await connector.close()


@pytest.mark.parametrize(
    "url",
    [
        # rdns=False — resolves locally, leaking domains to RU resolvers
        "socks5://127.0.0.1:1080",
        "socks4://127.0.0.1:1080",
        "http://127.0.0.1:1080",
        "https://proxy.example.com:8080",
        # the loopback inbound has no authentication
        "socks5h://user:pass@127.0.0.1:1080",
        "socks5h://127.0.0.1",
        "socks5h://127.0.0.1:notaport",
        "socks5h://:1080",
        "not a url at all",
    ],
)
def test_anything_else_fails_fast(url: str) -> None:
    # ValueError at startup beats an obscure per-request failure later
    with pytest.raises(ValueError, match="EGRESS_PROXY_URL"):
        egress_connector(url)


async def test_env_toggles_the_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    # unset/blank keeps the direct egress (local development unchanged)
    monkeypatch.delenv("EGRESS_PROXY_URL", raising=False)
    assert egress_connector_from_env() is None
    monkeypatch.setenv("EGRESS_PROXY_URL", "   ")
    assert egress_connector_from_env() is None

    monkeypatch.setenv("EGRESS_PROXY_URL", "socks5h://127.0.0.1:1080")
    connector = egress_connector_from_env()
    try:
        assert connector is not None
        assert connector._rdns is True
    finally:
        await connector.close()
