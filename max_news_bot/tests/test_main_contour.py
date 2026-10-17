"""Startup contour commutation tests (plan sections 8, 14).

``main()`` must run exactly one publishing contour:

- RUBRICS_ENABLED=true: the slot scheduler plus its summary and backup
  tasks start, and neither legacy loop (digest ``scheduler_loop``, factory
  ``factory_scheduler_loop``) does;
- flag off (the default): the legacy loops start exactly as before and no
  rubrics task exists;
- both contours enabled: ``load_config`` fails fast, and the failure
  happens on the real startup path - before any task is created.

Dual-channel wiring (plan §2.4/§2.5): in both contours the writer /
completion is wrapped into the fallback wrapper over two local fake
channels, and the readiness probe runs before any task starts - a
config-kind failure (401) of ANY channel fails the launch with ValueError,
an infra-kind one (500) only warns and the startup continues.

The real ``main`` runs against the shipped configs; only the polling
loop, the loop functions themselves and the two best-effort MAX startup
calls are replaced, so the commutation under test is the production one.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

import max_news_bot.main as main_module
from max_news_bot.max_api.client import MaxClient
from max_news_bot.writers.completion import LlmCompletionClient
from max_news_bot.writers.fallback import FallbackCompletion, FallbackWriter
from max_news_bot.writers.llm import LlmWriter


class LoopRecorder:
    """Replaces every background loop; records its name and blocks."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def make(self, name: str):
        async def fake_loop(*args, **kwargs) -> None:
            self.started.append(name)
            await asyncio.sleep(3600)  # cancelled by main's finally block

        return fake_loop


