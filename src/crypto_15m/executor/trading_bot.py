"""Automated trading bot for Kalshi 15-minute crypto markets.

This bot:
1. Monitors real-time BTC price from Binance
2. Tracks Kalshi 15-minute markets
3. Calculates fair value using momentum + volatility model
4. Detects edge opportunities
5. Executes trades (paper or live)
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Optional
import json

import structlog

from ..feeds.binance_ws import BinanceBTCFeed
from ..feeds.kalshi_15m import Kalshi15MFeed, Kalshi15MMarket
from ..models.fair_value import FairValueCalculator, FairValueResult

logger = structlog.get_logger()


@dataclass
class Trade:
    """Record of a trade."""
    timestamp: datetime
    ticker: str
    side: str  # "YES" or "NO"
    price: float
    size: int
    target_price: float
    current_btc: float
    edge: float
    reasoning: str
    result: Optional[str] = None  # "WIN", "LOSS", or None if pending
    pnl: float = 0.0


@dataclass
class TradingSession:
    """Track session statistics."""
    trades: list[Trade] = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0


class Crypto15MTradingBot:
    """Automated trading bot for 15-minute crypto markets."""

    def __init__(
        self,
        mode: str = "paper",  # "paper" or "live"
        bankroll: float = 1000.0,
        max_position_pct: float = 0.05,  # Max 5% of bankroll per trade
        min_edge: float = 0.05,  # Minimum 5% edge to trade
        kalshi_api_key: str = None,
        kalshi_private_key: str = None,
    ):
        self.mode = mode
        self.bankroll = bankroll
        self.max_position_pct = max_position_pct
        self.min_edge = min_edge

        # API credentials (for live trading)
        self.kalshi_api_key = kalshi_api_key
        self.kalshi_private_key = kalshi_private_key

        # Components
        self.btc_feed = BinanceBTCFeed()
        self.kalshi_feed: Optional[Kalshi15MFeed] = None
        self.fair_value_calc = FairValueCalculator()

        # State
        self.session = TradingSession()
        self.current_position: Optional[Trade] = None
        self._running = False

    async def start(self):
        """Start the trading bot."""
        logger.info("bot_starting", mode=self.mode, bankroll=self.bankroll)
        self._running = True

        # Start BTC feed in background
        btc_task = asyncio.create_task(self.btc_feed.connect())

        # Wait for initial data
        await asyncio.sleep(5)

        # Main loop
        async with Kalshi15MFeed(assets=["BTC"]) as kalshi:
            self.kalshi_feed = kalshi

            while self._running:
                try:
                    await self._trading_cycle()
                    await asyncio.sleep(5)  # Check every 5 seconds
                except Exception as e:
                    logger.error("trading_cycle_error", error=str(e))
                    await asyncio.sleep(10)

        await self.btc_feed.disconnect()
        btc_task.cancel()

    async def stop(self):
        """Stop the trading bot."""
        self._running = False
        logger.info("bot_stopped", session=self._session_summary())

    async def _trading_cycle(self):
        """One cycle of the trading loop."""
        # Get current market state
        markets = await self.kalshi_feed.get_active_markets()
        if not markets:
            return

        market = markets[0]  # Focus on BTC for now

        # Get features
        btc_features = self.btc_feed.get_all_features()
        if not btc_features.get("valid", False):
            return

        # Get recent results for momentum
        recent_results = await self.kalshi_feed.get_recent_results("BTC", limit=10)

        # Calculate fair value
        result = self.fair_value_calc.calculate(
            current_price=btc_features["price"],
            target_price=float(market.target_price),
            minutes_remaining=market.minutes_remaining,
            volatility_15m=btc_features.get("vol_15m", 0.003),
            recent_results=recent_results,
            market_yes_bid=float(market.yes_bid),
            market_yes_ask=float(market.yes_ask),
        )

        # Log state
        self._log_state(market, btc_features, result)

        # Check for exit
        if self.current_position:
            await self._check_exit(market, result)

        # Check for entry
        if not self.current_position and result.signal != "NO_TRADE":
            await self._enter_trade(market, result, btc_features["price"])

    def _log_state(self, market: Kalshi15MMarket, features: dict, result: FairValueResult):
        """Log current state."""
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"BTC: ${features['price']:,.0f} | "
            f"Target: ${float(market.target_price):,.0f} | "
            f"Time: {market.minutes_remaining:.1f}m | "
            f"Market: {float(market.yes_bid)*100:.0f}c/{float(market.yes_ask)*100:.0f}c | "
            f"Fair: {result.fair_value_yes*100:.0f}c | "
            f"Edge: {max(result.edge_yes, result.edge_no)*100:+.1f}% | "
            f"Signal: {result.signal}"
        )

    async def _enter_trade(self, market: Kalshi15MMarket, result: FairValueResult, current_btc: float):
        """Enter a new trade."""
        # Calculate position size
        edge = result.edge_yes if result.signal == "BUY_YES" else result.edge_no
        kelly_fraction = edge / 0.5  # Simplified Kelly
        position_pct = min(self.max_position_pct, kelly_fraction)
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
            reasoning=result.reasoning,
        )

        if self.mode == "paper":
            logger.info(
                "paper_trade_entered",
                side=side,
                price=price,
                size=position_size,
                edge=f"{edge*100:.1f}%",
            )
            print(f"\n*** PAPER TRADE: {side} @ {price*100:.0f}c x {position_size} contracts ***")
            print(f"    Reasoning: {result.reasoning}\n")
        else:
            # TODO: Implement live Kalshi API execution
            logger.info("live_trade_would_execute", trade=trade)

        self.current_position = trade
        self.session.trades.append(trade)

    async def _check_exit(self, market: Kalshi15MMarket, result: FairValueResult):
        """Check if we should exit current position."""
        if not self.current_position:
            return

        should_exit = False
        reason = ""

        # Exit if < 1 minute remaining (avoid settlement risk)
        if market.minutes_remaining < 1:
            should_exit = True
            reason = "Time expiring"

        # Exit if edge flipped
        if self.current_position.side == "YES" and result.signal == "BUY_NO":
            should_exit = True
            reason = "Edge flipped"
        elif self.current_position.side == "NO" and result.signal == "BUY_YES":
            should_exit = True
            reason = "Edge flipped"

        # Exit if profit target hit (50% of max)
        if self.current_position.side == "YES":
            current_value = float(market.yes_bid)
        else:
            current_value = float(market.no_bid)

        entry_price = self.current_position.price
        pnl_pct = (current_value - entry_price) / entry_price

        if pnl_pct > 0.5:  # 50% profit
            should_exit = True
            reason = f"Profit target hit ({pnl_pct*100:.0f}%)"

        if should_exit:
            await self._exit_trade(current_value, reason)

    async def _exit_trade(self, exit_price: float, reason: str):
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

        logger.info(
            "trade_exited",
            side=self.current_position.side,
            entry=entry_price,
            exit=exit_price,
            pnl=total_pnl,
            reason=reason,
        )
        print(
            f"\n*** TRADE EXITED: {self.current_position.side} | "
            f"Entry: {entry_price*100:.0f}c | Exit: {exit_price*100:.0f}c | "
            f"PnL: ${total_pnl:.2f} | Reason: {reason} ***\n"
        )

        self.current_position = None

    def _session_summary(self) -> dict:
        """Get session summary."""
        return {
            "trades": len(self.session.trades),
            "wins": self.session.wins,
            "losses": self.session.losses,
            "win_rate": f"{self.session.win_rate*100:.1f}%",
            "total_pnl": f"${self.session.total_pnl:.2f}",
            "bankroll": f"${self.bankroll:.2f}",
        }


async def main():
    """Run the trading bot."""
    print("=" * 70)
    print("CRYPTO 15M TRADING BOT - PAPER MODE")
    print("=" * 70)
    print("\nThis bot monitors Kalshi 15-minute BTC markets and")
    print("identifies edge opportunities using momentum + volatility signals.")
    print("\nPress Ctrl+C to stop.\n")

    bot = Crypto15MTradingBot(
        mode="paper",
        bankroll=1000.0,
        max_position_pct=0.05,
        min_edge=0.05,
    )

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()
        print("\n" + "=" * 70)
        print("SESSION SUMMARY")
        print("=" * 70)
        summary = bot._session_summary()
        for k, v in summary.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
