from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from max_news_bot.core.models import Post


@dataclass(frozen=True)
class PostPublishResult:
    success: bool
    message_id: int | None
    error: str | None
    # "content" (text itself is unusable — burns an attempt) or "infra"
    # (network/flood — retried for free, plan P0.3); None means unknown/infra
    error_kind: str | None = None


class PostPublisherProtocol(Protocol):
    async def publish_post(
        self, post: Post, *, to_admins: bool = False
    ) -> PostPublishResult: ...
