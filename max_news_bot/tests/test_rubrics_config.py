from __future__ import annotations

from pathlib import Path

import pytest

from max_news_bot.config.loader import load_config
from max_news_bot.config.rubrics import (
    RubricsConfigError,
    WEEKDAYS,
    load_rubrics_config,
)

SHIPPED_YAML = Path(__file__).resolve().parents[1] / "config" / "rubrics.yaml"

# Smallest valid rubrics config: everything except id/time/kind is optional.
MINIMAL_YAML = """
rubrics:
  - id: stub
    time: "09:00"
    kind: bank
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "rubrics.yaml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def rubrics_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # load_rubrics_config never calls load_dotenv; only its own env flags.
    # The shipped grid has LLM rubrics, so both channel keys are set for the
    # default tests; tests of the fail-fast rules delete them explicitly.
    monkeypatch.delenv("RUBRICS_ENABLED", raising=False)
    monkeypatch.delenv("RUBRICS_CONFIG_PATH", raising=False)
    monkeypatch.delenv("RUBRICS_DRY_RUN", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")


def _load_with(tmp_path: Path, content: str) -> None:
    load_rubrics_config(_write_config(tmp_path, content))


# --- shipped config ----------------------------------------------------------


def test_shipped_config_loads(rubrics_env: None) -> None:
    config = load_rubrics_config()

    assert config.timezone == "Europe/Moscow"
    assert "старшего возраста" in config.persona
    assert "тёплые цвета" in config.image_style
    # LLM channels moved here from the removed sources.yaml: one contour -
    # one config file; the keys themselves stay env-only
    assert config.llm.primary.base_url == "https://openrouter.ai/api/v1"
    assert config.llm.primary.model == "qwen/qwen3.8-flash"
    assert config.llm.primary.api_key == "test-openrouter-key"
    assert config.llm.primary.disable_reasoning is True
    assert config.llm.fallback is not None
    assert config.llm.fallback.base_url == "https://ai.api.cloud.yandex.net/v1"
    assert config.llm.fallback.api_key == "test-llm-key"
    # image generation section (dual-channel plan section 3): fal is the
    # active provider, yandex_art is reserved as a stub
    assert config.image_generation.fal.model == "fal-ai/flux/dev"
    assert config.image_generation.yandex_art.size == "1024x1024"
    assert config.image_generation.timeout_seconds == 120
    assert config.daily_cap == 6
    # daily summary slot (plan section 11): 22:40, after the last 21:30 post
    assert config.summary_time == "22:40"
    # contour dry-run (plan section 14, stage 1): previews by default, the
    # cutover to publishing is a deliberate RUBRICS_DRY_RUN=false flip
    assert config.dry_run is True
    assert config.targets["main"].channel_id == -1001234567890

    # rubric grid from plan section 2: time, kind, grace (60/30/30/30/20/60)
    by_id = {rubric.id: rubric for rubric in config.rubrics}
    assert list(by_id) == [
        "good_morning",
        "weather",
        "topic_day",
        "second_post",
        "good_news",
        "good_night",
        # stage-1 shakedown stub (plan section 14): full slot cycle without
        # LLM, dry-run previews to admins
        "smoke_test",
    ]
    assert by_id["good_morning"].time == "07:00"
    assert by_id["good_morning"].kind == "bank"
    assert by_id["good_morning"].grace_minutes == 60
    assert by_id["good_morning"].image == "postcard"
    assert by_id["weather"].time == "07:05"
    assert by_id["weather"].kind == "weather"
    assert by_id["weather"].grace_minutes == 30
    # jitter cap so weather never precedes the morning post (plan section 2)
    assert by_id["weather"].jitter_minutes <= 2
    # weather has no reserve post: a failed slot is skipped (plan section 4)
    assert by_id["weather"].on_llm_failure == "skip"
    assert by_id["weather"].max_chars == 4000
    assert by_id["topic_day"].kind == "generative"
    assert by_id["topic_day"].days == ("mon", "tue", "wed", "thu", "fri", "sat")
    assert by_id["topic_day"].topics_by_weekday["fri"] == (
        "История, культура, «в этот день».",
    )
    # every working day of the grid carries a non-empty topic (plan section
    # 2); Sunday stays topic-free - the quiet day
    assert set(by_id["topic_day"].topics_by_weekday) == {
        "mon", "tue", "wed", "thu", "fri", "sat",
    }
    for topics in by_id["topic_day"].topics_by_weekday.values():
        assert topics and all(topic.strip() for topic in topics)
    # both daytime rubrics are off on Sunday
    assert by_id["second_post"].days == ("mon", "tue", "wed", "thu", "fri", "sat")
    assert by_id["second_post"].source is not None
    assert by_id["second_post"].source.get("rotation") is True
    assert by_id["good_news"].kind == "digest"
    assert by_id["good_news"].grace_minutes == 20
    assert by_id["good_news"].on_llm_failure == "bank"
    assert by_id["good_night"].time == "21:30"
    assert by_id["good_night"].grace_minutes == 60
    assert by_id["good_night"].prompt_overlay is not None
    # the shakedown stub: bank kind, no LLM, no jitter, a quiet slot time;
    # ships disabled - an enabled 7th slot would exceed daily_cap and the
    # cap would cut the night slot on working days (plan section 2)
    assert by_id["smoke_test"].kind == "bank"
    assert by_id["smoke_test"].time == "10:07"
    assert by_id["smoke_test"].enabled is False
    assert by_id["smoke_test"].jitter_minutes == 0
    assert by_id["smoke_test"].source is not None
    assert by_id["smoke_test"].source.get("bank_types") == ["smoke", "fact"]
    for rubric in config.rubrics:
        if rubric.id != "smoke_test":
            assert rubric.enabled is True
        assert rubric.target == "main"
        assert 0.0 < rubric.similarity_threshold <= 1.0
        assert rubric.min_chars < rubric.max_chars

    # 7 weather points (plan section 4):spb first, then the six region towns
    names = [point.name for point in config.weather_points]
    assert names[0] == "Санкт-Петербург"
    assert names[1:] == [
        "Гатчина",
        "Выборг",
        "Тихвин",
        "Луга",
        "Кингисепп",
        "Кировск (Лен. обл.)",
    ]


def test_shipped_enabled_grid_fits_daily_cap(rubrics_env: None) -> None:
    # plan section 2: the enabled grid is at most 6 slots a day and
    # daily_cap is 6, so the cap is only a manual-run safety net and never
    # cuts a scheduled slot (the last one of the day, good_night 21:30,
    # would be the victim otherwise - slots run strictly in time order)
    config = load_rubrics_config()

    per_day = {
        day: [r.id for r in config.rubrics if r.enabled and day in r.days]
        for day in WEEKDAYS
    }
    for day, slots in per_day.items():
        assert len(slots) <= config.daily_cap, day

    # mon-sat carry the full six-rubric grid, sunday is the quiet day
    assert per_day["mon"] == [
        "good_morning",
        "weather",
        "topic_day",
        "second_post",
        "good_news",
        "good_night",
    ]
    assert per_day["sun"] == ["good_morning", "weather", "good_news", "good_night"]


def test_minimal_config_defaults(rubrics_env: None, tmp_path: Path) -> None:
    config = load_rubrics_config(_write_config(tmp_path, MINIMAL_YAML))

    rubric = config.rubrics[0]
    assert rubric.id == "stub"
    assert rubric.enabled is True  # rubrics opt out explicitly
    assert rubric.days == ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    assert rubric.grace_minutes == 30
    assert rubric.jitter_minutes == 0
    assert rubric.kind == "bank"
    # no targets section: "main" falls back to the legacy MAX_CHANNEL_ID env
    assert rubric.target == "main"
    assert config.targets["main"].channel_id == -1001234567890
    assert rubric.source is None
    assert rubric.topics_by_weekday == {}
    assert rubric.prompt_overlay is None
    assert rubric.min_chars == 200
    assert rubric.max_chars == 1800
    assert rubric.image is None
    assert rubric.history_n == 5
    assert rubric.similarity_threshold == 0.7
    assert rubric.validators == ()
    assert rubric.on_llm_failure == "bank"


def test_config_path_from_env(rubrics_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_config(tmp_path, MINIMAL_YAML)
    monkeypatch.setenv("RUBRICS_CONFIG_PATH", str(path))

    config = load_rubrics_config()

    assert config.rubrics[0].id == "stub"


def test_missing_file_fails(rubrics_env: None, tmp_path: Path) -> None:
    # the contour is the only publishing one: its config file is mandatory
    with pytest.raises(RubricsConfigError, match="rubrics config not found"):
        load_rubrics_config(tmp_path / "absent.yaml")


# --- typos must fail startup --------------------------------------------------


def test_unknown_top_level_key_fails(rubrics_env: None, tmp_path: Path) -> None:
    with pytest.raises(RubricsConfigError, match=r"unknown keys \['timeznoe'\]"):
        _load_with(tmp_path, MINIMAL_YAML + "\ntimeznoe: Europe/Moscow\n")


def test_unknown_rubric_key_fails(rubrics_env: None, tmp_path: Path) -> None:
    content = MINIMAL_YAML.replace(
        '    time: "09:00"\n', '    time: "09:00"\n    timez: "09:30"\n'
    )

    with pytest.raises(
        RubricsConfigError, match=r"rubrics\[stub\]: unknown keys \['timez'\]"
    ):
        _load_with(tmp_path, content)


def test_unknown_kind_fails(rubrics_env: None, tmp_path: Path) -> None:
    content = MINIMAL_YAML.replace("kind: bank", "kind: generativ")

    with pytest.raises(RubricsConfigError, match="unknown kind 'generativ'"):
        _load_with(tmp_path, content)


@pytest.mark.parametrize("bad_time", ["9:00", "24:00", "07:0", "07-00", "07:60"])
def test_invalid_time_fails(rubrics_env: None, tmp_path: Path, bad_time: str) -> None:
    content = MINIMAL_YAML.replace('time: "09:00"', f'time: "{bad_time}"')

    with pytest.raises(RubricsConfigError, match="expected HH:MM"):
        _load_with(tmp_path, content)


def test_unknown_weekday_fails(rubrics_env: None, tmp_path: Path) -> None:
    content = MINIMAL_YAML.replace("kind: bank", "kind: bank\n    days: [mon, вс]")

    with pytest.raises(RubricsConfigError, match=r"unknown weekday 'вс'"):
        _load_with(tmp_path, content)


def test_unknown_weekday_in_topics_fails(
    rubrics_env: None, tmp_path: Path
) -> None:
    content = MINIMAL_YAML.replace(
        "kind: bank", "kind: generative\n    topics_by_weekday:\n      mond: тема"
    )

    with pytest.raises(RubricsConfigError, match=r"unknown weekday 'mond'"):
        _load_with(tmp_path, content)


def test_unknown_target_fails(rubrics_env: None, tmp_path: Path) -> None:
    content = MINIMAL_YAML.replace("kind: bank", "kind: bank\n    target: chanel")

    with pytest.raises(RubricsConfigError, match=r"unknown target 'chanel'"):
        _load_with(tmp_path, content)


def test_duplicate_rubric_ids_fail(rubrics_env: None, tmp_path: Path) -> None:
    content = MINIMAL_YAML + """
  - id: stub
    time: "10:00"
    kind: bank
