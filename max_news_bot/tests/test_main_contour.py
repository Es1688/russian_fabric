"""Startup contour commutation tests (plan sections 8, 14).

``main()`` must run exactly one publishing contour:

- RUBRICS_ENABLED=true: the slot scheduler plus its summary and backup
  tasks start, and neither legacy loop (digest ``scheduler_loop``, factory
  ``factory_scheduler_loop``) does;
- flag off (the default): the legacy loops start exactly as before and no
  rubrics task exists;
- both contours enabled: ``load_config`` fails fast, and the failure
  happens on the real startup path - before any task is created.

The real ``main`` runs against the shipped configs; only the polling
loop, the loop functions themselves and the two best-effort MAX startup
calls are replaced, so the commutation under test is the production one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import max_news_bot.main as main_module
from max_news_bot.max_api.client import MaxClient


class LoopRecorder:
    """Replaces every background loop; records its name and blocks."""

    def __init__(self) -> None:
        self.started: list[str] = []

    def make(self, name: str):
        async def fake_loop(*args, **kwargs) -> None:
            self.started.append(name)
            await asyncio.sleep(3600)  # cancelled by main's finally block

        return fake_loop


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


def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_BOT_TOKEN", "123456789:test-token")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "111,222")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "max_news_bot.db"))
    monkeypatch.delenv("FACTORY_ENABLED", raising=False)
    monkeypatch.delenv("RUBRICS_ENABLED", raising=False)
    monkeypatch.delenv("RUBRICS_CONFIG_PATH", raising=False)
    monkeypatch.delenv("RUBRICS_DRY_RUN", raising=False)
    monkeypatch.delenv("NEWS_SOURCES_PATH", raising=False)


async def test_rubrics_mode_starts_contour_tasks_only(
    recorder: LoopRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    recorder: LoopRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the contour starts with admin previews, not channel posts (plan
    # section 14, stage 1); RUBRICS_DRY_RUN is unset on purpose
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RUBRICS_ENABLED", "true")
    monkeypatch.setenv("FACTORY_ENABLED", "false")

    captured: dict[str, object] = {}
    real_build = main_module.build_rubrics_runtime

    def spy_build(config, db, client, session, fetcher):
        runtime = real_build(config, db, client, session, fetcher)
        captured["dry_run"] = runtime.scheduler._dry_run
        return runtime

    monkeypatch.setattr(main_module, "build_rubrics_runtime", spy_build)

    await asyncio.wait_for(main_module.main(), timeout=10)

    assert captured["dry_run"] is True


async def test_legacy_mode_starts_legacy_loops_only(
    recorder: LoopRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
