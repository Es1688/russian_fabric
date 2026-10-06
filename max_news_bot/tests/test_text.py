from __future__ import annotations

from max_news_bot.utils.text import canonical_url, jaccard, title_tokens


class TestCanonicalUrl:
    def test_strips_utm_params(self) -> None:
        url = "https://example.com/news/42?utm_source=rss&utm_medium=feed&id=7"
        assert canonical_url(url) == "https://example.com/news/42?id=7"

    def test_strips_utm_params_case_insensitive(self) -> None:
        url = "https://example.com/a?UTM_Campaign=launch"
        assert canonical_url(url) == "https://example.com/a"

    def test_strips_fbclid(self) -> None:
        url = "https://example.com/a?fbclid=abc123&x=1"
        assert canonical_url(url) == "https://example.com/a?x=1"

    def test_strips_fragment(self) -> None:
        assert canonical_url("https://example.com/a#comments") == "https://example.com/a"

    def test_strips_trailing_slash(self) -> None:
        assert canonical_url("https://example.com/a/") == "https://example.com/a"

    def test_root_trailing_slash(self) -> None:
        assert canonical_url("https://example.com/") == "https://example.com"

    def test_lowercases_host_and_scheme_only(self) -> None:
        url = "HTTPS://EXAMPLE.COM/News/Article"
        assert canonical_url(url) == "https://example.com/News/Article"

    def test_keeps_query_order_of_non_tracking_params(self) -> None:
        url = "https://example.com/a?b=2&a=1&utm_source=x"
        assert canonical_url(url) == "https://example.com/a?b=2&a=1"

    def test_full_example(self) -> None:
        url = "https://Habr.com/ru/news/1/?utm_source=rss#comments"
        assert canonical_url(url) == "https://habr.com/ru/news/1"

    def test_idempotent(self) -> None:
        url = "https://example.com/a/?utm_source=rss&fbclid=zzz#frag"
        once = canonical_url(url)
        assert canonical_url(once) == once

    def test_empty_url(self) -> None:
        assert canonical_url("") == ""


class TestTitleTokens:
    def test_lowercase_and_no_punctuation(self) -> None:
        assert title_tokens("Python 3.13: Новый Релиз!") == [
            "python",
            "новый",
            "релиз",
        ]

    def test_drops_short_tokens(self) -> None:
        # "go", "is", "3", "22" are all <= 2 chars
        assert title_tokens("Go 3.22 is out") == ["out"]

    def test_no_ascii_punctuation_tokens(self) -> None:
        tokens = title_tokens("CUDA, PyTorch — и нейросети!")
        assert tokens == ["cuda", "pytorch", "нейросети"]

    def test_empty_text(self) -> None:
        assert title_tokens("") == []


class TestJaccard:
    def test_identical_sets(self) -> None:
        assert jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self) -> None:
        assert jaccard({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self) -> None:
        assert jaccard({"a", "b", "c"}, {"a", "b", "d"}) == 0.5

    def test_both_empty(self) -> None:
        assert jaccard(set(), set()) == 0.0

    def test_one_empty(self) -> None:
        assert jaccard({"a"}, set()) == 0.0

    def test_subset(self) -> None:
        assert jaccard({"a", "b"}, {"a", "b", "c"}) == 2 / 3

    def test_near_duplicate_titles_above_threshold(self) -> None:
        a = set(title_tokens("Яндекс выпустил новую модель нейросети"))
        b = set(title_tokens("Яндекс выпустил новую большую модель нейросети"))
        assert jaccard(a, b) >= 0.7

    def test_different_titles_below_threshold(self) -> None:
        a = set(title_tokens("Яндекс выпустил новую модель нейросети"))
        b = set(title_tokens("Роскомнадзор заблокировал популярный мессенджер"))
        assert jaccard(a, b) < 0.7
