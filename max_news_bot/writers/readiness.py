"""Startup readiness probe of the LLM channels (dual-channel plan §2.1).

A broken channel key must not wait for an outage of the other channel to
be noticed: before the background loops start, every wired LLM channel
gets one minimal completion probe (``max_tokens=1`` plus a few-token
prompt; cost is a fraction of a cent). The probe checks transport and
authorization only — an HTTP 200 with a parseable chat-completion answer
means ready; the content itself is never validated.

Outcomes:
- 200: the channel is ready;
- config-kind status (401/402/403/404): broken key, balance or model is
  a static deploy error — ValueError fails the launch;
- infra-kind (timeout, network, 5xx, malformed body): a temporarily
  unavailable external service must not gate the startup — a WARNING is
  logged and the launch continues.

Images are deliberately not probed at startup: generation is billed per
request, so a config-broken image channel surfaces on the first real
generation via the alert hook of the failover wrappers instead.
"""

from __future__ import annotations

import aiohttp

from max_news_bot.config.loader import FactoryConfig, LlmConfig
from max_news_bot.utils.logging import get_logger
from max_news_bot.writers.completion import LlmCompletionClient, LlmCompletionError


logger = get_logger(__name__)

_PROBE_MAX_TOKENS = 1
_PROBE_SYSTEM = "readiness probe"
_PROBE_USER = "Reply with the single word: ok"


async def probe_llm_channel(
    channel: str,
    llm: LlmConfig,
    session: aiohttp.ClientSession | None,
) -> None:
    """Send one minimal completion to a channel and judge its outcome.

    A config-kind failure raises ValueError (deploy must be fixed before
    restart); an infra-kind failure only logs a WARNING and returns.
    """
    client = LlmCompletionClient(
        base_url=llm.base_url,
        api_key=llm.api_key,
        model=llm.model,
        timeout=llm.timeout,
        max_tokens=_PROBE_MAX_TOKENS,
        temperature=llm.temperature,
        disable_reasoning=llm.disable_reasoning,
        session=session,
    )
    try:
        await client.complete(_PROBE_SYSTEM, _PROBE_USER)
    except LlmCompletionError as exc:
        if exc.kind == "config":
            raise ValueError(
                f"LLM {channel} channel (model {llm.model}) failed the "
                f"startup readiness probe: {exc} - a broken key, balance "
                "or model is a static deploy error; fix the channel "
                "configuration and restart"
            ) from exc
        logger.warning(
            "llm_probe_degraded channel=%s model=%s error=%s: temporary "
            "unavailability does not block the startup",
            channel,
            llm.model,
            exc,
        )
        return
    logger.info("llm_probe_ok channel=%s model=%s", channel, llm.model)


async def probe_llm_channels(
    factory: FactoryConfig,
    session: aiohttp.ClientSession | None,
) -> None:
    """Probe the primary channel and, when configured, the fallback —
    the failover wiring decides which channels exist, this only mirrors
    it (callers probe exactly the channels they have wired)."""
    channels: list[tuple[str, LlmConfig]] = [("primary", factory.llm)]
    if factory.llm_fallback is not None:
        channels.append(("fallback", factory.llm_fallback))
    for channel, llm in channels:
        await probe_llm_channel(channel, llm, session)
