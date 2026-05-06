"""Enhanced trading bot with multiple signal sources.

Uses:
1. Real-time BTC price from Binance WebSocket
2. Order flow analysis (aggressive trade detection)
3. Funding rate positioning signals
4. Kalshi market data
5. Enhanced fair value model combining all signals
"""

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
import json

import structlog

from ..feeds.binance_ws import BinanceBTCFeed
from ..feeds.binance_orderflow import BinanceOrderFlowFeed
from ..feeds.funding_rate import FundingRateFeed
from ..feeds.kalshi_15m import Kalshi15MFeed, Kalshi15MMarket
from ..models.enhanced_fair_value import EnhancedFairValueCalculator, EnhancedFairValueResult

logger = structlog.get_logger()


@dataclass
class Trade:
    """Record of a trade."""
    timestamp: datetime
    ticker: str
    side: str
    price: float
    size: int
    target_price: float
    current_btc: float
    edge: float
    signal_strength: float
    reasoning: str
    components: dict
    result: Optional[str] = None
    pnl: float = 0.0


@dataclass
class TradingSession:
    """Track session statistics."""
    trades: list[Trade] = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    signals_seen: int = 0
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0


class EnhancedTradingBot:
    """Enhanced trading bot with multiple signal sources."""

    def __init__(
        self,
        mode: str = "paper",
        bankroll: float = 1000.0,
        max_position_pct: float = 0.05,
        min_edge: float = 0.04,
        min_strength: float = 0.3,
    ):
        self.mode = mode
        self.bankroll = bankroll
        self.max_position_pct = max_position_pct
        self.min_edge = min_edge
        self.min_strength = min_strength

        # Feeds
        self.btc_feed = BinanceBTCFeed()
        self.orderflow_feed = BinanceOrderFlowFeed()
        self.funding_feed: Optional[FundingRateFeed] = None
        self.kalshi_feed: Optional[Kalshi15MFeed] = None

        # Calculator
        self.fair_value_calc = EnhancedFairValueCalculator()

        # State
        self.session = TradingSession()
        self.current_position: Optional[Trade] = None
        self._running = False

    async def start(self):
        """Start the enhanced trading bot."""
        logger.info("enhanced_bot_starting", mode=self.mode, bankroll=self.bankroll)
        self._running = True

        # Start feeds in background
        btc_task = asyncio.create_task(self.btc_feed.connect())
        orderflow_task = asyncio.create_task(self.orderflow_feed.connect())

        # Wait for feeds to collect data
        print("Initializing feeds (30 seconds to collect data)...")
        await asyncio.sleep(30)
        print("Feeds ready. Starting trading loop.\n")

        # Main loop with funding + Kalshi feeds
        async with FundingRateFeed() as funding:
            self.funding_feed = funding
            async with Kalshi15MFeed(assets=["BTC"]) as kalshi:
                self.kalshi_feed = kalshi

                while self._running:
                    try:
                        await self._trading_cycle()
                        await asyncio.sleep(5)
                    except Exception as e:
                        logger.error("trading_cycle_error", error=str(e))
                        await asyncio.sleep(10)

        await self.btc_feed.disconnect()
        await self.orderflow_feed.disconnect()
        btc_task.cancel()
        orderflow_task.cancel()

    async def stop(self):
        """Stop the bot."""
        self._running = False
        logger.info("enhanced_bot_stopped", session=self._session_summary())

    async def _trading_cycle(self):
        """One cycle of the trading loop."""
        # Get Kalshi market
        markets = await self.kalshi_feed.get_active_markets()
        if not markets:
            return

        market = markets[0]

        # Get BTC features
        btc_features = self.btc_feed.get_all_features()
        if not btc_features.get("valid", False):
            return

        # Get order flow signal
        of_signal = self.orderflow_feed.get_signal()

        # Get funding signal
        fund_signal = await self.funding_feed.get_signal("BTCUSDT")

        # Get recent results for momentum
        recent_results = await self.kalshi_feed.get_recent_results("BTC", limit=10)

        # Calculate enhanced fair value
        result = self.fair_value_calc.calculate(
            current_price=btc_features["price"],
            target_price=float(market.target_price),
            minutes_remaining=market.minutes_remaining,
            volatility_15m=btc_features.get("vol_15m", 0.003),
            order_flow_signal=of_signal.signal,
            order_flow_strength=of_signal.strength,
            order_flow_delta=of_signal.delta_60s,
            funding_signal=fund_signal.signal,
            funding_strength=fund_signal.strength,
            funding_rate=fund_signal.current_rate,
            recent_results=recent_results,
            market_yes_bid=float(market.yes_bid),
            market_yes_ask=float(market.yes_ask),
        )

        # Log state
        self._log_state(market, btc_features, of_signal, fund_signal, result)
        self.session.signals_seen += 1

        # Check for exit
        if self.current_position:
            await self._check_exit(market, result)

        # Check for entry
        if not self.current_position and result.signal != "NO_TRADE":
            if result.strength >= self.min_strength:
                await self._enter_trade(market, result, btc_features["price"])

    def _log_state(self, market, features, of_signal, fund_signal, result):
        """Log current state with all signals."""
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"BTC: ${features['price']:,.0f} | "
            f"Target: ${float(market.target_price):,.0f} | "
            f"Time: {market.minutes_remaining:.1f}m | "
            f"Mkt: {float(market.yes_bid)*100:.0f}/{float(market.yes_ask)*100:.0f}c | "
            f"Fair: {result.fair_value_yes*100:.0f}c | "
            f"OF: {of_signal.signal[:1]} | "
            f"Fund: {fund_signal.signal[:1]} | "
            f"Edge: {max(result.edge_yes, result.edge_no)*100:+.0f}% | "
            f"{result.signal}"
        )

    async def _enter_trade(self, market, result, current_btc):
        """Enter a new trade."""
        edge = result.edge_yes if result.signal == "BUY_YES" else result.edge_no

        # Kelly-based position sizing
        kelly_fraction = edge / 0.5
        position_pct = min(self.max_position_pct, kelly_fraction * 0.5)  # Half Kelly
        position_size = int(self.bankroll * position_pct)

        if result.signal == "BUY_YES":
            price = float(market.yes_ask)
            side = "YES"
        else:
            price = float(market.no_ask)
            side = "NO"

        trade = Trade(
            timestamp=datetime.now(timezone.utc),
            ticker=market.ticker,
            side=side,
            price=price,
            size=position_size,
            target_price=float(market.target_price),
            current_btc=current_btc,
            edge=edge,
            signal_strength=result.strength,
            reasoning=result.reasoning,
            components=result.components,
        )

        if self.mode == "paper":
            print(
                f"\n{'='*60}\n"
                f"*** PAPER TRADE: {side} @ {price*100:.0f}c x {position_size} ***\n"
                f"    Edge: {edge*100:.1f}% | Strength: {result.strength:.0%}\n"
                f"    Reason: {result.reasoning}\n"
                f"{'='*60}\n"
            )

        self.current_position = trade
        self.session.trades.append(trade)

    async def _check_exit(self, market, result):
        """Check if we should exit."""
        if not self.current_position:
            return

        should_exit = False
        reason = ""

        # Exit conditions
        if market.minutes_remaining < 1:
            should_exit = True
            reason = "Time expiring"

        if self.current_position.side == "YES" and result.signal == "BUY_NO":
            if result.strength > 0.5:  # Strong reversal signal
                should_exit = True
                reason = "Strong reversal signal"

        # Profit/loss targets
        if self.current_position.side == "YES":
            current_value = float(market.yes_bid)
        else:
            current_value = float(market.no_bid)

        entry_price = self.current_position.price
        pnl_pct = (current_value - entry_price) / entry_price if entry_price > 0 else 0

        if pnl_pct > 0.4:  # 40% profit
            should_exit = True
            reason = f"Profit target ({pnl_pct*100:.0f}%)"
        elif pnl_pct < -0.3:  # 30% loss
            should_exit = True
            reason = f"Stop loss ({pnl_pct*100:.0f}%)"

        if should_exit:
            await self._exit_trade(current_value, reason)

    async def _exit_trade(self, exit_price, reason):
        """Exit current position."""
        if not self.current_position:
            return

        entry_price = self.current_position.price
        pnl_per_contract = exit_price - entry_price
        total_pnl = pnl_per_contract * self.current_position.size

        self.current_position.pnl = total_pnl
        self.current_position.result = "WIN" if total_pnl > 0 else "LOSS"

        if total_pnl > 0:
            self.session.wins += 1
        else:
            self.session.losses += 1

        self.session.total_pnl += total_pnl
        self.bankroll += total_pnl

        print(
            f"\n{'='*60}\n"
            f"*** EXIT: {self.current_position.side} | "
            f"Entry: {entry_price*100:.0f}c → Exit: {exit_price*100:.0f}c | "
            f"PnL: ${total_pnl:+.2f} | {reason} ***\n"
            f"{'='*60}\n"
        )

        self.current_position = None

    def _session_summary(self) -> dict:
        return {
            "trades": len(self.session.trades),
            "wins": self.session.wins,
            "losses": self.session.losses,
            "win_rate": f"{self.session.win_rate*100:.1f}%",
            "total_pnl": f"${self.session.total_pnl:.2f}",
            "bankroll": f"${self.bankroll:.2f}",
            "signals_seen": self.session.signals_seen,
        }


async def main():
    """Run enhanced trading bot."""
    print("=" * 70)
    print("ENHANCED CRYPTO 15M TRADING BOT")
    print("=" * 70)
    print("\nSignal Sources:")
    print("  - Real-time BTC price (Binance WebSocket)")
    print("  - Order flow analysis (aggressive trade detection)")
    print("  - Funding rate positioning")
    print("  - Kalshi market momentum")
    print("\nPress Ctrl+C to stop.\n")

    bot = EnhancedTradingBot(
        mode="paper",
        bankroll=1000.0,
        max_position_pct=0.05,
        min_edge=0.04,
        min_strength=0.3,
    )

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()
        print("\n" + "=" * 70)
        print("SESSION SUMMARY")
        print("=" * 70)
        for k, v in bot._session_summary().items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
