"""YandexART image client: one synchronous OpenAI-compatible call (plan §3.1).

Implementation of the contract the former stub pinned (research
2026-10-09 §2): ``POST {base_url}/images/generations`` on the same Yandex
AI Studio gateway the fallback LLM channel uses; the image comes back as
base64 in ``data[0].b64_json``.

Requests are billed per call and are not idempotent (research §2.4), so
the only repeat is exactly one retry on HTTP 401 with the alternate auth
scheme — a rejected auth never reaches generation and is not billed:
``Authorization: Bearer <key>`` first, then ``Api-Key: <key>`` plus
``OpenAI-Project: <folder_id>``. Bearer works for chat completions; its
acceptance by the Images API was the open question of research §2.3 and
is resolved by that retry on the first live call.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from typing import Any

import aiohttp

from max_news_bot.images.base import MAX_IMAGE_BYTES, ImageGenerationError


DEFAULT_BASE_URL = "https://ai.api.cloud.yandex.net/v1"
DEFAULT_MODEL = "aliceai-image-art-3.0"
DEFAULT_SIZE = "1024x1024"

# HTTP statuses meaning the key/balance is wrong: no retry, immediate alert.
_CONFIG_STATUSES = frozenset({401, 402, 403})

_MAX_ERROR_LEN = 300
# The API rejects longer prompts (research §2.4).
_MAX_PROMPT_CHARS = 500

_FALLBACK_MODEL_RE = re.compile(r"^gpt://([^/]+)/")
# The yaml default ships this placeholder instead of a real folder id.
_PLACEHOLDER_FOLDER = "FOLDER_ID"


def _truncate(text: str) -> str:
    return text[:_MAX_ERROR_LEN]


def _http_error(status: int) -> ImageGenerationError:
    kind = "config" if status in _CONFIG_STATUSES else "infra"
    return ImageGenerationError(kind, f"yandex_art http {status}")


def folder_id_from_fallback_model(model: str) -> str | None:
    """Extract the folder id from a ``gpt://<folder_id>/<model>`` LLM URI.

    The yaml default carries the ``FOLDER_ID`` placeholder: it parses but
    is not a real folder, so it is treated as missing (the wiring then
    leaves YandexART inactive with a startup warning instead of calling
    a bogus folder).
    """
    match = _FALLBACK_MODEL_RE.match(model.strip())
    folder = match.group(1) if match else None
    if not folder or folder == _PLACEHOLDER_FOLDER:
        return None
    return folder


class YandexArtGenerator:
    """Generates one image via the Yandex AI Studio Images API; the bytes
    are the decoded ``data[0].b64_json`` PNG."""

    def __init__(
        self,
        api_key: str,
        folder_id: str,
        model: str = DEFAULT_MODEL,
        size: str = DEFAULT_SIZE,
        timeout: float = 120.0,
        base_url: str = DEFAULT_BASE_URL,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._api_key = api_key
        self._folder_id = folder_id
        self._model_uri = f"art://{folder_id}/{model.strip('/')}"
        self._size = size
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        # Shared process-wide session wins; otherwise one per call.
        self._session = session

    async def generate(self, prompt: str) -> bytes:
        """One synchronous generation; raises ImageGenerationError, never
        other errors."""
        payload = {
            "model": self._model_uri,
            "prompt": prompt[:_MAX_PROMPT_CHARS],
            "size": self._size,
        }
        try:
            if self._session is not None:
                data = await self._generate(self._session, payload)
            else:
                async with aiohttp.ClientSession() as session:
                    data = await self._generate(session, payload)
        except ImageGenerationError:
            raise
        except Exception as exc:  # unexpected: keep the error contract
            raise ImageGenerationError(
                "infra",
                _truncate(f"yandex_art request failed: {type(exc).__name__} {exc}"),
            ) from exc
        return self._decode(data)

    async def _generate(
        self, session: aiohttp.ClientSession, payload: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{self._base_url}/images/generations"
        status, body = await self._post(
            session, url, payload, {"Authorization": f"Bearer {self._api_key}"}
        )
        if status == 401:
            # the alternate scheme of the official examples; free to try -
            # the rejected Bearer request never reached generation
            status, body = await self._post(
                session,
                url,
                payload,
                {"Api-Key": self._api_key, "OpenAI-Project": self._folder_id},
            )
            if status == 401:
                raise ImageGenerationError(
                    "config",
                    "yandex_art http 401 under both the Bearer and Api-Key "
                    "schemes: check the LLM_API_KEY and its billing",
                )
        if status != 200:
            raise _http_error(status)
        return self._parse(body)

    async def _post(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[int, str]:
        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as response:
                return response.status, await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ImageGenerationError(
                "infra",
                _truncate(f"yandex_art request failed: {type(exc).__name__} {exc}"),
            ) from exc

    def _parse(self, body: str) -> dict[str, Any]:
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ImageGenerationError(
                "infra", _truncate(f"yandex_art invalid response: {exc}")
            ) from exc
        if not isinstance(data, dict):
            raise ImageGenerationError(
                "infra", "yandex_art invalid response: not a JSON object"
            )
        return data

    def _decode(self, data: dict[str, Any]) -> bytes:
        items = data.get("data")
        b64 = items[0].get("b64_json") if isinstance(items, list) and items else None
        if not isinstance(b64, str) or not b64:
            raise ImageGenerationError(
                "infra", "yandex_art response has no data[0].b64_json"
            )
        # the length is checked before decoding so a huge payload cannot
        # allocate memory first (4 base64 chars encode 3 bytes)
        if len(b64) * 3 // 4 > MAX_IMAGE_BYTES:
            raise ImageGenerationError(
                "infra", f"yandex_art image exceeds the {MAX_IMAGE_BYTES}-byte limit"
            )
        try:
            return base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageGenerationError(
                "infra", _truncate(f"yandex_art invalid base64: {exc}")
            ) from exc
