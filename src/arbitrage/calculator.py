"""Core arbitrage calculations."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import structlog

from src.arbitrage.fees import FeeCalculator
from src.collectors.base import MarketData

logger = structlog.get_logger()


@dataclass
class ArbitrageResult:
    """Result of an arbitrage calculation."""

    # Markets
    market_a: MarketData
    market_b: MarketData

    # Positions
    side_a: str  # "yes" or "no"
    side_b: str
    price_a: Decimal
    price_b: Decimal

    # Calculations
    gross_spread: Decimal
    gross_profit: Decimal
    total_fees: Decimal
    net_profit: Decimal
    net_profit_pct: Decimal

    # Metadata
    is_profitable: bool
    position_size: Decimal
    notes: Optional[str] = None

    # New fields for filtering
    priority: str = "MEDIUM"  # HIGH, MEDIUM, LOW based on resolution time
    days_to_resolution: Optional[int] = None
    liquidity_a: Optional[Decimal] = None
    liquidity_b: Optional[Decimal] = None
    filtered_reason: Optional[str] = None  # If filtered out, why

    def __repr__(self) -> str:
        return (
            f"<ArbitrageResult {self.market_a.platform}/{self.market_b.platform} "
            f"net={self.net_profit_pct:.2f}% profitable={self.is_profitable}>"
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "platform_a": self.market_a.platform,
            "platform_b": self.market_b.platform,
            "market_a_id": self.market_a.platform_market_id,
            "market_b_id": self.market_b.platform_market_id,
            "market_a_title": self.market_a.title,
            "market_b_title": self.market_b.title,
            "side_a": self.side_a,
            "side_b": self.side_b,
            "price_a": float(self.price_a),
            "price_b": float(self.price_b),
            "gross_spread": float(self.gross_spread),
            "gross_profit": float(self.gross_profit),
            "total_fees": float(self.total_fees),
            "net_profit": float(self.net_profit),
            "net_profit_pct": float(self.net_profit_pct),
            "is_profitable": self.is_profitable,
            "position_size": float(self.position_size),
            "url_a": self.market_a.url,
            "url_b": self.market_b.url,
            "priority": self.priority,
            "days_to_resolution": self.days_to_resolution,
            "liquidity_a": float(self.liquidity_a) if self.liquidity_a else None,
            "liquidity_b": float(self.liquidity_b) if self.liquidity_b else None,
        }


class ArbitrageCalculator:
    """Calculate arbitrage opportunities between markets."""

    def __init__(
        self,
        min_net_spread_pct: float = 1.0,
        default_position_size: float = 100.0,
        max_days_to_resolution: int = 90,
        min_liquidity: float = 100.0,
        require_liquidity_data: bool = False,
    ):
        """Initialize calculator.

        Args:
            min_net_spread_pct: Minimum net profit percentage to consider profitable.
            default_position_size: Default position size per side in dollars.
            max_days_to_resolution: Maximum days until resolution (longer = capital locked).
            min_liquidity: Minimum liquidity in dollars to consider.
            require_liquidity_data: If True, skip markets without liquidity data.
        """
        self.min_net_spread_pct = Decimal(str(min_net_spread_pct))
        self.default_position_size = Decimal(str(default_position_size))
        self.max_days_to_resolution = max_days_to_resolution
        self.min_liquidity = Decimal(str(min_liquidity))
        self.require_liquidity_data = require_liquidity_data
        self.fee_calculator = FeeCalculator()
        self.logger = logger.bind(component="ArbitrageCalculator")

    def _check_resolution_date(self, market: MarketData) -> tuple[bool, Optional[str]]:
        """Check if market resolves within acceptable timeframe.

        Returns:
            (passes_filter, reason_if_failed)
        """
        days = market.days_to_resolution
        if days is None:
            return True, None  # No date info, allow
        if days > self.max_days_to_resolution:
            return False, f"Resolves in {days} days (max {self.max_days_to_resolution})"
        return True, None

    def _check_liquidity(self, market: MarketData) -> tuple[bool, Optional[str]]:
        """Check if market has sufficient liquidity.

        Returns:
            (passes_filter, reason_if_failed)
        """
        if not market.has_sufficient_liquidity:
            return False, "Insufficient liquidity"
        if self.require_liquidity_data:
            if market.volume_24h is None and market.liquidity is None:
                return False, "No liquidity data available"
        return True, None

    def _calculate_priority(self, market_a: MarketData, market_b: MarketData) -> str:
        """Calculate opportunity priority based on resolution time."""
        days_a = market_a.days_to_resolution
        days_b = market_b.days_to_resolution

        # Use shorter resolution time
        days = min(d for d in [days_a, days_b] if d is not None) if any([days_a, days_b]) else None

        if days is None:
            return "MEDIUM"
        if days <= 7:
            return "HIGH"  # Quick turnaround
        if days <= 30:
            return "MEDIUM"
        return "LOW"  # Capital locked too long

    def calculate_cross_platform(
        self,
        market_a: MarketData,
        market_b: MarketData,
        position_size: Optional[Decimal] = None,
    ) -> Optional[ArbitrageResult]:
        """Calculate arbitrage opportunity between two markets for the same event.

        For the same event on different platforms:
        - If YES price on A + NO price on B < 1: Arbitrage exists
        - Buy YES on cheaper platform, buy NO on other platform

        Args:
            market_a: First market.
            market_b: Second market.
            position_size: Position size per side (defaults to default_position_size).

        Returns:
            ArbitrageResult if opportunity exists, None otherwise.
        """
        if position_size is None:
            position_size = self.default_position_size

        # Need prices from both markets
        if market_a.yes_price is None or market_b.yes_price is None:
            return None

        # Check resolution date filter
        res_ok_a, res_reason_a = self._check_resolution_date(market_a)
        res_ok_b, res_reason_b = self._check_resolution_date(market_b)
        if not res_ok_a or not res_ok_b:
            reason = res_reason_a or res_reason_b
            self.logger.debug("Filtered by resolution date", reason=reason)
            return None

        # Check liquidity filter
        liq_ok_a, liq_reason_a = self._check_liquidity(market_a)
        liq_ok_b, liq_reason_b = self._check_liquidity(market_b)
        if not liq_ok_a or not liq_ok_b:
            reason = liq_reason_a or liq_reason_b
            self.logger.debug("Filtered by liquidity", reason=reason)
            return None

        # Calculate both possible arbitrage directions

        # Direction 1: Buy YES on A, Buy NO on B
        cost_1 = market_a.yes_price + (Decimal("1") - market_b.yes_price)

        # Direction 2: Buy NO on A, Buy YES on B
        cost_2 = (Decimal("1") - market_a.yes_price) + market_b.yes_price

        # Check if either direction is profitable (cost < 1)
        if cost_1 >= Decimal("1") and cost_2 >= Decimal("1"):
            return None

        # Choose the better direction
        if cost_1 < cost_2:
            side_a = "yes"
            side_b = "no"
            price_a = market_a.yes_price
            price_b = Decimal("1") - market_b.yes_price
            total_cost = cost_1
        else:
            side_a = "no"
            side_b = "yes"
            price_a = Decimal("1") - market_a.yes_price
            price_b = market_b.yes_price
            total_cost = cost_2

        # Calculate profit
        gross_spread = Decimal("1") - total_cost
        gross_profit = position_size * gross_spread

        # Calculate fees
        fee_result = self.fee_calculator.calculate_arbitrage_fees(
            platform_a=market_a.platform,
            platform_b=market_b.platform,
            position_size=position_size,
            gross_profit=gross_profit,
        )

        net_profit = fee_result["net_profit"]
        net_profit_pct = fee_result["net_profit_pct"]

        is_profitable = net_profit_pct >= self.min_net_spread_pct

        # Calculate priority and days to resolution
        priority = self._calculate_priority(market_a, market_b)
        days_a = market_a.days_to_resolution
        days_b = market_b.days_to_resolution
        days_to_resolution = min(d for d in [days_a, days_b] if d is not None) if any([days_a, days_b]) else None

        return ArbitrageResult(
            market_a=market_a,
            market_b=market_b,
            side_a=side_a,
            side_b=side_b,
            price_a=price_a,
            price_b=price_b,
            gross_spread=gross_spread,
            gross_profit=gross_profit,
            total_fees=fee_result["total_fees"],
            net_profit=net_profit,
            net_profit_pct=Decimal(str(net_profit_pct)),
            is_profitable=is_profitable,
            position_size=position_size,
            priority=priority,
            days_to_resolution=days_to_resolution,
            liquidity_a=market_a.volume_24h or market_a.liquidity,
            liquidity_b=market_b.volume_24h or market_b.liquidity,
        )

    def calculate_same_platform_logical(
        self,
        markets: list[MarketData],
        relationship: str = "mutually_exclusive",
    ) -> list[ArbitrageResult]:
        """Calculate logical arbitrage within a single platform.

        For mutually exclusive outcomes (e.g., "Trump wins" + "Biden wins" + "Other wins"),
        the sum of YES prices should equal 1.0. If it doesn't, arbitrage exists.

        Args:
            markets: List of related markets.
            relationship: Type of relationship ("mutually_exclusive", "temporal").

        Returns:
            List of ArbitrageResult if opportunities exist.
        """
        if relationship != "mutually_exclusive":
            self.logger.warning(
                "Only mutually_exclusive relationship currently supported",
                relationship=relationship,
            )
            return []

        # For mutually exclusive: sum of YES prices should = 1
        total_yes = sum(
            m.yes_price for m in markets if m.yes_price is not None
        )

        if total_yes is None or total_yes == Decimal("1"):
            return []

        # If sum > 1: Sell all (but can't easily do this on most platforms)
        # If sum < 1: Buy all (guaranteed profit)

        if total_yes < Decimal("1"):
            # Profit opportunity: buy YES on all
            gross_spread = Decimal("1") - total_yes
            self.logger.info(
                "Logical arbitrage detected",
                platform=markets[0].platform if markets else "unknown",
                total_yes=float(total_yes),
                spread=float(gross_spread),
            )
            # TODO: Implement full calculation
            # For now, just log the opportunity

        return []

    def calculate_temporal_arbitrage(
        self,
        earlier_market: MarketData,
        later_market: MarketData,
    ) -> Optional[ArbitrageResult]:
        """Calculate arbitrage based on temporal relationships.

        For example: "Event X by March 2026" should always be <= "Event X by June 2026"

        Args:
            earlier_market: Market with earlier deadline.
            later_market: Market with later deadline.

        Returns:
            ArbitrageResult if opportunity exists.
        """
        if earlier_market.yes_price is None or later_market.yes_price is None:
            return None

        # Earlier event YES should be <= Later event YES
        # If earlier > later, that's irrational pricing

        if earlier_market.yes_price > later_market.yes_price:
            spread = earlier_market.yes_price - later_market.yes_price
            self.logger.info(
                "Temporal arbitrage detected",
                earlier_price=float(earlier_market.yes_price),
                later_price=float(later_market.yes_price),
                spread=float(spread),
            )
            # Strategy: Buy NO on earlier, Buy YES on later
            # TODO: Implement full calculation

        return None
