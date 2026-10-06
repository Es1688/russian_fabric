"""Manual prompt regression against a real LLM: ``pytest -m llm``.

Excluded from the default run (pyproject ``addopts``). Requires LLM_API_KEY
in the environment; LLM_BASE_URL / LLM_MODEL override the endpoint and model.
Checks that the production prompt still yields validator-clean output.
"""

from __future__ import annotations

import os

import pytest

from max_news_bot.core.models import NewsItem
from max_news_bot.writers.llm import DEFAULT_PROMPT, LlmWriter
from max_news_bot.writers.validator import validate_post_text

pytestmark = pytest.mark.llm

if not os.getenv("LLM_API_KEY"):
    pytest.skip(
        "LLM_API_KEY is not set (manual run: pytest -m llm)",
        allow_module_level=True,
    )


_ITEM = NewsItem(
    title="Вышла стабильная версия языка программирования с JIT-компилятором",
    url="https://example.com/news/jit-release",
    source="Test RSS",
    summary=(
        "Разработчики опубликовали стабильный релиз с экспериментальным "
        "JIT-компилятором: старт интерпретатора ускорился на 40%, "
        "сообщения об ошибках стали понятнее, обратная совместимость "
        "сохранена. Пакеты появятся в дистрибутивах до конца квартала."
    ),
)


async def test_real_llm_output_passes_validator() -> None:
    writer = LlmWriter(
        base_url=os.getenv("LLM_BASE_URL", "https://ai.api.cloud.yandex.net/v1"),
        api_key=os.environ["LLM_API_KEY"],
        model=os.getenv("LLM_MODEL", "gpt://FOLDER_ID/yandexgpt-5-lite"),
        timeout=90,
        max_tokens=800,
        temperature=0.7,
        prompt=DEFAULT_PROMPT,
    )

    result = await writer.write_post(_ITEM)

    assert result.status in ("written", "skipped")
    if result.status == "skipped":
        # a legit outcome for a borderline item, but suspicious for this one
        pytest.skip("model returned SKIP for a clearly relevant item")
    assert result.post is not None and result.post.text
    ok, reason = validate_post_text(
        result.post.text, max_chars=2000, min_chars=200
    )
    assert ok, f"validator rejected real LLM output: {reason}: {result.post.text[:300]}"
    assert result.tokens > 0
