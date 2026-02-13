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

    # Slippage estimation
    slippage_warning: Optional[str] = None  # Warning if likely slippage
    depth_a: Optional[Decimal] = None  # Contracts available at price_a
    depth_b: Optional[Decimal] = None  # Contracts available at price_b
    effective_net_profit_pct: Optional[Decimal] = None  # Net profit after estimated slippage

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
            "slippage_warning": self.slippage_warning,
            "depth_a": float(self.depth_a) if self.depth_a else None,
            "depth_b": float(self.depth_b) if self.depth_b else None,
            "effective_net_profit_pct": float(self.effective_net_profit_pct) if self.effective_net_profit_pct else None,
        }


class ArbitrageCalculator:
    """Calculate arbitrage opportunities between markets."""

    def __init__(
        self,
        min_net_spread_pct: float = 0.5,
        default_position_size: float = 100.0,
        max_days_to_resolution: int = 180,
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

    def _estimate_slippage(
        self,
        market: MarketData,
        side: str,
        position_size: Decimal,
    ) -> tuple[Optional[Decimal], Optional[str]]:
        """Estimate slippage based on order book depth.

        Args:
            market: Market data with depth info.
            side: "yes" or "no" - which side we're buying.
            position_size: How much we want to trade in dollars.

        Returns:
            (depth_available, warning_message)
        """
        if side == "yes":
            depth = market.yes_ask_size
            price = market.yes_ask or market.yes_price
        else:
            depth = market.no_ask_size
            price = market.no_ask or (Decimal("1") - market.yes_bid if market.yes_bid else None)

        if depth is None or price is None:
            return None, None

        # Convert depth (contracts) to dollars
        depth_dollars = depth * price

        if depth_dollars < position_size:
            shortfall = position_size - depth_dollars
            warning = f"Only ${float(depth_dollars):.0f} at best price (need ${float(position_size):.0f})"
            return depth, warning

        return depth, None

    @staticmethod
    def _buy_yes_price(market: MarketData) -> Optional[Decimal]:
        """Get the price you'd actually pay to buy YES on this market.

        Uses yes_ask (what sellers are asking) when available,
        falls back to yes_price (midpoint).
        """
        if market.yes_ask is not None and market.yes_ask > 0:
            return market.yes_ask
        return market.yes_price

    @staticmethod
    def _buy_no_price(market: MarketData) -> Optional[Decimal]:
        """Get the price you'd actually pay to buy NO on this market.

        Uses no_ask when available, then (1 - yes_bid) since selling YES
        at the bid is equivalent to buying NO, falls back to (1 - yes_price).
        """
        if market.no_ask is not None and market.no_ask > 0:
            return market.no_ask
        if market.yes_bid is not None and market.yes_bid > 0:
            return Decimal("1") - market.yes_bid
        if market.yes_price is not None:
            return Decimal("1") - market.yes_price
        return None

    def calculate_cross_platform(
        self,
        market_a: MarketData,
        market_b: MarketData,
        position_size: Optional[Decimal] = None,
    ) -> Optional[ArbitrageResult]:
        """Calculate arbitrage opportunity between two markets for the same event.

        Uses bid/ask execution prices when available (what you'd actually pay),
        falling back to midpoint prices. This eliminates phantom arbs that
        disappear once you cross the bid/ask spread.

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

        # Get execution prices (what you'd actually pay)
        buy_yes_a = self._buy_yes_price(market_a)
        buy_no_a = self._buy_no_price(market_a)
        buy_yes_b = self._buy_yes_price(market_b)
        buy_no_b = self._buy_no_price(market_b)

        if not all([buy_yes_a, buy_no_a, buy_yes_b, buy_no_b]):
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

        # Calculate both possible arbitrage directions using EXECUTION prices

        # Direction 1: Buy YES on A, Buy NO on B
        cost_1 = buy_yes_a + buy_no_b

        # Direction 2: Buy NO on A, Buy YES on B
        cost_2 = buy_no_a + buy_yes_b

        # Check if either direction is profitable (cost < 1)
        if cost_1 >= Decimal("1") and cost_2 >= Decimal("1"):
            return None

        # Choose the better direction
        if cost_1 < cost_2:
            side_a = "yes"
            side_b = "no"
            price_a = buy_yes_a
            price_b = buy_no_b
            total_cost = cost_1
        else:
            side_a = "no"
            side_b = "yes"
            price_a = buy_no_a
            price_b = buy_yes_b
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

        # Estimate slippage
        depth_a, warning_a = self._estimate_slippage(market_a, side_a, position_size)
        depth_b, warning_b = self._estimate_slippage(market_b, side_b, position_size)

        slippage_warning = None
        if warning_a and warning_b:
            slippage_warning = f"A: {warning_a}; B: {warning_b}"
        elif warning_a:
            slippage_warning = f"A: {warning_a}"
        elif warning_b:
            slippage_warning = f"B: {warning_b}"

        # Estimate effective profit after slippage (rough: -1% per side with warning)
        effective_net_profit_pct = net_profit_pct
        if slippage_warning:
            slippage_penalty = Decimal("1.0") if warning_a else Decimal("0")
            slippage_penalty += Decimal("1.0") if warning_b else Decimal("0")
            effective_net_profit_pct = net_profit_pct - slippage_penalty

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
            slippage_warning=slippage_warning,
            depth_a=depth_a,
            depth_b=depth_b,
            effective_net_profit_pct=effective_net_profit_pct,
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
