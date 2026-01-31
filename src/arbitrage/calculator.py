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
        }


class ArbitrageCalculator:
    """Calculate arbitrage opportunities between markets."""

    def __init__(
        self,
        min_net_spread_pct: float = 1.0,
        default_position_size: float = 100.0,
    ):
        """Initialize calculator.

        Args:
            min_net_spread_pct: Minimum net profit percentage to consider profitable.
            default_position_size: Default position size per side in dollars.
        """
        self.min_net_spread_pct = Decimal(str(min_net_spread_pct))
        self.default_position_size = Decimal(str(default_position_size))
        self.fee_calculator = FeeCalculator()
        self.logger = logger.bind(component="ArbitrageCalculator")

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