"""

    with pytest.raises(RubricsConfigError, match=r"duplicate ids \['stub'\]"):
        _load_with(tmp_path, content)


def test_invalid_timezone_fails(rubrics_env: None, tmp_path: Path) -> None:
    with pytest.raises(RubricsConfigError, match="unknown timezone"):
        _load_with(tmp_path, "timezone: Europe/Moscoww\n" + MINIMAL_YAML)


def test_invalid_on_llm_failure_fails(rubrics_env: None, tmp_path: Path) -> None:
    content = MINIMAL_YAML.replace(
        "kind: bank", "kind: bank\n    on_llm_failure: reserv"
    )

    with pytest.raises(RubricsConfigError, match=r"unknown policy 'reserv'"):
        _load_with(tmp_path, content)


def test_min_chars_above_max_chars_fails(
    rubrics_env: None, tmp_path: Path
) -> None:
    content = MINIMAL_YAML.replace(
        "kind: bank", "kind: bank\n    min_chars: 2000\n    max_chars: 1500"
    )

    with pytest.raises(RubricsConfigError, match="min_chars"):
        _load_with(tmp_path, content)


def test_max_chars_above_platform_limit_fails(
    rubrics_env: None, tmp_path: Path
) -> None:
    content = MINIMAL_YAML.replace(
        "kind: bank", "kind: bank\n    max_chars: 5000"
    )

    with pytest.raises(RubricsConfigError, match="4000"):
        _load_with(tmp_path, content)


def test_boolean_channel_id_fails(rubrics_env: None, tmp_path: Path) -> None:
    content = "targets:\n  main:\n    channel_id: true\n" + MINIMAL_YAML

    with pytest.raises(RubricsConfigError, match=r"channel_id: expected an integer"):
        _load_with(tmp_path, content)


def test_weather_rubric_without_points_fails(
    rubrics_env: None, tmp_path: Path
) -> None:
    content = MINIMAL_YAML.replace("kind: bank", "kind: weather")

    with pytest.raises(RubricsConfigError, match="weather: points are required"):
        _load_with(tmp_path, content)


def test_channel_id_env_resolution(rubrics_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_TEST_CHANNEL_ID", "-1009999999999")
    content = (
        "targets:\n"
        "  main:\n    channel_id: -1001234567890\n"
        "  test:\n    channel_id_env: MAX_TEST_CHANNEL_ID\n"
        + MINIMAL_YAML
    )

    config = load_rubrics_config(_write_config(tmp_path, content))

    assert config.targets["main"].channel_id == -1001234567890
    assert config.targets["test"].channel_id == -1009999999999


def test_channel_id_env_unset_fails(rubrics_env: None, tmp_path: Path) -> None:
    content = (
        "targets:\n  main:\n    channel_id_env: MAX_ABSENT_CHANNEL_ID\n"
        + MINIMAL_YAML
    )

    with pytest.raises(
        RubricsConfigError, match="MAX_ABSENT_CHANNEL_ID is not set"
    ):
        _load_with(tmp_path, content)


def test_broken_yaml_fails(rubrics_env: None, tmp_path: Path) -> None:
    with pytest.raises(RubricsConfigError, match="invalid yaml"):
        _load_with(tmp_path, "rubrics: [unclosed")


# --- summary_time (daily summary slot, plan section 11) -------------------------


def test_summary_time_defaults_to_2240(rubrics_env: None, tmp_path: Path) -> None:
    config = load_rubrics_config(_write_config(tmp_path, MINIMAL_YAML))
    assert config.summary_time == "22:40"


def test_summary_time_custom_value(rubrics_env: None, tmp_path: Path) -> None:
    content = 'summary_time: "23:05"\n' + MINIMAL_YAML
    config = load_rubrics_config(_write_config(tmp_path, content))
    assert config.summary_time == "23:05"


def test_summary_time_invalid_rejected(rubrics_env: None, tmp_path: Path) -> None:
    with pytest.raises(RubricsConfigError, match="summary_time"):
        _load_with(tmp_path, 'summary_time: "9:99"\n' + MINIMAL_YAML)


# --- image_generation section (dual-channel plan section 3.2) -------------------

# A rubric switched to generated images; the reserved word rides on a bank
# rubric on purpose: bank type names live in the DB, not in the config.
GENERATE_YAML = MINIMAL_YAML.replace(
    "kind: bank", "kind: bank\n    image: generate"
)


def test_image_generation_defaults_without_section(
    rubrics_env: None, tmp_path: Path
) -> None:
    # the section is optional: without it the defaults apply (and only a
    # rubric with image: generate makes them matter)
    config = load_rubrics_config(_write_config(tmp_path, MINIMAL_YAML))

    assert config.image_generation.fal.model == "fal-ai/flux/dev"
    assert config.image_generation.yandex_art.size == "1024x1024"
    assert config.image_generation.timeout_seconds == 120


def test_image_generation_section_parsed(
    rubrics_env: None, tmp_path: Path
) -> None:
    # the top-level key is whitelisted (an unknown top key still fails) and
    # every sub-section carries its own default
    content = (
        "image_generation:\n"
        "  fal:\n"
        '    model: fal-ai/flux/schnell\n'
        "  yandex_art:\n"
        '    size: "512x512"\n'
        "  timeout_seconds: 60\n"
    ) + MINIMAL_YAML

    config = load_rubrics_config(_write_config(tmp_path, content))

    assert config.image_generation.fal.model == "fal-ai/flux/schnell"
    assert config.image_generation.yandex_art.size == "512x512"
    assert config.image_generation.timeout_seconds == 60


@pytest.mark.parametrize(
    "section",
    [
        "image_generation:\n  fal:\n    modle: x\n",  # typo inside fal
        "image_generation:\n  yandex_art:\n    dimensions: x\n",  # typo inside stub
        "image_generation:\n  ollama: {}\n",  # unknown provider
        "image_generation: 120\n",  # not a mapping
        "image_generation:\n  timeout_seconds: nope\n",  # not an integer
        "image_generation:\n  timeout_seconds: 0\n",  # not positive
    ],
)
def test_image_generation_malformed_fails(
    rubrics_env: None, tmp_path: Path, section: str
) -> None:
    with pytest.raises(RubricsConfigError):
        _load_with(tmp_path, section + MINIMAL_YAML)


def test_image_generate_rubric_valid(
    rubrics_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAL_KEY", "test-fal-key")

    config = load_rubrics_config(_write_config(tmp_path, GENERATE_YAML))

    assert config.rubrics[0].image == "generate"


def test_generate_image_without_fal_key_fails(
    rubrics_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fail-fast (plan section 3.2): the missing key aborts the launch, it
    # does not degrade the slot to a text-only post silently
    monkeypatch.delenv("FAL_KEY", raising=False)

    with pytest.raises(RubricsConfigError, match="FAL_KEY"):
        _load_with(tmp_path, GENERATE_YAML)


def test_generate_image_disabled_rubric_needs_no_key(
    rubrics_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a disabled rubric never runs, so it does not bind the deploy env;
    # a plain bank stub stays enabled to satisfy the contour requirement
    monkeypatch.delenv("FAL_KEY", raising=False)
    content = GENERATE_YAML.replace(
        "kind: bank", "enabled: false\n    kind: bank"
    ) + '''
  - id: stub_enabled
    time: "10:00"
    kind: bank
'''

    config = load_rubrics_config(_write_config(tmp_path, content))

    by_id = {rubric.id: rubric for rubric in config.rubrics}
    assert by_id["stub"].enabled is False
    assert by_id["stub_enabled"].enabled is True


def test_yandex_art_section_alone_needs_no_key(
    rubrics_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the stub is parsed and validated but never activates a provider and
    # requires no key until the real client lands
    monkeypatch.delenv("FAL_KEY", raising=False)
    content = (
        'image_generation:\n  yandex_art:\n    size: "512x512"\n' + MINIMAL_YAML
    )

    config = load_rubrics_config(_write_config(tmp_path, content))

    assert config.image_generation.yandex_art.size == "512x512"


def test_generate_with_yandex_art_only_still_requires_fal_key(
    rubrics_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the reserved yandex_art section cannot cover the requirement: fal is
    # the only active provider (priority is fixed fal -> yandex_art)
    monkeypatch.delenv("FAL_KEY", raising=False)
    content = (
        'image_generation:\n  yandex_art:\n    size: "512x512"\n' + GENERATE_YAML
    )

    with pytest.raises(RubricsConfigError, match="FAL_KEY"):
        _load_with(tmp_path, content)


def test_bank_image_rubrics_need_no_section_or_key(
    rubrics_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # postcard bank rubrics and no-image rubrics stay key-free
    monkeypatch.delenv("FAL_KEY", raising=False)
    content = MINIMAL_YAML.replace("kind: bank", "kind: bank\n    image: postcard")

    config = load_rubrics_config(_write_config(tmp_path, content))

    assert config.rubrics[0].image == "postcard"


# --- startup integration via load_config --------------------------------------


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_BOT_TOKEN", "123456789:test-token")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "111,222")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    # dual-channel fail-fast: LLM rubrics require both the primary
    # OpenRouter key and the Yandex fallback key
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.delenv("RUBRICS_ENABLED", raising=False)
    monkeypatch.delenv("RUBRICS_CONFIG_PATH", raising=False)
    monkeypatch.delenv("RUBRICS_DRY_RUN", raising=False)
    monkeypatch.delenv("NEWS_SOURCES_PATH", raising=False)


def test_load_config_reads_shipped_rubrics(app_env: None) -> None:
    config = load_config()

    assert len(config.rubrics.rubrics) == 7  # grid of 6 + the smoke stub
    assert config.rubrics.llm.primary.api_key == "test-openrouter-key"


def test_load_config_rubrics_typo_fails(
    app_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the broken yaml aborts the launch: a typo must fail the restart,
    # not the future 07:00 post (plan section 8)
    monkeypatch.setenv(
        "RUBRICS_CONFIG_PATH", str(_write_config(tmp_path, MINIMAL_YAML + "\nimeg_style: x\n"))
    )

    with pytest.raises(RubricsConfigError, match=r"unknown keys \['imeg_style'\]"):
        load_config()


def test_load_config_rubrics_without_llm_key_fails(
    app_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    # the shipped grid has generative/digest rubrics -> LLM key is required
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        load_config()


def test_load_config_rubrics_without_enabled_rubrics_fails(
    app_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "RUBRICS_CONFIG_PATH",
        str(_write_config(tmp_path, MINIMAL_YAML.replace("kind: bank", "kind: bank\n    enabled: false"))),
    )

    with pytest.raises(ValueError, match="at least one enabled rubric"):
        load_config()


def test_load_config_generate_image_without_fal_key_fails(
    app_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # startup integration (dual-channel plan section 3.2): an enabled
    # image: generate rubric aborts load_config without FAL_KEY
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv(
        "RUBRICS_CONFIG_PATH", str(_write_config(tmp_path, GENERATE_YAML))
    )

    # RubricsConfigError is a ValueError subclass: same startup contract as
    # the LLM-key fail-fast above
    with pytest.raises(ValueError, match="FAL_KEY"):
        load_config()


# --- contour dry-run (RUBRICS_DRY_RUN, plan section 14 stage 1) ------------------


def test_dry_run_env_flips_publishing_on(
    app_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rollout switch on the VPS: RUBRICS_DRY_RUN=false turns on channel
    # publishing without touching rubrics.yaml in the repo
    monkeypatch.setenv("RUBRICS_DRY_RUN", "false")

    config = load_config()

    assert config.rubrics.dry_run is False


def test_rubrics_dry_run_without_admin_ids_fails(
    app_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # previews are delivered to admins: without them every dry-run post
    # would fail, so fail fast at startup instead
    monkeypatch.delenv("ADMIN_IDS", raising=False)

    with pytest.raises(ValueError, match="ADMIN_IDS"):
        load_config()


def test_rubrics_dry_run_off_survives_without_admin_ids(
    app_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUBRICS_DRY_RUN", "false")
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    # bank/weather only: no LLM key requirement either
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv(
        "RUBRICS_CONFIG_PATH",
        str(_write_config(tmp_path, "weather:\n  points:\n    - name: СПб\n      latitude: 59.94\n      longitude: 30.31\n" + MINIMAL_YAML.replace("kind: bank", "kind: weather"))),
    )

    config = load_config()

    assert config.rubrics.dry_run is False
