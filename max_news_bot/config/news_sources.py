"""Operator-confirmed RSS feeds for the good-news rubric (plan section 5).

``news_sources.yaml`` lives next to ``sources.yaml`` and carries the list of
federal soft-section feeds confirmed by the operator at Stage 0 (verification
details: docs/plans/2026-10-09-epoch0-research.md, section 3). The loader
validates the same way ``rubrics.yaml`` is validated: a typo must abort the
launch with a readable cause, not surface later as a silently missing feed
of the 19:00 digest.

The path override env mirrors RUBRICS_CONFIG_PATH, so a deploy can swap the
feed list without rebuilding the image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class NewsSourcesConfigError(ValueError):
    """Invalid news_sources.yaml: startup must abort with a readable cause."""


@dataclass(frozen=True)
class NewsSource:
    """One confirmed feed of the good-news rubric."""

    name: str
    url: str
    section: str | None = None  # culture|science|nature|sport (plan section 5)
    enabled: bool = True  # a feed can be parked without deleting it


_SOURCE_KEYS = ("name", "url", "section", "enabled")


def _as_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NewsSourcesConfigError(
            f"{where}: expected a non-empty string, got {value!r}"
        )
    return value.strip()


def _parse_source(item: Any, index: int) -> NewsSource:
    where = f"sources[{index}]"
    if not isinstance(item, Mapping):
        raise NewsSourcesConfigError(f"{where}: expected a mapping")
    unknown = sorted(set(item) - set(_SOURCE_KEYS))
    if unknown:
        raise NewsSourcesConfigError(
            f"{where}: unknown keys {unknown}; allowed keys: {sorted(_SOURCE_KEYS)}"
        )
    for required in ("name", "url"):
        if required not in item:
            raise NewsSourcesConfigError(f"{where}: '{required}' is required")

    name = _as_str(item["name"], f"{where}.name")
    url = _as_str(item["url"], f"{where}.url")
    if not url.startswith(("http://", "https://")):
        raise NewsSourcesConfigError(
            f"{where}.url: expected an http(s) URL, got {url!r}"
        )
    section_raw = item.get("section")
    section = (
        _as_str(section_raw, f"{where}.section")
        if section_raw is not None
        else None
    )
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise NewsSourcesConfigError(
            f"{where}.enabled: expected true/false, got {enabled!r}"
        )
    return NewsSource(name=name, url=url, section=section, enabled=enabled)


def load_news_sources(path: Path | None = None) -> tuple[NewsSource, ...]:
    """Load and validate the confirmed feed list.

    A missing file is an error, not an empty list: the digest rubric names
    this file in its source config (``feeds_file``), so absence means a
    broken deploy.
    """
    env_path = os.getenv("NEWS_SOURCES_PATH", "").strip()
    if path is not None:
        config_path = path
    elif env_path:
        config_path = Path(env_path)
    else:
        config_path = Path(__file__).resolve().parent / "news_sources.yaml"

    if not config_path.exists():
        raise NewsSourcesConfigError(f"news sources config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise NewsSourcesConfigError(f"{config_path}: invalid yaml: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise NewsSourcesConfigError(
            f"{config_path}: expected a mapping at the top level"
        )
    unknown_top = sorted(set(raw) - {"sources"})
    if unknown_top:
        raise NewsSourcesConfigError(
            f"{config_path}: unknown keys {unknown_top}; allowed keys: ['sources']"
        )
    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise NewsSourcesConfigError("sources: expected a non-empty list of feeds")

    sources = tuple(
        _parse_source(item, index) for index, item in enumerate(raw_sources)
    )
    urls = [source.url for source in sources]
    duplicate_urls = sorted({url for url in urls if urls.count(url) > 1})
    if duplicate_urls:
        raise NewsSourcesConfigError(
            f"sources: duplicate urls {duplicate_urls}"
        )
    names = [source.name for source in sources]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise NewsSourcesConfigError(
            f"sources: duplicate names {duplicate_names}"
        )
    return sources
