"""YandexART placeholder: reserves the ImageGenerator slot (plan §3.1).

The real client is a future task; this stub is never wired at runtime.
Its docstring pins the contract so the future implementation needs no
protocol or wiring changes (plan §3.1, research 2026-10-09 §2):

- synchronous ``POST {yandex_base_url}/images/generations`` (no queue):
  ``{"model": "art://<folder_id>/aliceai-image-art-3.0", "prompt": ...,
  "size": "1024x1024"}`` -> ``data[0].b64_json``;
- auth: ``Authorization: Bearer <LLM_API_KEY>``; on 401 — repeat the call
  with ``Api-Key: <key>`` and ``OpenAI-Project: <folder_id>`` headers
  (open question of research §2.3);
- ``folder_id`` is parsed from the fallback LLM model of the config
  (``gpt://<folder_id>/yandexgpt-5-lite``);
- prompt is capped at 500 characters; ``b64_json`` is decoded with a
  MAX_IMAGE_BYTES length check (malformed base64 / oversized -> infra).

Until implemented, ``generate`` raises a config-kind error so any
accidental wiring fails loudly instead of silently degrading.
"""

from __future__ import annotations

from max_news_bot.images.base import ImageGenerationError


class YandexArtStub:
    """Same interface as the future YandexART client; never generates."""

    async def generate(self, prompt: str) -> bytes:
        raise ImageGenerationError("config", "yandex_art: not implemented (stub)")
