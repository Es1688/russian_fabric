from __future__ import annotations

from max_news_bot.config.loader import SourceConfig
from max_news_bot.core.models import NewsItem, Post, PublishedResult
from max_news_bot.publishers.post import PostPublishResult
from max_news_bot.writers.base import WriteResult


class MockFetcher:
    def __init__(
        self,
        items: list[NewsItem] | None = None,
        items_by_source: dict[str, list[NewsItem]] | None = None,
    ) -> None:
        self.items = items or []
        self.items_by_source = items_by_source or {}
        self.fetch_calls: list[tuple] = []

    async def fetch(self, source: SourceConfig, max_news: int, timeout: int) -> list[NewsItem]:
        self.fetch_calls.append((source, max_news, timeout))
        if source.name in self.items_by_source:
            return self.items_by_source[source.name][:max_news]
        return self.items[:max_news]


class MockPublisher:
    def __init__(self, *, success: bool = True, raise_on_publish: Exception | None = None) -> None:
        self.success = success
        self.raise_on_publish = raise_on_publish
        self.publish_calls: list[list[NewsItem]] = []

    async def publish(self, items: list[NewsItem]) -> PublishedResult:
        self.publish_calls.append(list(items))
        if self.raise_on_publish:
            raise self.raise_on_publish
        if not items:
            return PublishedResult(success=True, published=0)
        if self.success:
            return PublishedResult(success=True, published=len(items))
        return PublishedResult(success=False, published=0, failed=len(items))


# Sample post body long enough to pass validate_post_text defaults.
_SAMPLE_POST_TEXT = (
    "Релиз Python 3.13 принесёт экспериментальный JIT-компилятор и новый "
    "режим REPL с многострочным редактированием. Разработчики отмечают "
    "ускорение старта интерпретатора и улучшенные сообщения об ошибках. "
    "Совместимость с популярными фреймворками сохраняется, миграция на "
    "новую версию проходит без существенных изменений в коде проектов."
)


def written_result(item: NewsItem, text: str | None = None) -> WriteResult:
    """Convenience WriteResult: a successfully written draft post."""
    return WriteResult(
        status="written",
        post=Post(
            id=None,
            news_url=item.url,
            source=item.source,
            title=item.title,
            text=_SAMPLE_POST_TEXT if text is None else text,
            status="draft",
        ),
        error_kind=None,
        error=None,
    )


def skipped_result(item: NewsItem) -> WriteResult:
    return WriteResult(
        status="skipped",
        post=Post(
            id=None,
            news_url=item.url,
            source=item.source,
            title=item.title,
            text=None,
            status="skipped",
        ),
        error_kind=None,
        error=None,
    )


def error_result(
    item: NewsItem, error_kind: str = "infra", error: str = "boom"
) -> WriteResult:
    return WriteResult(
        status="error",
        post=None,
        error_kind=error_kind,
        error=error,
    )


class MockWriter:
    """Configurable Writer double.

    ``results`` is either a single fixed WriteResult or a list played back
    in order (the last item repeats once the list is exhausted). Every call
    is recorded in ``calls``.
    """

    def __init__(
        self,
        results: WriteResult | list[WriteResult] | None = None,
    ) -> None:
        if results is None:
            self._results: list[WriteResult] = []
            self._fixed_default = True
        elif isinstance(results, WriteResult):
            self._results = [results]
            self._fixed_default = True
        else:
            self._results = list(results)
            self._fixed_default = False
        self.calls: list[NewsItem] = []

    async def write_post(self, item: NewsItem) -> WriteResult:
        self.calls.append(item)
        if not self._results:
            return written_result(item)
        if self._fixed_default:
            return self._results[0]
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class MockPostPublisher:
    """Configurable PostPublisherProtocol double; records every call."""

    def __init__(
        self,
        *,
        result: PostPublishResult | None = None,
        raise_on_publish: Exception | None = None,
    ) -> None:
        self.result = result or PostPublishResult(
            success=True, message_id="mid.1000", error=None
        )
        self.raise_on_publish = raise_on_publish
        self.calls: list[tuple[Post, bool]] = []

    async def publish_post(
        self, post: Post, *, to_admins: bool = False
    ) -> PostPublishResult:
        self.calls.append((post, to_admins))
        if self.raise_on_publish:
            raise self.raise_on_publish
        return self.result


class MockAlerter:
    """Alerter double recording alerts and recoveries."""

    def __init__(self) -> None:
        self.alerts: list[str] = []
        self.recoveries: list[str] = []

    async def send_alert(self, text: str) -> None:
        self.alerts.append(text)

    async def send_recovery(self, text: str) -> None:
        self.recoveries.append(text)
