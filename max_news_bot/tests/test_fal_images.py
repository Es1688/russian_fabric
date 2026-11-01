"""fal.ai image generation tests (plan §3.1, §3.5).

Covers the queue flow against a local FakeFalServer (submit -> poll ->
response -> download), the error taxonomy (401 -> config, FAILED / 5xx /
timeout / oversized -> infra), transient-only retries and the
FallbackImageGenerator failover with its per-provider events and config
alerts (the YandexART client itself lives in test_yandex_art.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from max_news_bot.images import (
    MAX_IMAGE_BYTES,
    FalImageGenerator,
    FallbackImageGenerator,
    ImageGenerationError,
)
from max_news_bot.tests.mocks import MockAlerter
from max_news_bot.utils.db import Database

_MODEL = "fal-ai/flux/dev"
_API_KEY = "test-fal-key"
_PROMPT = "a cozy morning in a code cave, flat illustration"


class FakeFalServer:
    """Local aiohttp server impersonating the fal.ai queue API.

    Submit statuses and poll statuses are played back from lists (the
    last element repeats), so tests script retries and multi-step polls.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.url = ""
        self.submit_statuses: list[int] = [200]
        self.statuses: list[str] = ["COMPLETED"]
        self.image = b"\x89PNG fake image bytes"
        self.response_payload: dict[str, Any] | None = None
        self.submit_requests: list[dict[str, Any]] = []
        self.status_requests: list[str] = []
        self.response_requests = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        base = self

        async def submit(request: web.Request) -> web.Response:
            base.submit_requests.append(
                {"headers": dict(request.headers), "json": await request.json()}
            )
            index = min(len(base.submit_requests) - 1, len(base.submit_statuses) - 1)
            status = base.submit_statuses[index]
            if status != 200:
                return web.Response(status=status, text="boom")
            return web.json_response(
                {
                    "request_id": "req-1",
                    "status_url": f"{base.url}/status",
                    "response_url": f"{base.url}/response",
                }
            )

        async def status(request: web.Request) -> web.Response:
            index = min(len(base.status_requests), len(base.statuses) - 1)
            current = base.statuses[index]
            base.status_requests.append(current)
            return web.json_response({"status": current})

        async def response(request: web.Request) -> web.Response:
            base.response_requests += 1
            payload = base.response_payload or {
                "images": [{"url": f"{base.url}/image.png"}]
            }
            return web.json_response(payload)

        async def image(request: web.Request) -> web.Response:
            return web.Response(body=base.image, content_type="image/png")

        app = web.Application()
        app.router.add_get("/status", status)
        app.router.add_get("/response", response)
        app.router.add_get("/image.png", image)
        app.router.add_post(f"/{self.model}", submit)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        port = runner.addresses[0][1]
        self._runner = runner
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def fal_server():
    server = FakeFalServer(_MODEL)
    await server.start()
    yield server
    await server.stop()


def make_generator(base_url: str, **overrides: Any) -> FalImageGenerator:
    params: dict[str, Any] = {
        "api_key": _API_KEY,
        "model": _MODEL,
        "timeout": 5.0,
        "poll_interval": 0.01,
        "width": 1024,
        "height": 768,
        "base_url": base_url,
        "max_retries": 2,
        "backoff": 0.0,
        "session": None,
    }
    params.update(overrides)
    return FalImageGenerator(**params)


