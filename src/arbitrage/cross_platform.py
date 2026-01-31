"""Cross-platform arbitrage detection."""

from decimal import Decimal
from typing import Optional

import structlog

from src.arbitrage.calculator import ArbitrageCalculator, ArbitrageResult
from src.collectors.base import MarketData
from src.matching.fuzzy_matcher import MarketMatcher

logger = structlog.get_logger()


class CrossPlatformDetector:
    """Detect arbitrage opportunities across prediction market platforms."""

    def __init__(
        self,
        min_net_spread_pct: float = 1.0,
        min_match_confidence: float = 0.8,
        default_position_size: float = 100.0,
    ):
        """Initialize detector.

        Args:
            min_net_spread_pct: Minimum net profit percentage to alert.
            min_match_confidence: Minimum confidence for automatic market matching.
            default_position_size: Default position size per side.
        """
        self.calculator = ArbitrageCalculator(
            min_net_spread_pct=min_net_spread_pct,
            default_position_size=default_position_size,
        )
        self.matcher = MarketMatcher(min_confidence=min_match_confidence)
        self.min_match_confidence = min_match_confidence
        self.logger = logger.bind(component="CrossPlatformDetector")

    def find_opportunities(
        self,
        markets_by_platform: dict[str, list[MarketData]],
        matched_pairs: Optional[list[tuple[MarketData, MarketData, float]]] = None,
    ) -> list[ArbitrageResult]:
        """Find all arbitrage opportunities across platforms.

        Args:
            markets_by_platform: Dict mapping platform name to list of markets.
            matched_pairs: Optional pre-matched market pairs. If not provided,
                          automatic matching will be attempted.

        Returns:
            List of profitable ArbitrageResult objects.
        """
        opportunities = []

        if matched_pairs is None:
            # Automatic matching
            matched_pairs = self._auto_match_markets(markets_by_platform)

        for market_a, market_b, confidence in matched_pairs:
            # Skip low-confidence matches
            if confidence < self.min_match_confidence:
                continue

            result = self.calculator.calculate_cross_platform(market_a, market_b)

            if result and result.is_profitable:
                result.notes = f"Match confidence: {confidence:.2%}"
                opportunities.append(result)
                self.logger.info(
                    "Arbitrage opportunity found",
                    platform_a=market_a.platform,
                    platform_b=market_b.platform,
                    net_profit_pct=float(result.net_profit_pct),
                    confidence=confidence,
                )

        # Sort by profitability
        opportunities.sort(key=lambda x: x.net_profit_pct, reverse=True)

        self.logger.info(
            "Scan complete",
            total_pairs=len(matched_pairs) if matched_pairs else 0,
            opportunities_found=len(opportunities),
        )

        return opportunities

    def _auto_match_markets(
        self,
        markets_by_platform: dict[str, list[MarketData]],
    ) -> list[tuple[MarketData, MarketData, float]]:
        """Automatically match markets across platforms.

        Args:
            markets_by_platform: Dict mapping platform name to list of markets.

        Returns:
            List of (market_a, market_b, confidence) tuples.
        """
        platforms = list(markets_by_platform.keys())
        matched_pairs = []

        # Compare each platform pair
        for i, platform_a in enumerate(platforms):
            for platform_b in platforms[i + 1:]:
                markets_a = markets_by_platform[platform_a]
                markets_b = markets_by_platform[platform_b]

                # Find matches between these two platforms
                matches = self.matcher.find_matches(markets_a, markets_b)

                for market_a, market_b, confidence in matches:
                    matched_pairs.append((market_a, market_b, confidence))

        self.logger.info(
            "Automatic matching complete",
            total_matches=len(matched_pairs),
            platforms=platforms,
        )

        return matched_pairs

    def check_specific_pair(
        self,
        market_a: MarketData,
        market_b: MarketData,
        position_size: Optional[Decimal] = None,
    ) -> Optional[ArbitrageResult]:
        """Check a specific pair of markets for arbitrage.

        Args:
            market_a: First market.
            market_b: Second market.
            position_size: Optional position size override.

        Returns:
            ArbitrageResult if opportunity exists.
        """
        return self.calculator.calculate_cross_platform(
            market_a=market_a,
            market_b=market_b,
            position_size=position_size,
        )

    def scan_for_matching_markets(
        self,
        reference_market: MarketData,
        candidate_markets: list[MarketData],
        min_confidence: Optional[float] = None,
    ) -> list[tuple[MarketData, float, Optional[ArbitrageResult]]]:
        """Find markets that match a reference market and check for arbitrage.

        Args:
            reference_market: Market to find matches for.
            candidate_markets: List of potential matching markets.
            min_confidence: Minimum match confidence (defaults to instance setting).

        Returns:
            List of (matched_market, confidence, arbitrage_result) tuples.
        """
        if min_confidence is None:
            min_confidence = self.min_match_confidence

        results = []

        for candidate in candidate_markets:
            # Skip same platform
            if candidate.platform == reference_market.platform:
                continue

            confidence = self.matcher.calculate_similarity(
                reference_market.title,
                candidate.title,
            )

            if confidence >= min_confidence:
                arb_result = self.calculator.calculate_cross_platform(
                    reference_market,
                    candidate,
                )
                results.append((candidate, confidence, arb_result))

        # Sort by arbitrage potential, then by match confidence
        results.sort(
            key=lambda x: (
                x[2].net_profit_pct if x[2] and x[2].is_profitable else Decimal("-100"),
                x[1],
            ),
            reverse=True,
        )

        return results
