"""YandexART client tests (plan §3.1, research 2026-10-09 §2).

A local fake of the Yandex AI Studio Images API covers the synchronous
call contract: the model URI / size / prompt cap in the payload, the
auth-scheme retry (Bearer first, Api-Key + OpenAI-Project after a 401 —
the rejected auth is not billed, so it is the only repeat), the
config/infra taxonomy without transient retries, and the size check
before the base64 decode.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from aiohttp import web

from max_news_bot.images import (
    MAX_IMAGE_BYTES,
    ImageGenerationError,
    YandexArtGenerator,
)
from max_news_bot.images.yandex_art import folder_id_from_fallback_model

_API_KEY = "test-yandex-key"
_FOLDER_ID = "b1gtestfolder"
_MODEL = "aliceai-image-art-3.0"
_PROMPT = "a cozy morning in a code cave, flat illustration"
_PNG = b"\x89PNG fake image bytes"


class FakeYandexArtServer:
    """Local aiohttp server impersonating the Images API. ``statuses``
    play back per attempt (the last element repeats); ``override_payload``
    replaces the response body, ``raw_body`` breaks the JSON on purpose."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.statuses: list[int] = []
        self.b64 = base64.b64encode(_PNG).decode()
        self.override_payload: Any = None
        self.raw_body: str | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        base = self

        async def generations(request: web.Request) -> web.Response:
            base.requests.append(
                {"headers": dict(request.headers), "json": await request.json()}
            )
            index = min(len(base.requests) - 1, len(base.statuses) - 1)
            status = base.statuses[index] if base.statuses else 200
            if base.raw_body is not None:
                return web.Response(
                    status=status, text=base.raw_body, content_type="text/plain"
                )
            if status != 200:
                return web.Response(status=status, text="denied")
            payload = base.override_payload or {"data": [{"b64_json": base.b64}]}
            return web.json_response(payload)

        app = web.Application()
        app.router.add_post("/v1/images/generations", generations)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        port = runner.addresses[0][1]
        self._runner = runner
        return f"http://127.0.0.1:{port}/v1"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def art_server():
    server = FakeYandexArtServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


def make_generator(base_url: str, **overrides: Any) -> YandexArtGenerator:
    params: dict[str, Any] = {
        "api_key": _API_KEY,
        "folder_id": _FOLDER_ID,
        "model": _MODEL,
        "size": "1024x1024",
        "timeout": 5.0,
        "base_url": base_url,
        "session": None,
    }
    params.update(overrides)
    return YandexArtGenerator(**params)


# --- happy path -----------------------------------------------------------------


async def test_success_decodes_b64_and_sends_the_contract_payload(
    art_server,
) -> None:
    server, base_url = art_server

    data = await make_generator(base_url).generate(_PROMPT)

    assert data == _PNG
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["headers"]["Authorization"] == f"Bearer {_API_KEY}"
    assert request["json"] == {
        "model": f"art://{_FOLDER_ID}/{_MODEL}",
        "prompt": _PROMPT,
        "size": "1024x1024",
    }


async def test_prompt_is_capped_at_500_characters(art_server) -> None:
    server, base_url = art_server

    await make_generator(base_url).generate("я" * 600)

    assert server.requests[0]["json"]["prompt"] == "я" * 500


# --- auth-scheme retry (the only repeat) ------------------------------------------


async def test_401_on_bearer_retries_with_api_key_scheme(art_server) -> None:
    server, base_url = art_server
    server.statuses = [401, 200]

    data = await make_generator(base_url).generate(_PROMPT)

    assert data == _PNG
    assert len(server.requests) == 2
    assert server.requests[0]["headers"]["Authorization"] == f"Bearer {_API_KEY}"
    second = server.requests[1]["headers"]
    assert second.get("Api-Key") == _API_KEY
    assert second.get("OpenAI-Project") == _FOLDER_ID


async def test_401_on_both_schemes_is_config_without_key_leak(
    art_server,
) -> None:
    server, base_url = art_server
    server.statuses = [401, 401]

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(base_url).generate(_PROMPT)

    assert excinfo.value.kind == "config"
    assert "Bearer" in str(excinfo.value) and "Api-Key" in str(excinfo.value)
    assert _API_KEY not in str(excinfo.value)
    assert len(server.requests) == 2


async def test_402_balance_is_config_without_retry(art_server) -> None:
    server, base_url = art_server
    server.statuses = [402]

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(base_url).generate(_PROMPT)

    assert excinfo.value.kind == "config"
    assert len(server.requests) == 1  # the paid call is never repeated


# --- infra taxonomy: no transient retries -----------------------------------------


async def test_500_is_infra_without_retry(art_server) -> None:
    server, base_url = art_server
    server.statuses = [500]

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(base_url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert len(server.requests) == 1


async def test_connection_failure_is_infra() -> None:
    # a dead loopback port: the transport error must not escape the contract
    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator("http://127.0.0.1:1/v1").generate(_PROMPT)

    assert excinfo.value.kind == "infra"


# --- response shape ---------------------------------------------------------------


async def test_invalid_json_is_infra(art_server) -> None:
    server, base_url = art_server
    server.raw_body = "<html>not json</html>"

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(base_url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert "invalid response" in str(excinfo.value)


async def test_missing_b64_json_is_infra(art_server) -> None:
    server, base_url = art_server
    server.override_payload = {"data": [{"revised_prompt": "something"}]}

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(base_url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert "b64_json" in str(excinfo.value)


async def test_malformed_base64_is_infra(art_server) -> None:
    server, base_url = art_server
    server.override_payload = {"data": [{"b64_json": "not base64 at all!!"}]}

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(base_url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert "base64" in str(excinfo.value)


async def test_oversized_b64_is_rejected_before_decoding(art_server) -> None:
    server, base_url = art_server
    # ~14 MB of base64 for an 11 MB image: the length check fires before
    # base64.b64decode allocates anything
    server.override_payload = {
        "data": [{"b64_json": "A" * (MAX_IMAGE_BYTES * 4 // 3 + 8)}]
    }

    with pytest.raises(ImageGenerationError) as excinfo:
        await make_generator(base_url).generate(_PROMPT)

    assert excinfo.value.kind == "infra"
    assert str(MAX_IMAGE_BYTES) in str(excinfo.value)


# --- folder id extraction -----------------------------------------------------------


def test_folder_id_parses_from_a_real_fallback_model() -> None:
    assert (
        folder_id_from_fallback_model("gpt://b1greal123/yandexgpt-5-lite")
        == "b1greal123"
    )


def test_folder_id_rejects_placeholder_and_foreign_models() -> None:
    # the yaml default ships the placeholder, not a real folder
    assert folder_id_from_fallback_model("gpt://FOLDER_ID/yandexgpt-5-lite") is None
    # a non-gpt:// model carries no folder for the art:// URI
    assert folder_id_from_fallback_model("fallback-model") is None
    assert folder_id_from_fallback_model("gpt://only-folder") is None
