"""fal.ai queue client: submit -> poll -> download (plan §3.1).

Port of the visual-studio ``FalClient`` (scripts/fal_adapter.py) onto
aiohttp. Flow: ``POST {base_url}/{model}`` with the prompt -> queued job
carrying ``status_url`` / ``response_url``; poll ``status_url`` until
COMPLETED (FAILED -> error); ``GET response_url`` -> ``images[0].url``;
the image is then streamed down with the MAX_IMAGE_BYTES cap. Retries
cover transient failures only (network errors, 429, 5xx) with
exponential backoff; everything else fails immediately and is classified
into the shared config/infra taxonomy.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp

from max_news_bot.images.base import MAX_IMAGE_BYTES, ImageGenerationError


DEFAULT_QUEUE_URL = "https://queue.fal.run"
DEFAULT_POLL_INTERVAL = 1.5

# HTTP statuses meaning the key/balance is wrong: no retry, immediate alert.
_CONFIG_STATUSES = frozenset({401, 402, 403})

# Terminal fal queue statuses besides COMPLETED.
_FAILURE_STATUSES = frozenset({"FAILED", "ERROR"})

_MAX_ERROR_LEN = 300
_DOWNLOAD_CHUNK = 64 * 1024


def _truncate(text: str) -> str:
    return text[:_MAX_ERROR_LEN]


def _http_error(url: str, status: int) -> ImageGenerationError:
    kind = "config" if status in _CONFIG_STATUSES else "infra"
    return ImageGenerationError(kind, f"fal http {status}: {url}")


class FalImageGenerator:
    """Generates one image via the fal.ai queue API and returns its bytes."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        width: int = 1024,
        height: int = 1024,
        base_url: str = DEFAULT_QUEUE_URL,
        max_retries: int = 2,
        backoff: float = 0.5,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model.lstrip("/")
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._width = width
        self._height = height
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff = backoff
        # Shared process-wide session wins; otherwise one per call.
        self._session = session

    async def generate(self, prompt: str) -> bytes:
        """One full queue flow; raises ImageGenerationError, never others."""
        deadline = time.monotonic() + self._timeout
        try:
            if self._session is not None:
                return await self._generate(self._session, prompt, deadline)
            async with aiohttp.ClientSession() as session:
                return await self._generate(session, prompt, deadline)
        except ImageGenerationError:
            raise
        except Exception as exc:  # unexpected: keep the error contract
            raise ImageGenerationError(
                "infra", _truncate(f"fal request failed: {type(exc).__name__} {exc}")
            ) from exc

    async def _generate(
        self, session: aiohttp.ClientSession, prompt: str, deadline: float
    ) -> bytes:
        submitted = await self._submit(session, prompt)
        await self._poll(session, submitted["status_url"], deadline)
        result = await self._result(session, submitted["response_url"])
        images = result.get("images")
        url = images[0].get("url") if isinstance(images, list) and images else None
        if not url or not isinstance(url, str):
            raise ImageGenerationError("infra", "fal response has no images[0].url")
        return await self._download(session, url)

    async def _submit(
        self, session: aiohttp.ClientSession, prompt: str
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{self._model}"
        payload = {
            "prompt": prompt,
            "image_size": {"width": self._width, "height": self._height},
        }
        async with await self._request(session, "POST", url, json=payload) as response:
            if response.status != 200:
                raise _http_error(url, response.status)
            data = await self._json(response, url)
        if not data.get("status_url") or not data.get("response_url"):
            raise ImageGenerationError(
                "infra", "fal submit response has no status_url/response_url"
            )
        return data

    async def _poll(
        self, session: aiohttp.ClientSession, status_url: str, deadline: float
    ) -> None:
        while True:
            async with await self._request(session, "GET", status_url) as response:
                if response.status != 200:
                    raise _http_error(status_url, response.status)
                data = await self._json(response, status_url)
            status = str(data.get("status", "")).upper()
            if status == "COMPLETED":
                return
            if status in _FAILURE_STATUSES:
                raise ImageGenerationError(
                    "infra", f"fal request failed: {_truncate(repr(data))}"
                )
            if time.monotonic() >= deadline:
                raise ImageGenerationError(
                    "infra", f"fal poll timeout after {self._timeout:.0f}s"
                )
            await asyncio.sleep(self._poll_interval)

    async def _result(
        self, session: aiohttp.ClientSession, response_url: str
    ) -> dict[str, Any]:
        async with await self._request(session, "GET", response_url) as response:
            if response.status != 200:
                raise _http_error(response_url, response.status)
            return await self._json(response, response_url)

    async def _download(
        self, session: aiohttp.ClientSession, url: str
    ) -> bytes:
        response = await self._request(session, "GET", url)
        async with response:
            if response.status != 200:
                raise _http_error(url, response.status)
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(_DOWNLOAD_CHUNK):
                chunks.extend(chunk)
                if len(chunks) > MAX_IMAGE_BYTES:
                    raise ImageGenerationError(
                        "infra", f"fal image exceeds the {MAX_IMAGE_BYTES}-byte limit"
                    )
            return bytes(chunks)

    async def _json(
        self, response: aiohttp.ClientResponse, url: str
    ) -> dict[str, Any]:
        body = await response.text()
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ImageGenerationError(
                "infra", _truncate(f"fal invalid response from {url}: {exc}")
            ) from exc
        if not isinstance(data, dict):
            raise ImageGenerationError(
                "infra", f"fal invalid response from {url}: not a JSON object"
            )
        return data

    async def _request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        """One HTTP call; retries transient failures only (network errors,
        429, 5xx) with exponential backoff. The caller owns the response."""
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            transient: str | None = None
            try:
                response = await session.request(
                    method,
                    url,
                    headers={"Authorization": f"Key {self._api_key}"},
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    **kwargs,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                transient = f"{type(exc).__name__}: {exc}"
            else:
                if response.status == 429 or response.status >= 500:
                    transient = f"http {response.status}"
                    response.release()
                else:
                    return response
            if attempt == attempts:
                raise ImageGenerationError(
                    "infra",
                    _truncate(
                        f"fal {method} {url}: transient failure "
                        f"after {attempts} attempts: {transient}"
                    ),
                )
            await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover
