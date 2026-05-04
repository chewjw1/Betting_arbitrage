"""Real-time cross-platform arbitrage detector."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, Callable, Awaitable
import structlog

from ..feeds.base import PriceUpdate

logger = structlog.get_logger()


@dataclass
class ArbOpportunity:
    """Detected arbitrage opportunity."""
    opportunity_id: str
    event_key: str
    detected_at: datetime

    platform_a: str
    market_id_a: str
    side_a: str  # "yes" or "no"
    price_a: Decimal
    size_a: Optional[Decimal]

    platform_b: str
    market_id_b: str
    side_b: str
    price_b: Decimal
    size_b: Optional[Decimal]

    gross_profit_pct: Decimal
    net_profit_pct: Decimal
    max_stake: Optional[Decimal]

    title: str = ""
    expired: bool = False

    def __str__(self) -> str:
        return (
            f"ARB {self.net_profit_pct:.2f}%: "
            f"{self.platform_a} {self.side_a}@{self.price_a:.3f} vs "
            f"{self.platform_b} {self.side_b}@{self.price_b:.3f}"
        )


ArbCallback = Callable[[ArbOpportunity], Awaitable[None]]


@dataclass
class MarketPair:
    """Linked markets across platforms for the same event."""
    event_key: str
    title: str
    kalshi_market_id: Optional[str] = None
    polymarket_token_id: Optional[str] = None
    kalshi_prices: Optional[PriceUpdate] = None
    polymarket_prices: Optional[PriceUpdate] = None
    last_update: datetime = field(default_factory=datetime.utcnow)


class ArbDetector:
    """Detect cross-platform arbitrage in real-time.

    Monitors linked markets across platforms and identifies when
    buying on one platform and selling on another creates profit.
    """

    def __init__(
        self,
        min_profit_pct: Decimal = Decimal("0.5"),
        fee_rate: Decimal = Decimal("0.02"),
        callback: Optional[ArbCallback] = None,
    ):
        self.min_profit_pct = min_profit_pct
        self.fee_rate = fee_rate
        self.callback = callback

        self._market_pairs: dict[str, MarketPair] = {}
        self._kalshi_to_event: dict[str, str] = {}
        self._poly_to_event: dict[str, str] = {}
        self._active_opps: dict[str, ArbOpportunity] = {}
        self._opp_counter = 0
        self._lock = asyncio.Lock()

    def register_pair(
        self,
        event_key: str,
        title: str,
        kalshi_market_id: Optional[str] = None,
        polymarket_token_id: Optional[str] = None,
    ) -> None:
        """Register a linked market pair."""
        pair = MarketPair(
            event_key=event_key,
            title=title,
            kalshi_market_id=kalshi_market_id,
            polymarket_token_id=polymarket_token_id,
        )
        self._market_pairs[event_key] = pair

        if kalshi_market_id:
            self._kalshi_to_event[kalshi_market_id] = event_key
        if polymarket_token_id:
            self._poly_to_event[polymarket_token_id] = event_key

        logger.debug(
            "registered_market_pair",
            event_key=event_key,
            kalshi=kalshi_market_id,
            polymarket=polymarket_token_id[:16] if polymarket_token_id else None,
        )

    async def on_price_update(self, update: PriceUpdate) -> Optional[ArbOpportunity]:
        """Process a price update and check for arbitrage."""
        async with self._lock:
            event_key = None

            if update.platform == "kalshi":
                event_key = self._kalshi_to_event.get(update.market_id)
            elif update.platform == "polymarket":
                event_key = self._poly_to_event.get(update.market_id)

            if not event_key or event_key not in self._market_pairs:
                return None

            pair = self._market_pairs[event_key]
            pair.last_update = datetime.utcnow()

            if update.platform == "kalshi":
                pair.kalshi_prices = update
            else:
                pair.polymarket_prices = update

            if pair.kalshi_prices and pair.polymarket_prices:
                return await self._check_arbitrage(pair)

            return None

    async def _check_arbitrage(self, pair: MarketPair) -> Optional[ArbOpportunity]:
        """Check for arbitrage between two platforms."""
        k = pair.kalshi_prices
        p = pair.polymarket_prices

        if not k or not p:
            return None

        opportunities = []

        if k.yes_ask and p.yes_bid:
            profit = self._calc_profit(k.yes_ask, p.yes_bid)
            if profit >= self.min_profit_pct:
                opportunities.append(self._create_opportunity(
                    pair=pair,
                    platform_a="kalshi",
                    market_id_a=k.market_id,
                    side_a="yes",
                    price_a=k.yes_ask,
                    size_a=k.yes_ask_size,
                    platform_b="polymarket",
                    market_id_b=p.market_id,
                    side_b="yes",
                    price_b=p.yes_bid,
                    size_b=p.yes_bid_size,
                    gross_profit=profit,
                ))

        if p.yes_ask and k.yes_bid:
            profit = self._calc_profit(p.yes_ask, k.yes_bid)
            if profit >= self.min_profit_pct:
                opportunities.append(self._create_opportunity(
                    pair=pair,
                    platform_a="polymarket",
                    market_id_a=p.market_id,
                    side_a="yes",
                    price_a=p.yes_ask,
                    size_a=p.yes_ask_size,
                    platform_b="kalshi",
                    market_id_b=k.market_id,
                    side_b="yes",
                    price_b=k.yes_bid,
                    size_b=k.yes_bid_size,
                    gross_profit=profit,
                ))

        if k.no_ask and p.no_bid:
            profit = self._calc_profit(k.no_ask, p.no_bid)
            if profit >= self.min_profit_pct:
                opportunities.append(self._create_opportunity(
                    pair=pair,
                    platform_a="kalshi",
                    market_id_a=k.market_id,
                    side_a="no",
                    price_a=k.no_ask,
                    size_a=k.no_ask_size,
                    platform_b="polymarket",
                    market_id_b=p.market_id,
                    side_b="no",
                    price_b=p.no_bid,
                    size_b=p.no_bid_size,
                    gross_profit=profit,
                ))

        if p.no_ask and k.no_bid:
            profit = self._calc_profit(p.no_ask, k.no_bid)
            if profit >= self.min_profit_pct:
                opportunities.append(self._create_opportunity(
                    pair=pair,
                    platform_a="polymarket",
                    market_id_a=p.market_id,
                    side_a="no",
                    price_a=p.no_ask,
                    size_a=p.no_ask_size,
                    platform_b="kalshi",
                    market_id_b=k.market_id,
                    side_b="no",
                    price_b=k.no_bid,
                    size_b=k.no_bid_size,
                    gross_profit=profit,
                ))

        if opportunities:
            best = max(opportunities, key=lambda o: o.net_profit_pct)
            self._active_opps[best.opportunity_id] = best

            if self.callback:
                await self.callback(best)

            return best

        return None

    def _calc_profit(self, buy_price: Decimal, sell_price: Decimal) -> Decimal:
        """Calculate gross profit percentage."""
        if buy_price >= sell_price:
            return Decimal("-100")

        spread = sell_price - buy_price
        profit_pct = (spread / buy_price) * 100
        return profit_pct

    def _create_opportunity(
        self,
        pair: MarketPair,
        platform_a: str,
        market_id_a: str,
        side_a: str,
        price_a: Decimal,
        size_a: Optional[Decimal],
        platform_b: str,
        market_id_b: str,
        side_b: str,
        price_b: Decimal,
        size_b: Optional[Decimal],
        gross_profit: Decimal,
    ) -> ArbOpportunity:
        """Create an arbitrage opportunity."""
        self._opp_counter += 1

        fee_cost = (price_a + price_b) * self.fee_rate
        net_profit = gross_profit - (fee_cost / price_a * 100)

        max_stake = None
        if size_a and size_b:
            max_stake = min(size_a * price_a, size_b * price_b)

        return ArbOpportunity(
            opportunity_id=f"ARB-{self._opp_counter:06d}",
            event_key=pair.event_key,
            detected_at=datetime.utcnow(),
            platform_a=platform_a,
            market_id_a=market_id_a,
            side_a=side_a,
            price_a=price_a,
            size_a=size_a,
            platform_b=platform_b,
            market_id_b=market_id_b,
            side_b=side_b,
            price_b=price_b,
            size_b=size_b,
            gross_profit_pct=gross_profit,
            net_profit_pct=net_profit,
            max_stake=max_stake,
            title=pair.title,
        )

    def get_active_opportunities(self) -> list[ArbOpportunity]:
        """Get all active (non-expired) opportunities."""
        return [o for o in self._active_opps.values() if not o.expired]

    def expire_opportunity(self, opportunity_id: str) -> None:
        """Mark an opportunity as expired."""
        if opportunity_id in self._active_opps:
            self._active_opps[opportunity_id].expired = True
