"""Main runner for live sports arbitrage scanner."""

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
import structlog

from .feeds import KalshiFeed, PolymarketFeed, PriceUpdate
from .execution import KalshiExecutor, PolymarketExecutor, Order, OrderSide
from .detector import ArbDetector, ArbOpportunity

logger = structlog.get_logger()


class SportsArbRunner:
    """Main runner for live sports arbitrage.

    Modes:
    - measurement: Log all opportunities without executing (Phase 0)
    - paper: Simulate trades with paper money
    - live: Execute real trades (requires API keys)
    """

    def __init__(
        self,
        mode: str = "measurement",
        min_profit_pct: float = 0.5,
        max_position_usd: float = 100.0,
        log_dir: Optional[str] = None,
        kalshi_api_key: Optional[str] = None,
        kalshi_private_key: Optional[str] = None,
        polymarket_api_key: Optional[str] = None,
        polymarket_api_secret: Optional[str] = None,
        polymarket_passphrase: Optional[str] = None,
    ):
        self.mode = mode
        self.min_profit_pct = Decimal(str(min_profit_pct))
        self.max_position_usd = Decimal(str(max_position_usd))
        self.log_dir = Path(log_dir) if log_dir else Path("./arb_logs")

        self._running = False
        self._opportunities_logged = 0
        self._trades_executed = 0
        self._total_pnl = Decimal("0")

        self.detector = ArbDetector(
            min_profit_pct=self.min_profit_pct,
            callback=self._on_opportunity,
        )

        self.kalshi_feed = KalshiFeed(callback=self._on_price_update)
        self.polymarket_feed = PolymarketFeed(callback=self._on_price_update)

        paper_mode = mode in ("measurement", "paper")
        self.kalshi_executor = KalshiExecutor(
            api_key=kalshi_api_key,
            private_key=kalshi_private_key,
            paper_mode=paper_mode,
        )
        self.polymarket_executor = PolymarketExecutor(
            api_key=polymarket_api_key,
            api_secret=polymarket_api_secret,
            passphrase=polymarket_passphrase,
            paper_mode=paper_mode,
        )

        self._log_file = None

    async def start(self) -> None:
        """Start the arbitrage scanner."""
        self._running = True
        self.log_dir.mkdir(parents=True, exist_ok=True)

        log_name = f"arb_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jsonl"
        self._log_file = open(self.log_dir / log_name, "a")

        logger.info(
            "sports_arb_starting",
            mode=self.mode,
            min_profit=str(self.min_profit_pct),
            max_position=str(self.max_position_usd),
            log_file=str(self.log_dir / log_name),
        )

        await self.kalshi_executor.connect()
        await self.polymarket_executor.connect()

        feed_tasks = [
            asyncio.create_task(self.kalshi_feed.run()),
            asyncio.create_task(self.polymarket_feed.run()),
        ]

        try:
            await asyncio.gather(*feed_tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the scanner."""
        self._running = False
        await self.kalshi_feed.stop()
        await self.polymarket_feed.stop()
        await self.kalshi_executor.disconnect()
        await self.polymarket_executor.disconnect()

        if self._log_file:
            self._log_file.close()

        logger.info(
            "sports_arb_stopped",
            opportunities_logged=self._opportunities_logged,
            trades_executed=self._trades_executed,
            total_pnl=str(self._total_pnl),
        )

    async def register_markets(
        self,
        market_pairs: list[dict],
    ) -> None:
        """Register linked markets for monitoring.

        Args:
            market_pairs: List of dicts with:
                - event_key: Unique identifier for the event
                - title: Human-readable title
                - kalshi_market_id: Kalshi ticker
                - polymarket_token_id: Polymarket token ID
        """
        kalshi_markets = []
        polymarket_markets = []

        for pair in market_pairs:
            self.detector.register_pair(
                event_key=pair["event_key"],
                title=pair.get("title", pair["event_key"]),
                kalshi_market_id=pair.get("kalshi_market_id"),
                polymarket_token_id=pair.get("polymarket_token_id"),
            )

            if pair.get("kalshi_market_id"):
                kalshi_markets.append(pair["kalshi_market_id"])
            if pair.get("polymarket_token_id"):
                polymarket_markets.append({
                    "token_id": pair["polymarket_token_id"],
                    "title": pair.get("title", ""),
                    "event_id": pair["event_key"],
                })

        if kalshi_markets:
            await self.kalshi_feed.subscribe(kalshi_markets)
        if polymarket_markets:
            await self.polymarket_feed.subscribe_with_info(polymarket_markets)

        logger.info(
            "markets_registered",
            kalshi_count=len(kalshi_markets),
            polymarket_count=len(polymarket_markets),
        )

    async def _on_price_update(self, update: PriceUpdate) -> None:
        """Handle incoming price update."""
        await self.detector.on_price_update(update)

    async def _on_opportunity(self, opp: ArbOpportunity) -> None:
        """Handle detected arbitrage opportunity."""
        self._opportunities_logged += 1
        self._log_opportunity(opp)

        logger.info(
            "arb_detected",
            opportunity_id=opp.opportunity_id,
            profit_pct=f"{opp.net_profit_pct:.2f}%",
            platform_a=opp.platform_a,
            platform_b=opp.platform_b,
            title=opp.title[:50],
        )

        if self.mode == "measurement":
            return

        if opp.net_profit_pct >= self.min_profit_pct:
            await self._execute_arbitrage(opp)

    def _log_opportunity(self, opp: ArbOpportunity) -> None:
        """Log opportunity to file."""
        if not self._log_file:
            return

        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "opportunity",
            "data": {
                "opportunity_id": opp.opportunity_id,
                "event_key": opp.event_key,
                "title": opp.title,
                "platform_a": opp.platform_a,
                "market_id_a": opp.market_id_a,
                "side_a": opp.side_a,
                "price_a": str(opp.price_a),
                "platform_b": opp.platform_b,
                "market_id_b": opp.market_id_b,
                "side_b": opp.side_b,
                "price_b": str(opp.price_b),
                "gross_profit_pct": str(opp.gross_profit_pct),
                "net_profit_pct": str(opp.net_profit_pct),
                "max_stake": str(opp.max_stake) if opp.max_stake else None,
            },
        }

        self._log_file.write(json.dumps(record) + "\n")
        self._log_file.flush()

    async def _execute_arbitrage(self, opp: ArbOpportunity) -> None:
        """Execute an arbitrage trade."""
        quantity = min(
            10,
            int(self.max_position_usd / opp.price_a) if opp.price_a else 10,
        )

        if quantity < 1:
            logger.warning("arb_skip_low_quantity", opportunity_id=opp.opportunity_id)
            return

        order_a = Order(
            market_id=opp.market_id_a,
            side=OrderSide.BUY,
            quantity=quantity,
            price=opp.price_a,
            is_yes=opp.side_a == "yes",
        )

        order_b = Order(
            market_id=opp.market_id_b,
            side=OrderSide.SELL,
            quantity=quantity,
            price=opp.price_b,
            is_yes=opp.side_b == "yes",
        )

        executor_a = (
            self.kalshi_executor if opp.platform_a == "kalshi"
            else self.polymarket_executor
        )
        executor_b = (
            self.kalshi_executor if opp.platform_b == "kalshi"
            else self.polymarket_executor
        )

        results = await asyncio.gather(
            executor_a.place_order(order_a),
            executor_b.place_order(order_b),
            return_exceptions=True,
        )

        success = all(
            not isinstance(r, Exception) and r.success
            for r in results
        )

        if success:
            self._trades_executed += 1
            logger.info(
                "arb_executed",
                opportunity_id=opp.opportunity_id,
                quantity=quantity,
            )
        else:
            logger.error(
                "arb_execution_failed",
                opportunity_id=opp.opportunity_id,
                results=[str(r) for r in results],
            )

        self._log_execution(opp, results, success)

    def _log_execution(
        self,
        opp: ArbOpportunity,
        results: list,
        success: bool,
    ) -> None:
        """Log execution to file."""
        if not self._log_file:
            return

        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "execution",
            "data": {
                "opportunity_id": opp.opportunity_id,
                "success": success,
                "results": [
                    {
                        "success": r.success if hasattr(r, "success") else False,
                        "order_id": r.order_id if hasattr(r, "order_id") else None,
                        "error": str(r) if isinstance(r, Exception) else r.error,
                    }
                    for r in results
                ],
            },
        }

        self._log_file.write(json.dumps(record) + "\n")
        self._log_file.flush()


async def main():
    """Example usage."""
    runner = SportsArbRunner(
        mode="measurement",
        min_profit_pct=0.5,
        max_position_usd=100.0,
        log_dir="./arb_logs",
    )

    example_pairs = [
        {
            "event_key": "btc-above-70k-may-2026",
            "title": "Will Bitcoin be above $70k on May 31, 2026?",
            "kalshi_market_id": "KXBTC-26MAY31-T70000",
            "polymarket_token_id": "0x...",
        },
    ]

    await runner.kalshi_feed.connect()
    await runner.polymarket_feed.connect()
    await runner.register_markets(example_pairs)

    try:
        await runner.start()
    except KeyboardInterrupt:
        await runner.stop()


if __name__ == "__main__":
    asyncio.run(main())
