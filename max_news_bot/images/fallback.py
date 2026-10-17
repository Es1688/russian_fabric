"""Per-request failover between image providers (plan §3.1).

No sticky state: every call starts with the primary; an error of any kind
switches to the fallback when one is wired. Today the fallback is None
(YandexART is a stub); when a real client lands, the wiring stays the
same — just a second generator passed in.
"""

from __future__ import annotations

from max_news_bot.images.base import ImageGenerationError, ImageGenerator
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)


class FallbackImageGenerator:
    """Tries the primary generator, falls back on any of its errors."""

    def __init__(
        self,
        primary: ImageGenerator,
        fallback: ImageGenerator | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def generate(self, prompt: str) -> bytes:
        try:
            return await self._primary.generate(prompt)
        except ImageGenerationError as exc:
            logger.warning("image_fallback_used kind=%s error=%s", exc.kind, exc)
            if self._fallback is None:
                raise
            # a failing fallback propagates its own error as is
            return await self._fallback.generate(prompt)
