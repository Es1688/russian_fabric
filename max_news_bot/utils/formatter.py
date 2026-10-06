from __future__ import annotations

from html import escape

from max_news_bot.core.models import NewsItem


MAX_MESSAGE_LEN = 4096


def format_items(items: list[NewsItem]) -> list[str]:
    messages: list[str] = []
    current = ""

    for item in items:
        block = _format_item_block(item)
        if len(block) > MAX_MESSAGE_LEN:
            block = _truncate_block(block, MAX_MESSAGE_LEN)
        if current and len(current) + len(block) + 2 > MAX_MESSAGE_LEN:
            messages.append(current.rstrip())
            current = ""
        current += block + "\n\n"

    if current.strip():
        messages.append(current.rstrip())
    return messages


def _format_item_block(item: NewsItem) -> str:
    title = escape(item.title)
    source = escape(item.source)
    url = escape(item.url)
    category = f" / {escape(item.category)}" if item.category else ""
    return f"• <b>{title}</b>\n{url}\n<i>{source}{category}</i>"


def _truncate_block(block: str, max_len: int) -> str:
    if len(block) <= max_len:
        return block
    return block[: max_len - 3] + "..."