class FakeLlmServer:
    """Local aiohttp server impersonating one OpenAI-compatible channel.

    Records request payloads; ``status`` switches the reply between a
    minimal valid completion and an error code for the probe scenarios.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def completions(request: web.Request) -> web.Response:
            self.requests.append(await request.json())
            payload = {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 1},
            }
            return web.json_response(payload, status=self.status)

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completions)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        self._runner = runner
        return f"http://127.0.0.1:{runner.addresses[0][1]}/v1"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


async def _fake_get_me(self) -> dict:
    return {"name": "test-bot"}


async def _fake_patch_commands(self, commands) -> None:
    return None


async def _fake_polling(client, handle_update, db) -> None:
    # the main awaited task: returning ends main() and cancels the rest
    return None


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> LoopRecorder:
    rec = LoopRecorder()
    for name in (
        "scheduler_loop",
        "factory_scheduler_loop",
        "slot_scheduler_loop",
        "daily_summary_loop",
        "daily_backup_loop",
    ):
        monkeypatch.setattr(main_module, name, rec.make(name), raising=True)
    monkeypatch.setattr(main_module, "polling_loop", _fake_polling)
    monkeypatch.setattr(MaxClient, "get_me", _fake_get_me)
    monkeypatch.setattr(MaxClient, "patch_commands", _fake_patch_commands)
    return rec


@pytest.fixture
async def llm_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeLlmServer, FakeLlmServer]:
    """Two local channels wired as primary and fallback through the
    loader's env overrides; no external network is touched. Any test that
    activates an LLM contour needs them: the readiness probe must hit the
    fakes, never the real providers."""
    primary = FakeLlmServer()
    fallback = FakeLlmServer()
    monkeypatch.setenv("LLM_BASE_URL", await primary.start())
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", await fallback.start())
    yield primary, fallback
    await primary.stop()
    await fallback.stop()


def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_BOT_TOKEN", "123456789:test-token")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "111,222")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "max_news_bot.db"))
    monkeypatch.delenv("FACTORY_ENABLED", raising=False)
    monkeypatch.delenv("RUBRICS_ENABLED", raising=False)
    monkeypatch.delenv("RUBRICS_CONFIG_PATH", raising=False)
    monkeypatch.delenv("RUBRICS_DRY_RUN", raising=False)
    monkeypatch.delenv("NEWS_SOURCES_PATH", raising=False)


async def test_rubrics_mode_starts_contour_tasks_only(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "true")
    monkeypatch.setenv("FACTORY_ENABLED", "false")

    await asyncio.wait_for(main_module.main(), timeout=10)

    assert set(recorder.started) == {
        "slot_scheduler_loop",
        "daily_summary_loop",
        "daily_backup_loop",
    }


async def test_rubrics_mode_dry_run_by_default(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
) -> None:
    # the contour starts with admin previews, not channel posts (plan
    # section 14, stage 1); RUBRICS_DRY_RUN is unset on purpose
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "true")
    monkeypatch.setenv("FACTORY_ENABLED", "false")

    captured: dict[str, object] = {}
    real_build = main_module.build_rubrics_runtime

    async def spy_build(config, db, client, session, fetcher):
        runtime = await real_build(config, db, client, session, fetcher)
        captured["dry_run"] = runtime.scheduler._dry_run
        return runtime

    monkeypatch.setattr(main_module, "build_rubrics_runtime", spy_build)

    await asyncio.wait_for(main_module.main(), timeout=10)

    assert captured["dry_run"] is True


async def test_legacy_mode_starts_legacy_loops_only(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "false")
    # both legacy cycles on: the factory too, so its loop start is covered
    monkeypatch.setenv("FACTORY_ENABLED", "true")

    await asyncio.wait_for(main_module.main(), timeout=10)

    assert set(recorder.started) == {"scheduler_loop", "factory_scheduler_loop"}


async def test_legacy_mode_without_factory_starts_digest_loop_only(
    recorder: LoopRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "false")
    monkeypatch.setenv("FACTORY_ENABLED", "false")

    await asyncio.wait_for(main_module.main(), timeout=10)

    assert set(recorder.started) == {"scheduler_loop"}


async def test_conflicting_contours_fail_fast_before_any_task(
    recorder: LoopRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "true")
    monkeypatch.setenv("FACTORY_ENABLED", "true")

    with pytest.raises(ValueError, match="Conflicting publishing contours"):
        await asyncio.wait_for(main_module.main(), timeout=10)

    assert recorder.started == []


# --- dual-channel wiring (plan §2.4/§2.5) ---------------------------------------


async def test_factory_contour_wires_fallback_writer_and_probes_channels(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "false")
    monkeypatch.setenv("FACTORY_ENABLED", "true")
    primary, fallback = llm_channels

    real_cls = main_module.ContentFactory
    built: dict[str, object] = {}

    def spy_factory(config, fetcher, writer, post_publisher, alerter, db):
        built["writer"] = writer
        built["alerter"] = alerter
        return real_cls(config, fetcher, writer, post_publisher, alerter, db)

    monkeypatch.setattr(main_module, "ContentFactory", spy_factory)

    await asyncio.wait_for(main_module.main(), timeout=10)

    writer = built["writer"]
    assert isinstance(writer, FallbackWriter)
    assert isinstance(writer._primary, LlmWriter)
    assert isinstance(writer._fallback, LlmWriter)
    assert writer._primary._base_url == server_base(primary)
    assert writer._fallback._base_url == server_base(fallback)
    # timeout / max_tokens / temperature are shared with the primary
    assert writer._fallback._timeout == writer._primary._timeout
    assert writer._fallback._max_tokens == writer._primary._max_tokens
    # the alert hook is wired to the contour's own alerter
    assert writer._observer._alerter is built["alerter"]
    # readiness probe of BOTH channels before the loops started
    assert len(primary.requests) == 1
    assert primary.requests[0]["max_tokens"] == 1
    assert len(fallback.requests) == 1
    assert fallback.requests[0]["max_tokens"] == 1
    assert set(recorder.started) == {"scheduler_loop", "factory_scheduler_loop"}


async def test_rubrics_contour_wires_fallback_completion_and_probes_channels(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "true")
    monkeypatch.setenv("FACTORY_ENABLED", "false")
    primary, fallback = llm_channels

    captured: dict[str, object] = {}
    real_build = main_module.build_rubrics_runtime

    async def spy_build(config, db, client, session, fetcher):
        runtime = await real_build(config, db, client, session, fetcher)
        captured["runtime"] = runtime
        captured["config"] = config
        captured["db"] = db
        return runtime

    monkeypatch.setattr(main_module, "build_rubrics_runtime", spy_build)

    await asyncio.wait_for(main_module.main(), timeout=10)

    runtime = captured["runtime"]
    config = captured["config"]
    generative_rubric = next(
        r for r in config.rubrics.rubrics if r.kind == "generative"
    )
    digest_rubric = next(r for r in config.rubrics.rubrics if r.kind == "digest")
    router = runtime.scheduler._pipeline
    generative = router.pipeline_for(generative_rubric)
    completion = generative._llm
    assert isinstance(completion, FallbackCompletion)
    assert isinstance(completion._primary, LlmCompletionClient)
    assert isinstance(completion._fallback, LlmCompletionClient)
    # digest and the bank reserve share the same wrapped completion
    assert router.pipeline_for(digest_rubric)._llm is completion
    assert completion._primary._base_url == server_base(primary)
    assert completion._fallback._base_url == server_base(fallback)
    assert completion._fallback._timeout == completion._primary._timeout
    assert completion._observer._alerter is runtime.alerter
    assert completion._observer._db is captured["db"]
    # the probe is the construction's only network call: one per channel
    assert len(primary.requests) == 1
    assert primary.requests[0]["max_tokens"] == 1
    assert len(fallback.requests) == 1
    assert fallback.requests[0]["max_tokens"] == 1


@pytest.mark.parametrize("channel", ["primary", "fallback"])
@pytest.mark.parametrize("contour", ["factory", "rubrics"])
async def test_readiness_config_kind_fails_startup_before_any_task(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
    contour: str,
    channel: str,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "true" if contour == "rubrics" else "false")
    monkeypatch.setenv("FACTORY_ENABLED", "true" if contour == "factory" else "false")
    primary, fallback = llm_channels
    (primary if channel == "primary" else fallback).status = 401

    with pytest.raises(ValueError, match="readiness probe"):
        await asyncio.wait_for(main_module.main(), timeout=10)

    # a broken key is a static deploy error: no background loop may start
    assert recorder.started == []


@pytest.mark.parametrize("channel", ["primary", "fallback"])
@pytest.mark.parametrize("contour", ["factory", "rubrics"])
async def test_readiness_infra_kind_warns_and_startup_continues(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
    caplog: pytest.LogCaptureFixture,
    contour: str,
    channel: str,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "true" if contour == "rubrics" else "false")
    monkeypatch.setenv("FACTORY_ENABLED", "true" if contour == "factory" else "false")
    primary, fallback = llm_channels
    (primary if channel == "primary" else fallback).status = 500

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(main_module.main(), timeout=10)

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "llm_probe_degraded" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert f"channel={channel}" in warnings[0].getMessage()
    expected_loops = (
        {"slot_scheduler_loop", "daily_summary_loop", "daily_backup_loop"}
        if contour == "rubrics"
        else {"scheduler_loop", "factory_scheduler_loop"}
    )
    assert set(recorder.started) == expected_loops


def server_base(server: FakeLlmServer) -> str:
    """The base URL the server was started on (the env override value)."""
    assert server._runner is not None
    assert server._runner.addresses, "test server did not bind"
    return f"http://127.0.0.1:{server._runner.addresses[0][1]}/v1"
