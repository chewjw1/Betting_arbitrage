"""Tests for fuzzy market matching."""

import pytest

from src.matching.fuzzy_matcher import MarketMatcher
from src.collectors.base import MarketData


@pytest.fixture
def matcher():
    """Create a matcher with default settings."""
    return MarketMatcher(min_confidence=0.8)


class TestMarketMatcher:
    """Tests for MarketMatcher."""

    def test_exact_match(self, matcher):
        """Test matching identical titles."""
        score = matcher.calculate_similarity(
            "Will it rain tomorrow?",
            "Will it rain tomorrow?",
        )
        assert score >= 0.99

    def test_similar_match(self, matcher):
        """Test matching similar titles."""
        score = matcher.calculate_similarity(
            "Will it rain tomorrow in New York?",
            "Rain in New York tomorrow?",
        )
        assert score >= 0.7

    def test_different_titles(self, matcher):
        """Test non-matching titles."""
        score = matcher.calculate_similarity(
            "Will Bitcoin reach $100k?",
            "Who will win the Super Bowl?",
        )
        assert score < 0.5

    def test_normalize_title(self, matcher):
        """Test title normalization."""
        normalized = matcher.normalize_title("  Will IT Rain?!  ", platform=None)
        assert normalized == "will it rain"

    def test_platform_specific_normalization(self, matcher):
        """Test platform-specific normalization."""
        kalshi_title = matcher.normalize_title(
            "[KXBTC-24DEC31] Bitcoin price prediction",
            platform="kalshi",
        )
        assert "kxbtc" not in kalshi_title.lower()

    def test_extract_keywords(self, matcher):
        """Test keyword extraction."""
        keywords = matcher.extract_keywords("will bitcoin reach 100k dollars")
        assert "bitcoin" in keywords
        assert "reach" in keywords
        assert "dollars" in keywords
        assert "will" not in keywords  # stop word

    def test_find_matches(self, matcher):
        """Test finding matches between market lists."""
        markets_a = [
            MarketData(
                platform="kalshi",
                platform_market_id="K1",
                title="Will Bitcoin reach $100k by end of 2025?",
            ),
            MarketData(
                platform="kalshi",
                platform_market_id="K2",
                title="Will there be a government shutdown in January?",
            ),
        ]

        markets_b = [
            MarketData(
                platform="polymarket",
                platform_market_id="P1",
                title="Bitcoin to $100k in 2025?",
            ),
            MarketData(
                platform="polymarket",
                platform_market_id="P2",
                title="Federal government shutdown January 2025?",
            ),
        ]

        matches = matcher.find_matches(markets_a, markets_b, min_confidence=0.6)

        assert len(matches) >= 1
        # Check that Bitcoin markets matched
        bitcoin_match = next(
            (m for m in matches if "bitcoin" in m[0].title.lower()),
            None,
        )
        assert bitcoin_match is not None

    def test_find_best_match(self, matcher):
        """Test finding the best match for a target."""
        target = MarketData(
            platform="kalshi",
            platform_market_id="K1",
            title="Will the Fed raise interest rates in March?",
        )

        candidates = [
            MarketData(
                platform="polymarket",
                platform_market_id="P1",
                title="Fed rate hike in March 2026?",
            ),
            MarketData(
                platform="polymarket",
                platform_market_id="P2",
                title="Bitcoin price prediction",
            ),
            MarketData(
                platform="polymarket",
                platform_market_id="P3",
                title="March interest rate decision by Federal Reserve?",
            ),
        ]

        result = matcher.find_best_match(target, candidates)

        assert result is not None
        match, confidence = result
        assert "fed" in match.title.lower() or "interest" in match.title.lower()
