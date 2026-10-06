from __future__ import annotations

from typing import Protocol

from max_news_bot.max_api.client import MaxClient
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)


class Alerter(Protocol):
    async def send_alert(self, text: str) -> None: ...

    async def send_recovery(self, text: str) -> None: ...


class MaxAlerter:
    """Delivers incident alerts to the admins' DMs.

    MAX constraint: a bot can DM a user only after that user has started
    it, so every admin must /start the bot once — otherwise the send just
    fails. Alerting must never break the factory loop: every failure is
    logged and swallowed.
    """

    def __init__(self, client: MaxClient, admin_ids: list[int]) -> None:
        self._client = client
        self._admin_ids = list(admin_ids)

    async def send_alert(self, text: str) -> None:
        await self._broadcast(text)

    async def send_recovery(self, text: str) -> None:
        await self._broadcast(text)

    async def _broadcast(self, text: str) -> None:
        for admin_id in self._admin_ids:
            try:
                await self._client.send_message(admin_id, text, fmt="html")
            except Exception as exc:
                logger.error("alert_delivery_failed admin_id=%s error=%s", admin_id, exc)