class StubGenerator:
    """Recording ImageGenerator double returning fixed bytes or raising."""

    def __init__(
        self,
        result: bytes | None = None,
        error: ImageGenerationError | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> bytes:
        self.prompts.append(prompt)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


async def test_success_flow_returns_image_bytes(fal_server) -> None:
    server = fal_server
    server.statuses = ["IN_PROGRESS", "COMPLETED"]

    data = await make_generator(server.url).generate(_PROMPT)

    assert data == server.image
    assert len(server.submit_requests) == 1
    request = server.submit_requests[0]
    assert request["headers"]["Authorization"] == f"Key {_API_KEY}"
    assert request["json"]["prompt"] == _PROMPT
    assert request["json"]["image_size"] == {"width": 1024, "height": 768}
    # the queue played IN_PROGRESS before the final COMPLETED
    assert server.status_requests == ["IN_PROGRESS", "COMPLETED"]
    assert server.response_requests == 1


async def test_failed_status_is_infra(fal_server) -> None:
    server = fal_server
    server.statuses = ["IN_PROGRESS", "FAILED"]

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(server.url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert "FAILED" in str(excinfo.value)


async def test_unauthorized_is_config_without_key_leak(fal_server) -> None:
    server = fal_server
    server.submit_statuses = [401]

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(server.url).generate(_PROMPT)

    assert excinfo.value.kind == "config"
    assert "401" in str(excinfo.value)
    assert _API_KEY not in str(excinfo.value)


async def test_server_error_is_retried_then_succeeds(fal_server) -> None:
    server = fal_server
    server.submit_statuses = [500, 200]

    data = await make_generator(server.url).generate(_PROMPT)

    assert data == server.image
    assert len(server.submit_requests) == 2


async def test_retries_exhausted_is_infra(fal_server) -> None:
    server = fal_server
    server.submit_statuses = [500]

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(server.url, max_retries=1).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert len(server.submit_requests) == 2


async def test_non_transient_400_is_not_retried(fal_server) -> None:
    server = fal_server
    server.submit_statuses = [400]

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(server.url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert len(server.submit_requests) == 1


async def test_poll_timeout_is_infra(fal_server) -> None:
    server = fal_server
    server.statuses = ["IN_PROGRESS"]

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(server.url, timeout=0.2, poll_interval=0.02).generate(
            _PROMPT
        )

    assert excinfo.value.kind == "infra"
    assert "timeout" in str(excinfo.value)
    assert len(server.status_requests) > 1


async def test_oversized_image_is_infra(fal_server) -> None:
    server = fal_server
    server.image = b"x" * (MAX_IMAGE_BYTES + 1)

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(server.url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert str(MAX_IMAGE_BYTES) in str(excinfo.value)


async def test_missing_images_in_response_is_infra(fal_server) -> None:
    server = fal_server
    server.response_payload = {"seed": 42}

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(server.url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert "images[0].url" in str(excinfo.value)


async def test_wrapper_emits_provider_event_on_success(tmp_path: Path) -> None:
    db = Database(tmp_path / "stats.db")
    await db.initialize()
    primary = StubGenerator(result=b"primary")

    await FallbackImageGenerator(primary, None, db=db).generate(_PROMPT)

    event = await db.get_last_event("image_fal_ok")
    assert event is not None and "provider=fal" in event["details"]


async def test_wrapper_keeps_the_provider_event_without_fal(
    tmp_path: Path,
) -> None:
    """Regression guard: an art-only wiring must keep emitting its
    success event - the observer may not depend on fal being wired."""
    db = Database(tmp_path / "stats.db")
    await db.initialize()
    art = StubGenerator(result=b"art")

    generator = FallbackImageGenerator(
        art, None, primary_name="yandex_art", db=db
    )
    await generator.generate(_PROMPT)

    event = await db.get_last_event("image_art_used")
    assert event is not None and "provider=yandex_art" in event["details"]
    assert await db.get_last_event("image_fal_ok") is None


async def test_wrapper_emits_art_event_and_alerts_on_config_failure(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "stats.db")
    await db.initialize()
    alerter = MockAlerter()
    primary = StubGenerator(error=ImageGenerationError("config", "fal http 401"))
    fallback = StubGenerator(result=b"reserve")

    generator = FallbackImageGenerator(
        primary, fallback, db=db, alerter=alerter
    )
    data = await generator.generate(_PROMPT)

    assert data == b"reserve"
    event = await db.get_last_event("image_art_used")
    assert event is not None and "primary_kind=config" in event["details"]
    assert len(alerter.alerts) == 1 and "fal" in alerter.alerts[0]
    assert "401" in alerter.alerts[0]


async def test_wrapper_dedups_config_alerts_per_provider_per_day(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "stats.db")
    await db.initialize()
    alerter = MockAlerter()
    # one wrapper for the whole run, as the runtime wiring builds it: the
    # daily dedup lives in the wrapper instance
    generator = FallbackImageGenerator(
        StubGenerator(error=ImageGenerationError("config", "fal http 401")),
        StubGenerator(result=b"reserve"),
        db=db,
        alerter=alerter,
    )

    await generator.generate(_PROMPT)
    await generator.generate(_PROMPT)

    assert len(alerter.alerts) == 1


async def test_fallback_not_called_when_primary_succeeds() -> None:
    primary = StubGenerator(result=b"primary")
    fallback = StubGenerator(result=b"fallback")

    data = await FallbackImageGenerator(primary, fallback).generate(_PROMPT)

    assert data == b"primary"
    assert primary.prompts == [_PROMPT]
    assert fallback.prompts == []


async def test_primary_failure_without_fallback_propagates() -> None:
    primary = StubGenerator(error=ImageGenerationError("infra", "fal down"))
    generator = FallbackImageGenerator(primary)

    with pytest.raises(ImageGenerationError) as excinfo:
        await generator.generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert primary.prompts == [_PROMPT]


async def test_fallback_rescues_primary_failure() -> None:
    primary = StubGenerator(error=ImageGenerationError("config", "fal http 401"))
    fallback = StubGenerator(result=b"reserve")

    data = await FallbackImageGenerator(primary, fallback).generate(_PROMPT)

    assert data == b"reserve"
    assert fallback.prompts == [_PROMPT]


async def test_both_failures_propagate_fallback_error_with_context() -> None:
    primary = StubGenerator(error=ImageGenerationError("infra", "fal down"))
    fallback = StubGenerator(error=ImageGenerationError("config", "art 401"))

    with pytest.raises(ImageGenerationError) as excinfo:
        await FallbackImageGenerator(primary, fallback).generate(_PROMPT)

    # the fallback's kind classifies the outcome; the message keeps both
    # failures so a both-broken incident is diagnosable from bot_stats
    assert excinfo.value.kind == "config"
    assert "fal down" in str(excinfo.value)
    assert "art 401" in str(excinfo.value)
