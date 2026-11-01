"""Startup tests of the single publishing contour.

``main()`` always runs the rubrics contour: the slot scheduler plus its
summary and backup tasks start, and polling is the main awaited task. The
legacy loops (digest ``scheduler_loop``, factory ``factory_scheduler_loop``)
are gone together with their env switches.

Dual-channel wiring (plan §2.4/§2.5): the completion is wrapped into the
fallback wrapper over two local fake channels, and the readiness probe runs
before any task starts - a config-kind failure (401) of ANY channel fails
the launch with ValueError, an infra-kind one (500) only warns and the
startup continues.

The real ``main`` runs against the shipped configs; only the polling
loop, the loop functions themselves and the two best-effort MAX startup
calls are replaced, so the startup under test is the production one.
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
from max_news_bot.writers.fallback import FallbackCompletion


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
    loader's env overrides; no external network is touched: the readiness
    probe must hit the fakes, never the real providers."""
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
    monkeypatch.delenv("RUBRICS_CONFIG_PATH", raising=False)
    monkeypatch.delenv("RUBRICS_DRY_RUN", raising=False)
    monkeypatch.delenv("NEWS_SOURCES_PATH", raising=False)


async def test_main_starts_the_contour_tasks(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
) -> None:
    _base_env(monkeypatch, tmp_path)

    await asyncio.wait_for(main_module.main(), timeout=10)

    assert set(recorder.started) == {
        "slot_scheduler_loop",
        "daily_summary_loop",
        "daily_backup_loop",
    }


async def test_main_dry_run_by_default(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
) -> None:
    # the contour starts with admin previews, not channel posts (plan
    # section 14, stage 1); RUBRICS_DRY_RUN is unset on purpose
    _base_env(monkeypatch, tmp_path)

    captured: dict[str, object] = {}
    real_build = main_module.build_rubrics_runtime

    async def spy_build(config, db, client, session, fetcher, **kwargs):
        runtime = await real_build(config, db, client, session, fetcher, **kwargs)
        captured["dry_run"] = runtime.scheduler._dry_run
        return runtime

    monkeypatch.setattr(main_module, "build_rubrics_runtime", spy_build)

    await asyncio.wait_for(main_module.main(), timeout=10)

    assert captured["dry_run"] is True


async def test_contour_wires_fallback_completion_and_probes_channels(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
) -> None:
    _base_env(monkeypatch, tmp_path)
    primary, fallback = llm_channels

    captured: dict[str, object] = {}
    real_build = main_module.build_rubrics_runtime

    async def spy_build(config, db, client, session, fetcher, **kwargs):
        runtime = await real_build(config, db, client, session, fetcher, **kwargs)
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
async def test_readiness_config_kind_fails_startup_before_any_task(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
    channel: str,
) -> None:
    _base_env(monkeypatch, tmp_path)
    primary, fallback = llm_channels
    (primary if channel == "primary" else fallback).status = 401

    with pytest.raises(ValueError, match="readiness probe"):
        await asyncio.wait_for(main_module.main(), timeout=10)

    # a broken key is a static deploy error: no background loop may start
    assert recorder.started == []


async def test_startup_failure_closes_both_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a config-kind startup failure must not leak the sessions: main()
    # closes them before re-raising, keeping crash-loop logs free of
    # "Unclosed client session" noise (seen on the VPS 401 crash-loop)
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("EGRESS_PROXY_URL", "socks5h://127.0.0.1:1080")

    captured: dict[str, Any] = {}

    async def failing_build(config, db, client, session, fetcher, **kwargs):
        captured["direct"] = session
        captured["outbound"] = kwargs["outbound_session"]
        raise ValueError("readiness probe failed")

    monkeypatch.setattr(main_module, "build_rubrics_runtime", failing_build)

    with pytest.raises(ValueError, match="readiness probe failed"):
        await asyncio.wait_for(main_module.main(), timeout=10)

    assert captured["direct"].closed is True
    assert captured["outbound"].closed is True


@pytest.mark.parametrize("channel", ["primary", "fallback"])
async def test_readiness_infra_kind_warns_and_startup_continues(
    recorder: LoopRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_channels: tuple[FakeLlmServer, FakeLlmServer],
    caplog: pytest.LogCaptureFixture,
    channel: str,
) -> None:
    _base_env(monkeypatch, tmp_path)
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
    assert set(recorder.started) == {
        "slot_scheduler_loop",
        "daily_summary_loop",
        "daily_backup_loop",
    }


def server_base(server: FakeLlmServer) -> str:
    """The base URL the server was started on (the env override value)."""
    assert server._runner is not None
    assert server._runner.addresses, "test server did not bind"
    return f"http://127.0.0.1:{server._runner.addresses[0][1]}/v1"
