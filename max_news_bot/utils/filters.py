from __future__ import annotations

from dataclasses import dataclass

from max_news_bot.core.models import NewsItem


@dataclass(frozen=True)
class KeywordFilter:
    include_keywords: list[str]
    exclude_keywords: list[str]

    def passes(self, item: NewsItem) -> bool:
        title = item.title.lower()
        exclude = [keyword for keyword in self.exclude_keywords if keyword.strip()]
        include = [keyword for keyword in self.include_keywords if keyword.strip()]
        if self._matches_any(title, exclude):
            return False
        if not include:
            return True
        return self._matches_any(title, include)

    @staticmethod
    def _matches_any(text: str, keywords: list[str]) -> bool:
        for keyword in keywords:
            cleaned = keyword.strip().lower()
            if cleaned and cleaned in text:
                return True
        return False
