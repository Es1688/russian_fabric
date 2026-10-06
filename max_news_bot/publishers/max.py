from __future__ import annotations

from max_news_bot.core.models import NewsItem, PublishedResult
from max_news_bot.max_api.client import MaxClient
from max_news_bot.utils.formatter import format_items
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)


class MaxDigestPublisher:
    """Publishes link digests to the MAX channel (Publisher protocol).

    Items are formatted by ``format_items`` (html batches capped at the MAX
    limit) and sent sequentially; a failure on any batch fails the whole
    digest, matching the all-or-nothing semantics of the source bot.
    """

    def __init__(self, client: MaxClient, chat_id: int) -> None:
        self._client = client
        self._chat_id = chat_id

    async def publish(self, items: list[NewsItem]) -> PublishedResult:
        if not items:
            return PublishedResult(success=True, published=0)

        messages = format_items(items)
        try:
            for message in messages:
                await self._client.send_message(
                    self._chat_id, message, fmt="html", disable_link_preview=True
                )
        except Exception as exc:
            logger.error("max_publish_failed error=%s", exc)
            return PublishedResult(success=False, published=0, failed=len(items))

        return PublishedResult(success=True, published=len(items))
