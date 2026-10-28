"""Configuration of the single publishing contour (plan section 8).

``rubrics.yaml`` carries the LLM channels (``llm``/``llm_fallback``,
moved here from the removed sources.yaml — one contour, one config file),
the global section (timezone, persona, image_style, image_generation,
targets, daily_cap, weather points) plus one block per rubric. Everything
is validated here at startup: a typo must abort the launch, not surface as
a broken 07:00 post.

The rubrics contour is the only publishing contour of the process (the
legacy digest/factory loops were removed together with RUBRICS_ENABLED /
FACTORY_ENABLED switches), so the file is mandatory and must define at
least one enabled rubric. The contour dry-run (previews to admins instead
of channel posts) is the RUBRICS_DRY_RUN env flag, true by default.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

# Short weekday tokens accepted in rubric `days` and `topics_by_weekday`.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Pipelines from plan section 8; an unknown kind is a config typo.
RUBRIC_KINDS = ("generative", "bank", "weather", "digest")
# Ultimate generation failure policy (plan section 7): publish a reserve
# post from the content bank or skip the slot with an alert. Weather never
# uses a reserve post (plan section 4) and ships with on_llm_failure=skip.
ON_LLM_FAILURE_POLICIES = ("bank", "skip")
# Kinds that call the LLM and therefore require LLM_API_KEY at startup.
LLM_KINDS = ("generative", "digest")
# Reserved per-rubric `image` value meaning generated images (dual-channel
# plan section 3): bank type names live in the DB, and the reservation keeps
# a bank type from shadowing the switch.
GENERATE_IMAGE = "generate"
# MAX message hard limit (plan section 4: weather must fit 4000 chars).
MAX_POST_CHARS = 4000
# Daily admin summary slot (plan section 11): a fixed config time after the
# last post of the grid (21:30), quoted in yaml like rubric times.
DEFAULT_SUMMARY_TIME = "22:40"
# Image generation defaults (dual-channel plan section 3.2): fal is the
# only active provider, yandex_art is reserved as a stub.
DEFAULT_FAL_MODEL = "fal-ai/flux/dev"
DEFAULT_YANDEX_ART_SIZE = "1024x1024"
DEFAULT_IMAGE_TIMEOUT_SECONDS = 120

# Channel theme of the rubrics contour: the only in-code copy of the editor
# persona (the working value lives in rubrics.yaml) and of the audience
# phrase embedded in the contour's secondary system prompts.
DEFAULT_PERSONA = (
    "Ты — редактор канала для читателей старшего возраста в Санкт-Петербурге "
    "и Ленинградской области. Говори уважительно и тепло, на «вы», простыми "
    "словами: без сленга, англицизмов и иронии. Регион — контекст читателя: "
    "северо-запад, короткий дачный сезон; не давай «южных» советов."
)
AUDIENCE = "читателей старшего возраста"

_RUBRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# NOTE: rubric times must be quoted in yaml: unquoted "12:30" parses as a
# sexagesimal int (750) in YAML 1.1 and would fail validation.
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_TOP_LEVEL_KEYS = (
    "timezone",
    "llm",
    "llm_fallback",
    "persona",
    "image_style",
    "image_generation",
    "targets",
    "daily_cap",
    "weather",
    "rubrics",
    "summary_time",
)
_TARGET_KEYS = ("channel_id", "channel_id_env")
_WEATHER_KEYS = ("points",)
_WEATHER_POINT_KEYS = ("name", "latitude", "longitude")
_IMAGE_GENERATION_KEYS = ("fal", "yandex_art", "timeout_seconds")
_FAL_IMAGE_KEYS = ("model",)
_YANDEX_ART_IMAGE_KEYS = ("size",)
_RUBRIC_KEYS = (
    "id",
    "enabled",
    "time",
    "days",
    "grace_minutes",
    "jitter_minutes",
    "kind",
    "target",
    "source",
    "topics_by_weekday",
    "prompt_overlay",
    "min_chars",
    "max_chars",
    "image",
    "history_n",
    "similarity_threshold",
    "validators",
    "on_llm_failure",
)


class RubricsConfigError(ValueError):
    """Invalid rubrics.yaml: startup must abort with a readable cause."""


@dataclass(frozen=True)
class WeatherPoint:
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class TargetConfig:
    name: str
    channel_id: int  # MAX chat_id is int64


@dataclass(frozen=True)
class LlmConfig:
    """One OpenAI-compatible LLM channel; the key is env-only, never yaml."""

    base_url: str
    model: str
    timeout: int
    max_tokens: int
    temperature: float
    api_key: str  # env-only secret, never in yaml
    # thinking models otherwise burn the whole max_tokens on reasoning
    disable_reasoning: bool = False


@dataclass(frozen=True)
class LlmChannelsConfig:
    """The per-request failover pair: primary OpenRouter + fallback Yandex."""

    primary: LlmConfig
    # None unless LLM_API_KEY is set; an LLM contour requires both keys
    fallback: LlmConfig | None = None

    def missing_keys(self) -> list[str]:
        """Env names of the absent channel keys (primary and/or fallback)."""
        return [
            name
            for name, value in (
                ("OPENROUTER_API_KEY", self.primary.api_key),
                ("LLM_API_KEY", self.fallback.api_key if self.fallback else ""),
            )
            if not value
        ]


@dataclass(frozen=True)
class FalImageConfig:
    """fal.ai generation settings; the provider is active iff FAL_KEY is set."""

    model: str = DEFAULT_FAL_MODEL


@dataclass(frozen=True)
class YandexArtImageConfig:
    """Reserved second image provider (stub): parsed and validated but never
    activated until the real client lands, so it requires no key yet."""

    size: str = DEFAULT_YANDEX_ART_SIZE


@dataclass(frozen=True)
class ImageGenerationConfig:
    """Provider settings with a fixed priority: fal first, then yandex_art."""

    fal: FalImageConfig
    yandex_art: YandexArtImageConfig
    timeout_seconds: int = DEFAULT_IMAGE_TIMEOUT_SECONDS


# Default for configs that omit image_generation (the section is not needed
# without image: generate rubrics); frozen, so sharing the instance is safe.
DEFAULT_IMAGE_GENERATION = ImageGenerationConfig(
    fal=FalImageConfig(), yandex_art=YandexArtImageConfig()
)


@dataclass(frozen=True)
class RubricConfig:
    id: str
    enabled: bool
    time: str  # "HH:MM" in the configured timezone
    days: tuple[str, ...]
    grace_minutes: int
    jitter_minutes: int
    kind: str
    target: str
    # Opaque kind-specific payload consumed by the pipeline stages
    # (weather section 2+ has no source; digest carries feeds, bank kinds
    # carry bank_types, generative kinds may point at bank topics).
    source: dict[str, Any] | None
    topics_by_weekday: dict[str, tuple[str, ...]]
    prompt_overlay: str | None
    min_chars: int
    max_chars: int
    image: str | None
    history_n: int
    similarity_threshold: float
    validators: tuple[str, ...]
    on_llm_failure: str


@dataclass(frozen=True)
class RubricsConfig:
    timezone: str
    persona: str
    image_style: str
    targets: dict[str, TargetConfig]
    daily_cap: int
    weather_points: tuple[WeatherPoint, ...]
    rubrics: tuple[RubricConfig, ...]
    # LLM channels of the contour (llm/llm_fallback sections); keys are
    # env-only, mandatory when rubrics of LLM kinds are enabled.
    llm: LlmChannelsConfig
    summary_time: str = DEFAULT_SUMMARY_TIME  # "HH:MM" local, plan section 11
    # Image generation providers (dual-channel plan section 3); the default
    # applies when rubrics.yaml omits the section.
    image_generation: ImageGenerationConfig = DEFAULT_IMAGE_GENERATION
    # Contour dry-run (plan section 14, stage 1): slots run the full cycle
    # but posts go to the admins as previews instead of the channel. True
    # by default - the cutover to publishing is a separate deliberate env
    # flip (RUBRICS_DRY_RUN=false) after the shakedown.
    dry_run: bool = True


# Local twin of loader._env_bool: importing it from loader would create a
# cycle (loader imports this module to build AppConfig.rubrics).
def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _parse_llm_channels(raw: Mapping[str, Any]) -> LlmChannelsConfig:
    """llm/llm_fallback sections with env-only keys and overrides.

    Keys: OPENROUTER_API_KEY (primary), LLM_API_KEY (fallback). The fallback
    exists iff its key is configured; an LLM contour still requires both
    (checked against the enabled rubrics after parsing). Overrides without
    editing the file: LLM_BASE_URL, LLM_MODEL, LLM_FALLBACK_BASE_URL,
    LLM_FALLBACK_MODEL. timeout/max_tokens/temperature are shared.
    """
    llm_raw = raw.get("llm", {}) or {}
    fallback_raw = raw.get("llm_fallback", {}) or {}
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    fallback_api_key = os.getenv("LLM_API_KEY", "").strip()

    timeout = int(llm_raw.get("timeout", 60))
    max_tokens = int(llm_raw.get("max_tokens", 800))
    temperature = float(llm_raw.get("temperature", 0.7))

    primary = LlmConfig(
        base_url=_env_str(
            "LLM_BASE_URL",
            str(llm_raw.get("base_url", "https://openrouter.ai/api/v1")),
        ),
        model=_env_str(
            "LLM_MODEL", str(llm_raw.get("model", "qwen/qwen3.8-flash"))
        ),
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        api_key=api_key,
        disable_reasoning=bool(llm_raw.get("disable_reasoning", True)),
    )
    fallback = None
    if fallback_api_key:
        fallback = LlmConfig(
            # Yandex AI Studio OpenAI-compatible endpoint; models are full
            # URIs gpt://<folder_id>/<model>.
            base_url=_env_str(
                "LLM_FALLBACK_BASE_URL",
                str(
                    fallback_raw.get(
                        "base_url", "https://ai.api.cloud.yandex.net/v1"
                    )
                ),
            ),
            model=_env_str(
                "LLM_FALLBACK_MODEL",
                str(fallback_raw.get("model", "gpt://FOLDER_ID/yandexgpt-5-lite")),
            ),
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
            api_key=fallback_api_key,
            disable_reasoning=bool(fallback_raw.get("disable_reasoning", False)),
        )
    return LlmChannelsConfig(primary=primary, fallback=fallback)


def _reject_unknown_keys(
    section: Mapping[str, Any], allowed: tuple[str, ...], where: str
) -> None:
    unknown = sorted(set(section) - set(allowed))
    if unknown:
        raise RubricsConfigError(
            f"{where}: unknown keys {unknown}; allowed keys: {sorted(allowed)}"
        )


def _as_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RubricsConfigError(
            f"{where}: expected a non-empty string, got {value!r}"
        )
    return value.strip()


def _as_int(value: Any, where: str) -> int:
    # bool is an int subclass: true/false must not pass as 1/0
    if isinstance(value, bool) or not isinstance(value, int):
        raise RubricsConfigError(f"{where}: expected an integer, got {value!r}")
    return value


def _as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise RubricsConfigError(
            f"{where}: expected true/false, got {value!r}"
        )
    return value


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RubricsConfigError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _parse_timezone(raw: Any) -> str:
    tz = _as_str(raw, "timezone")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RubricsConfigError(
            f"timezone: unknown timezone {tz!r}"
        ) from exc
    return tz


def _parse_channel_id_env(env_name: str, where: str) -> int:
    raw_value = os.getenv(env_name, "").strip()
    if not raw_value:
        raise RubricsConfigError(
            f"{where}: environment variable {env_name} is not set"
        )
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RubricsConfigError(
            f"{where}: {env_name} must be an integer chat id, got {raw_value!r}"
        ) from exc


def _parse_targets(raw: Any) -> dict[str, TargetConfig]:
    if raw is None:
        # No targets section: the legacy MAX_CHANNEL_ID env becomes "main",
        # so a minimal config keeps working with the deploy's env.
        return {
            "main": TargetConfig(
                name="main",
                channel_id=_parse_channel_id_env("MAX_CHANNEL_ID", "targets.main"),
            )
        }
    if not isinstance(raw, Mapping):
        raise RubricsConfigError("targets: expected a mapping")

    targets: dict[str, TargetConfig] = {}
    for raw_name, entry in raw.items():
        name = _as_str(raw_name, "targets")
        where = f"targets.{name}"
        if not isinstance(entry, Mapping):
            raise RubricsConfigError(f"{where}: expected a mapping")
        _reject_unknown_keys(entry, _TARGET_KEYS, where)
        has_literal = "channel_id" in entry
        has_env = "channel_id_env" in entry
        if has_literal == has_env:
            raise RubricsConfigError(
                f"{where}: exactly one of channel_id or channel_id_env is required"
            )
        if has_literal:
            channel_id = _as_int(entry["channel_id"], f"{where}.channel_id")
        else:
            env_name = _as_str(entry["channel_id_env"], f"{where}.channel_id_env")
            channel_id = _parse_channel_id_env(env_name, where)
        targets[name] = TargetConfig(name=name, channel_id=channel_id)
    return targets


def _parse_weather_points(raw: Any) -> tuple[WeatherPoint, ...]:
    section = raw if raw is not None else {}
    if not isinstance(section, Mapping):
        raise RubricsConfigError("weather: expected a mapping")
    _reject_unknown_keys(section, _WEATHER_KEYS, "weather")
    raw_points = section.get("points") or []
    if not isinstance(raw_points, list):
        raise RubricsConfigError("weather.points: expected a list")

    points: list[WeatherPoint] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_points):
        where = f"weather.points[{index}]"
        if not isinstance(item, Mapping):
            raise RubricsConfigError(f"{where}: expected a mapping")
        _reject_unknown_keys(item, _WEATHER_POINT_KEYS, where)
        name = _as_str(item.get("name"), f"{where}.name")
        latitude = _as_float(item.get("latitude"), f"{where}.latitude")
        longitude = _as_float(item.get("longitude"), f"{where}.longitude")
        if not -90.0 <= latitude <= 90.0:
            raise RubricsConfigError(
                f"{where}.latitude: expected -90..90, got {latitude}"
            )
        if not -180.0 <= longitude <= 180.0:
            raise RubricsConfigError(
                f"{where}.longitude: expected -180..180, got {longitude}"
            )
        if name in seen:
            raise RubricsConfigError(
                f"{where}: duplicate weather point name {name!r}"
            )
        seen.add(name)
        points.append(
            WeatherPoint(name=name, latitude=latitude, longitude=longitude)
        )
    return tuple(points)


def _parse_image_generation(raw: Any) -> ImageGenerationConfig:
    section = raw if raw is not None else {}
    if not isinstance(section, Mapping):
        raise RubricsConfigError("image_generation: expected a mapping")
    _reject_unknown_keys(section, _IMAGE_GENERATION_KEYS, "image_generation")

    fal_raw = section.get("fal")
    fal_section = fal_raw if fal_raw is not None else {}
    if not isinstance(fal_section, Mapping):
        raise RubricsConfigError("image_generation.fal: expected a mapping")
    _reject_unknown_keys(fal_section, _FAL_IMAGE_KEYS, "image_generation.fal")
    fal = FalImageConfig(
        model=_as_str(
            fal_section.get("model", DEFAULT_FAL_MODEL),
            "image_generation.fal.model",
        )
    )

    # Reserved stub (dual-channel plan section 3): parsed and validated, but
    # the provider is never activated and no key is required until the real
    # client lands.
    art_raw = section.get("yandex_art")
    art_section = art_raw if art_raw is not None else {}
    if not isinstance(art_section, Mapping):
        raise RubricsConfigError(
            "image_generation.yandex_art: expected a mapping"
        )
    _reject_unknown_keys(
        art_section, _YANDEX_ART_IMAGE_KEYS, "image_generation.yandex_art"
    )
    yandex_art = YandexArtImageConfig(
        size=_as_str(
            art_section.get("size", DEFAULT_YANDEX_ART_SIZE),
            "image_generation.yandex_art.size",
        )
    )

    timeout_seconds = _as_int(
        section.get("timeout_seconds", DEFAULT_IMAGE_TIMEOUT_SECONDS),
        "image_generation.timeout_seconds",
    )
    if timeout_seconds < 1:
        raise RubricsConfigError(
            "image_generation.timeout_seconds: expected a positive integer, "
            f"got {timeout_seconds}"
        )

    return ImageGenerationConfig(
        fal=fal, yandex_art=yandex_art, timeout_seconds=timeout_seconds
    )


def _parse_days(raw: Any, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise RubricsConfigError(
            f"{where}: expected a non-empty list of weekdays, "
            f"allowed: {list(WEEKDAYS)}"
        )
    days: list[str] = []
    for item in raw:
        day = _as_str(item, where).lower()
        if day not in WEEKDAYS:
            raise RubricsConfigError(
                f"{where}: unknown weekday {day!r}; allowed: {list(WEEKDAYS)}"
            )
        if day in days:
            raise RubricsConfigError(
                f"{where}: duplicate weekday {day!r}"
            )
        days.append(day)
    return tuple(days)


def _parse_topics(raw: Any, where: str) -> dict[str, tuple[str, ...]]:
    section = raw if raw is not None else {}
    if not isinstance(section, Mapping):
        raise RubricsConfigError(f"{where}: expected a mapping")
    topics: dict[str, tuple[str, ...]] = {}
    for raw_day, value in section.items():
        day = _as_str(raw_day, where).lower()
        if day not in WEEKDAYS:
            raise RubricsConfigError(
                f"{where}: unknown weekday {day!r}; allowed: {list(WEEKDAYS)}"
            )
        if isinstance(value, str):
            entries: list[Any] = [value]
        elif isinstance(value, list) and value:
            entries = value
        else:
            raise RubricsConfigError(
                f"{where}.{day}: expected a topic string or a non-empty "
                "list of strings"
            )
        topics[day] = tuple(_as_str(entry, f"{where}.{day}") for entry in entries)
    return topics


def _parse_rubric(
    item: Any, index: int, targets: dict[str, TargetConfig]
) -> RubricConfig:
    where = f"rubrics[{index}]"
    if not isinstance(item, Mapping):
        raise RubricsConfigError(f"{where}: expected a mapping")
    for required in ("id", "time", "kind"):
        if required not in item:
            raise RubricsConfigError(f"{where}: '{required}' is required")

    rubric_id = _as_str(item["id"], f"{where}.id")
    where = f"rubrics[{rubric_id}]"
    _reject_unknown_keys(item, _RUBRIC_KEYS, where)
    if not _RUBRIC_ID_RE.match(rubric_id):
        raise RubricsConfigError(
            f"{where}.id: expected lowercase snake_case (a-z, 0-9, _), "
            f"got {rubric_id!r}"
        )

    time = _as_str(item["time"], f"{where}.time")
    if not _TIME_RE.match(time):
        raise RubricsConfigError(
            f"{where}.time: expected HH:MM with quotes in yaml, got {time!r}"
        )

    kind = _as_str(item["kind"], f"{where}.kind")
    if kind not in RUBRIC_KINDS:
        raise RubricsConfigError(
            f"{where}.kind: unknown kind {kind!r}; allowed: {list(RUBRIC_KINDS)}"
        )

    days = _parse_days(item.get("days", list(WEEKDAYS)), f"{where}.days")

    grace_minutes = _as_int(item.get("grace_minutes", 30), f"{where}.grace_minutes")
    if grace_minutes < 1:
        raise RubricsConfigError(
            f"{where}.grace_minutes: expected a positive integer, got {grace_minutes}"
        )
    jitter_minutes = _as_int(
        item.get("jitter_minutes", 0), f"{where}.jitter_minutes"
    )
    if jitter_minutes < 0:
        raise RubricsConfigError(
            f"{where}.jitter_minutes: expected a non-negative integer, "
            f"got {jitter_minutes}"
        )

    target = _as_str(item.get("target", "main"), f"{where}.target")
    if target not in targets:
        raise RubricsConfigError(
            f"{where}.target: unknown target {target!r}; "
            f"defined: {sorted(targets)}"
        )

    source_raw = item.get("source")
    if source_raw is not None and not isinstance(source_raw, Mapping):
        raise RubricsConfigError(f"{where}.source: expected a mapping")
    source = dict(source_raw) if source_raw else None

    topics = _parse_topics(
        item.get("topics_by_weekday"), f"{where}.topics_by_weekday"
    )

    prompt_overlay_raw = item.get("prompt_overlay")
    prompt_overlay = (
        _as_str(prompt_overlay_raw, f"{where}.prompt_overlay")
        if prompt_overlay_raw is not None
        else None
    )

    min_chars = _as_int(item.get("min_chars", 200), f"{where}.min_chars")
    if min_chars < 1:
        raise RubricsConfigError(
            f"{where}.min_chars: expected a positive integer, got {min_chars}"
        )
    max_chars = _as_int(item.get("max_chars", 1800), f"{where}.max_chars")
    if max_chars > MAX_POST_CHARS:
        raise RubricsConfigError(
            f"{where}.max_chars: MAX allows at most {MAX_POST_CHARS} chars, "
            f"got {max_chars}"
        )
    if min_chars >= max_chars:
        raise RubricsConfigError(
            f"{where}: min_chars ({min_chars}) must be below "
            f"max_chars ({max_chars})"
        )

    image_raw = item.get("image")
    image = _as_str(image_raw, f"{where}.image") if image_raw is not None else None

    history_n = _as_int(item.get("history_n", 5), f"{where}.history_n")
    if history_n < 1:
        raise RubricsConfigError(
            f"{where}.history_n: expected a positive integer, got {history_n}"
        )

    similarity_threshold = _as_float(
        item.get("similarity_threshold", 0.7), f"{where}.similarity_threshold"
    )
    if not 0.0 < similarity_threshold <= 1.0:
        raise RubricsConfigError(
            f"{where}.similarity_threshold: expected 0 < t <= 1, "
            f"got {similarity_threshold}"
        )

    validators_raw = item.get("validators", [])
    if not isinstance(validators_raw, list):
        raise RubricsConfigError(
            f"{where}.validators: expected a list of validator names"
        )
    validators = tuple(
        _as_str(entry, f"{where}.validators") for entry in validators_raw
    )
    if len(set(validators)) != len(validators):
        raise RubricsConfigError(
            f"{where}.validators: duplicate validator names"
        )

    on_llm_failure = _as_str(
        item.get("on_llm_failure", "bank"), f"{where}.on_llm_failure"
    )
    if on_llm_failure not in ON_LLM_FAILURE_POLICIES:
        raise RubricsConfigError(
            f"{where}.on_llm_failure: unknown policy {on_llm_failure!r}; "
            f"allowed: {list(ON_LLM_FAILURE_POLICIES)}"
        )

    return RubricConfig(
        id=rubric_id,
        enabled=_as_bool(item.get("enabled", True), f"{where}.enabled"),
        time=time,
        days=days,
        grace_minutes=grace_minutes,
        jitter_minutes=jitter_minutes,
        kind=kind,
        target=target,
        source=source,
        topics_by_weekday=topics,
        prompt_overlay=prompt_overlay,
        min_chars=min_chars,
        max_chars=max_chars,
        image=image,
        history_n=history_n,
        similarity_threshold=similarity_threshold,
        validators=validators,
        on_llm_failure=on_llm_failure,
    )


def _parse_rubrics_config(
    raw: Any, *, source_path: Path
) -> RubricsConfig:
    if not isinstance(raw, Mapping):
        raise RubricsConfigError(
            f"{source_path}: expected a mapping at the top level"
        )
    _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, str(source_path))

    timezone = _parse_timezone(raw.get("timezone", "Europe/Moscow"))
    llm = _parse_llm_channels(raw)

    persona_raw = raw.get("persona")
    persona = _as_str(persona_raw, "persona") if persona_raw is not None else ""
    image_style_raw = raw.get("image_style")
    image_style = (
        _as_str(image_style_raw, "image_style")
        if image_style_raw is not None
        else ""
    )
    image_generation = _parse_image_generation(raw.get("image_generation"))

    daily_cap = _as_int(raw.get("daily_cap", 6), "daily_cap")
    if daily_cap < 1:
        raise RubricsConfigError(
            f"daily_cap: expected a positive integer, got {daily_cap}"
        )

    targets = _parse_targets(raw.get("targets"))
    weather_points = _parse_weather_points(raw.get("weather"))

    summary_time = _as_str(
        raw.get("summary_time", DEFAULT_SUMMARY_TIME), "summary_time"
    )
    if not _TIME_RE.match(summary_time):
        raise RubricsConfigError(
            f"summary_time: expected HH:MM with quotes in yaml, "
            f"got {summary_time!r}"
        )

    rubrics_raw = raw.get("rubrics") or []
    if not isinstance(rubrics_raw, list):
        raise RubricsConfigError("rubrics: expected a list")
    rubrics = tuple(
        _parse_rubric(item, index, targets)
        for index, item in enumerate(rubrics_raw)
    )
    ids = [rubric.id for rubric in rubrics]
    duplicates = sorted({rubric_id for rubric_id in ids if ids.count(rubric_id) > 1})
    if duplicates:
        raise RubricsConfigError(f"rubrics: duplicate ids {duplicates}")

    # The rubrics contour is the only publishing contour: no enabled rubric
    # means the process would have nothing to publish at all.
    enabled_rubrics = [rubric for rubric in rubrics if rubric.enabled]
    if not enabled_rubrics:
        raise RubricsConfigError(
            "rubrics.yaml must define at least one enabled rubric: the "
            "rubrics contour is the only publishing contour of the process"
        )

    # LLM rubrics need both channel keys at startup: a single channel
    # defeats the declared per-request failover.
    if any(rubric.kind in LLM_KINDS for rubric in enabled_rubrics):
        missing = llm.missing_keys()
        if missing:
            raise RubricsConfigError(
                f"{' and '.join(missing)} required when rubrics of LLM "
                f"kinds ({', '.join(LLM_KINDS)}) are enabled: both LLM "
                "keys must be set (primary OpenRouter, fallback Yandex)"
            )

    # Weather rubrics render the global points; without points the slot
    # could never run (plan section 4).
    if any(r.kind == "weather" and r.enabled for r in rubrics) and not weather_points:
        raise RubricsConfigError(
            "weather: points are required when a weather rubric is enabled"
        )

    # Fail-fast (dual-channel plan section 3.2): an enabled rubric with
    # image: generate needs a working image provider; fal is the only active
    # one (yandex_art is a stub), so a missing FAL_KEY must abort the launch
    # instead of degrading every such post to text-only. Checked while the
    # contour is off too - the yaml is validated on every restart.
    if any(
        rubric.enabled and rubric.image == GENERATE_IMAGE for rubric in rubrics
    ) and not os.getenv("FAL_KEY", "").strip():
        raise RubricsConfigError(
            "image: generate requires the FAL_KEY environment variable: fal "
            "is the only active image provider (yandex_art is a stub)"
        )

    return RubricsConfig(
        timezone=timezone,
        persona=persona,
        image_style=image_style,
        targets=targets,
        daily_cap=daily_cap,
        weather_points=weather_points,
        rubrics=rubrics,
        llm=llm,
        summary_time=summary_time,
        image_generation=image_generation,
        # Deploy-level roll-out switch, same shape as the contour's previews:
        # previews to admins by default, publishing only when turned off.
        dry_run=_env_flag("RUBRICS_DRY_RUN", True),
    )


def load_rubrics_config(path: Path | None = None) -> RubricsConfig:
    """Load and validate rubrics.yaml — the single contour's config file.

    The file is mandatory (the process has no other publishing contour);
    a missing file, a broken yaml or a failed validation aborts the launch.
    """
    env_path = os.getenv("RUBRICS_CONFIG_PATH", "").strip()
    if path is not None:
        config_path = path
    elif env_path:
        config_path = Path(env_path)
    else:
        config_path = Path(__file__).resolve().parent / "rubrics.yaml"

    if not config_path.exists():
        raise RubricsConfigError(f"rubrics config not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RubricsConfigError(f"{config_path}: invalid yaml: {exc}") from exc
    return _parse_rubrics_config(raw, source_path=config_path)
