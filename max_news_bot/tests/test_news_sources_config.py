from __future__ import annotations

from pathlib import Path

import pytest

from max_news_bot.config.news_sources import (
    NewsSource,
    NewsSourcesConfigError,
    load_news_sources,
)

SHIPPED_YAML = Path(__file__).resolve().parents[1] / "config" / "news_sources.yaml"

# The operator-confirmed list of Stage 0 (see the file header and
# docs/plans/2026-10-09-epoch0-research.md section 3.2): pinned exactly, so
# an accidental edit of the shipped yaml fails the suite visibly.
SHIPPED_FEEDS = (
    ("Газета.Ru — Культура", "https://www.gazeta.ru/export/rss/culture.xml", "culture"),
    ("Газета.Ru — Наука", "https://www.gazeta.ru/export/rss/science.xml", "science"),
    ("Газета.Ru — Спорт", "https://www.gazeta.ru/export/rss/sport.xml", "sport"),
    ("N+1", "https://nplus1.ru/rss", "science"),
    ("Наука и жизнь", "https://nkj.ru/rss/", "science"),
    ("Вокруг света", "https://www.vokrugsveta.ru/rss-feeds/rss.xml", "nature"),
    ("Sports.ru", "https://www.sports.ru/rss/all_news.xml", "sport"),
)


def _write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "news_sources.yaml"
    path.write_text(content, encoding="utf-8")
    return path


MINIMAL_YAML = """
sources:
  - name: Feed
    url: https://example.com/rss
"""


@pytest.fixture
def news_sources_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEWS_SOURCES_PATH", raising=False)


# --- shipped config ----------------------------------------------------------


def test_shipped_config_loads(news_sources_env: None) -> None:
    sources = load_news_sources()

    assert [(s.name, s.url, s.section) for s in sources] == [
        (name, url, section) for name, url, section in SHIPPED_FEEDS
    ]
    assert all(source.enabled for source in sources)


def test_shipped_config_sections_cover_plan_sections(
    news_sources_env: None,
) -> None:
    # Plan section 5: culture, science, nature, sport victories. The
    # research report adds the reserve margins (science x3, sport x2).
    sections = {source.section for source in load_news_sources()}
    assert sections == {"culture", "science", "nature", "sport"}


# --- path resolution ---------------------------------------------------------


def test_explicit_path_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(MINIMAL_YAML, encoding="utf-8")
    env_file = tmp_path / "env.yaml"
    env_file.write_text(
        "sources:\n  - name: Env\n    url: https://e.ru/r\n", encoding="utf-8"
    )
    monkeypatch.setenv("NEWS_SOURCES_PATH", str(env_file))

    sources = load_news_sources(explicit)

    assert [source.name for source in sources] == ["Feed"]


def test_env_override_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, news_sources_env: None
) -> None:
    env_file = _write_config(tmp_path, MINIMAL_YAML)
    monkeypatch.setenv("NEWS_SOURCES_PATH", str(env_file))

    sources = load_news_sources()

    assert [source.name for source in sources] == ["Feed"]


def test_missing_file_is_an_error(news_sources_env: None) -> None:
    with pytest.raises(NewsSourcesConfigError, match="not found"):
        load_news_sources(Path("/nonexistent/news_sources.yaml"))


# --- parsing and validation ----------------------------------------------------


def test_minimal_config_defaults(news_sources_env: None, tmp_path: Path) -> None:
    sources = load_news_sources(_write_config(tmp_path, MINIMAL_YAML))

    assert sources == (
        NewsSource(name="Feed", url="https://example.com/rss", section=None),
    )


def test_disabled_flag_is_parsed(tmp_path: Path, news_sources_env: None) -> None:
    path = _write_config(
        tmp_path,
        """
        sources:
          - name: Parked
            url: https://example.com/rss
            enabled: false
        """,
    )

    sources = load_news_sources(path)

    assert sources[0].enabled is False


@pytest.mark.parametrize(
    "content",
    [
        "- name: list\n  url: https://e.ru/r\n",  # top-level is a list
        "other: 1\nsources:\n  - name: A\n    url: https://a.ru/r\n",  # unknown key
        "sources: []\n",  # empty list
        "sources:\n  - url: https://a.ru/r\n",  # name missing
        "sources:\n  - name: A\n",  # url missing
        "sources:\n  - name: ''\n    url: https://a.ru/r\n",  # empty name
        # url without a scheme would poison URL dedup with path-only values
        "sources:\n  - name: A\n    url: not-a-url\n",
        # unknown per-source key: a typo must abort, not be ignored
        "sources:\n  - name: A\n    url: https://a.ru/r\n    feed: x\n",
        "sources:\n  - name: A\n    url: https://a.ru/r\n    enabled: yesplease\n",
        # same feed twice: one url must not be deduped twice
        "sources:\n  - name: A\n    url: https://a.ru/r\n"
        "  - name: B\n    url: https://a.ru/r\n",
        # duplicate names break per-source stats and empty-feed reporting
        "sources:\n  - name: A\n    url: https://a.ru/r\n"
        "  - name: A\n    url: https://b.ru/r\n",
        "sources:\n  - name: A\n    url: https://a.ru/r\n    section: ''\n",
        ":\n- broken: [yaml\n",
    ],
)
def test_invalid_configs_are_rejected(
    tmp_path: Path, news_sources_env: None, content: str
) -> None:
    with pytest.raises(NewsSourcesConfigError):
        load_news_sources(_write_config(tmp_path, content))
