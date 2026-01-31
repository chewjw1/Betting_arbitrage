"""Scheduled jobs for data collection and arbitrage detection.

This module runs automatic scans at configurable intervals:
- Default: every 30 seconds (configurable via POLL_INTERVAL_SECONDS)
- Collects data from all 6 platforms (3 API + 3 scraping)
- Detects both cross-platform AND logical arbitrage
- Sends Discord notifications for opportunities above threshold
- Stores all data in PostgreSQL for dashboard viewing

Usage:
    python -m src.scheduler.jobs

Or via Docker:
    docker-compose up scheduler
"""

import asyncio
from datetime import datetime
from decimal import Decimal
import time
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
import structlog

from src.arbitrage import CrossPlatformDetector, LogicalArbitrageDetector
from src.arbitrage.calculator import ArbitrageResult
from src.collectors import (
    KalshiCollector,
    PolymarketCollector,
    PredictItCollector,
    DraftKingsCollector,
    FanDuelCollector,
    IBKRCollector,
    API_COLLECTORS,
    SCRAPING_COLLECTORS,
)
from src.collectors.base import MarketData
from src.config import get_settings
from src.database import (
    async_session_factory,
    init_db,
    Market,
    Opportunity,
    Price,
)

logger = structlog.get_logger()
settings = get_settings()


class ArbitrageScanner:
    """Main scanner that coordinates data collection and arbitrage detection.

    Workflow:
    1. Fetch markets from all enabled platforms (parallel where possible)
    2. Store market data and price snapshots in database
    3. Run cross-platform arbitrage detection (same event, different prices)
    4. Run logical arbitrage detection (related events, probability inconsistencies)
    5. Send Discord notifications for profitable opportunities
    6. Store opportunities in database for dashboard
    """

    def __init__(
        self,
        discord_bot=None,
        min_net_spread_pct: Optional[float] = None,
        enable_scraping: bool = True,
        enable_logical: bool = True,
    ):
        """Initialize scanner.

        Args:
            discord_bot: Optional Discord bot for notifications.
            min_net_spread_pct: Override minimum profit threshold.
            enable_scraping: Whether to include scraping-based collectors.
            enable_logical: Whether to run logical arbitrage detection.
        """
        self.discord_bot = discord_bot
        self.min_net_spread_pct = min_net_spread_pct or settings.min_net_spread_pct
        self.enable_scraping = enable_scraping
        self.enable_logical = enable_logical
        self.logger = logger.bind(component="ArbitrageScanner")

        # Cross-platform detector
        self.cross_platform_detector = CrossPlatformDetector(
            min_net_spread_pct=self.min_net_spread_pct,
            min_match_confidence=0.8,
            default_position_size=settings.max_position_size,
        )

        # Logical arbitrage detector
        self.logical_detector = LogicalArbitrageDetector(
            min_violation_pct=2.0,
            min_net_profit_pct=self.min_net_spread_pct,
            min_relationship_confidence=0.7,
        )

    def _get_collectors(self) -> dict:
        """Get collectors based on configuration.

        Returns:
            Dict of platform_name -> collector_instance
        """
        collectors = {}

        # Always include API-based collectors (fast, reliable)
        collectors["kalshi"] = KalshiCollector()
        collectors["polymarket"] = PolymarketCollector()
        collectors["predictit"] = PredictItCollector()

        # Optionally include scraping-based collectors (slower, may fail)
        if self.enable_scraping:
            collectors["draftkings"] = DraftKingsCollector()
            collectors["fanduel"] = FanDuelCollector()
            collectors["ibkr"] = IBKRCollector()

        return collectors

    async def _collect_from_platform(
        self,
        platform: str,
        collector,
    ) -> tuple[str, list[MarketData]]:
        """Collect markets from a single platform.

        Args:
            platform: Platform name.
            collector: Collector instance.

        Returns:
            Tuple of (platform_name, markets_list).
        """
        try:
            async with collector:
                markets = await collector.fetch_markets()
                self.logger.info(
                    "Collected markets",
                    platform=platform,
                    count=len(markets),
                )
                return (platform, markets)
        except Exception as e:
            self.logger.error(
                "Failed to collect from platform",
                platform=platform,
                error=str(e),
            )
            return (platform, [])

    async def run_scan(self) -> dict:
        """Run a complete scan across all platforms.

        Returns:
            Dict with scan results:
            {
                "cross_platform": [ArbitrageResult, ...],
                "logical": [LogicalArbitrageResult, ...],
                "scan_time_seconds": float,
                "markets_by_platform": {platform: count, ...},
            }
        """
        start_time = time.time()
        self.logger.info("Starting arbitrage scan")

        collectors = self._get_collectors()

        # Collect from all platforms concurrently
        tasks = [
            self._collect_from_platform(platform, collector)
            for platform, collector in collectors.items()
        ]
        results = await asyncio.gather(*tasks)

        # Build markets dict
        markets_by_platform: dict[str, list[MarketData]] = {}
        for platform, markets in results:
            markets_by_platform[platform] = markets

        # Store markets and prices in database
        await self._store_market_data(markets_by_platform)

        # --- Cross-Platform Arbitrage Detection ---
        cross_platform_opps = self.cross_platform_detector.find_opportunities(
            markets_by_platform
        )
        self.logger.info(
            "Cross-platform scan complete",
            opportunities=len(cross_platform_opps),
        )

        # --- Logical Arbitrage Detection ---
        logical_opps = []
        if self.enable_logical:
            # Combine all markets for logical analysis
            all_markets = []
            for markets in markets_by_platform.values():
                all_markets.extend(markets)

            logical_opps = self.logical_detector.find_opportunities(
                all_markets,
                position_size=Decimal(str(settings.max_position_size)),
            )
            self.logger.info(
                "Logical arbitrage scan complete",
                opportunities=len(logical_opps),
            )

        # Store and notify for cross-platform opportunities
        for opp in cross_platform_opps:
            opportunity_id = await self._store_opportunity(opp, "cross_platform")
            if self.discord_bot and opportunity_id:
                await self.discord_bot.send_opportunity(opp, opportunity_id)

        # Store and notify for logical opportunities
        for opp in logical_opps:
            opportunity_id = await self._store_logical_opportunity(opp)
            if self.discord_bot and opportunity_id:
                await self._send_logical_notification(opp, opportunity_id)

        scan_time = time.time() - start_time

        self.logger.info(
            "Scan complete",
            duration_seconds=round(scan_time, 2),
            cross_platform_opportunities=len(cross_platform_opps),
            logical_opportunities=len(logical_opps),
            total_markets=sum(len(m) for m in markets_by_platform.values()),
        )

        # Send summary to Discord
        if self.discord_bot:
            await self.discord_bot.send_scan_summary(
                cross_platform_opps + logical_opps,  # type: ignore
                scan_time,
            )

        return {
            "cross_platform": cross_platform_opps,
            "logical": logical_opps,
            "scan_time_seconds": scan_time,
            "markets_by_platform": {
                p: len(m) for p, m in markets_by_platform.items()
            },
        }

    async def _store_market_data(
        self,
        markets_by_platform: dict[str, list[MarketData]],
    ) -> None:
        """Store collected market data in the database."""
        async with async_session_factory() as session:
            for platform, markets in markets_by_platform.items():
                for market_data in markets:
                    try:
                        # Check if market exists
                        query = select(Market).where(
                            Market.platform == market_data.platform,
                            Market.platform_market_id == market_data.platform_market_id,
                        )
                        result = await session.execute(query)
                        market = result.scalar_one_or_none()

                        if market:
                            # Update existing market
                            market.title = market_data.title
                            market.description = market_data.description
                            market.status = market_data.status
                            market.end_date = market_data.end_date
                            market.url = market_data.url
                            market.updated_at = datetime.utcnow()
                        else:
                            # Create new market
                            market = Market(
                                platform=market_data.platform,
                                platform_market_id=market_data.platform_market_id,
                                title=market_data.title,
                                description=market_data.description,
                                resolution_criteria=market_data.resolution_criteria,
                                category=market_data.category,
                                end_date=market_data.end_date,
                                status=market_data.status,
                                url=market_data.url,
                            )
                            session.add(market)
                            await session.flush()

                        # Add price snapshot
                        if market_data.yes_price is not None:
                            price = Price(
                                market_id=market.id,
                                yes_price=market_data.yes_price,
                                no_price=market_data.no_price,
                                yes_volume=market_data.yes_volume,
                                no_volume=market_data.no_volume,
                                bid_yes=market_data.yes_bid,
                                ask_yes=market_data.yes_ask,
                            )
                            session.add(price)

                    except Exception as e:
                        self.logger.warning(
                            "Failed to store market",
                            platform=platform,
                            market_id=market_data.platform_market_id,
                            error=str(e),
                        )

            await session.commit()

    async def _store_opportunity(
        self,
        result: ArbitrageResult,
        opportunity_type: str = "cross_platform",
    ) -> Optional[str]:
        """Store a cross-platform opportunity in the database."""
        async with async_session_factory() as session:
            try:
                market_a_query = select(Market).where(
                    Market.platform == result.market_a.platform,
                    Market.platform_market_id == result.market_a.platform_market_id,
                )
                market_b_query = select(Market).where(
                    Market.platform == result.market_b.platform,
                    Market.platform_market_id == result.market_b.platform_market_id,
                )

                market_a_result = await session.execute(market_a_query)
                market_b_result = await session.execute(market_b_query)

                market_a = market_a_result.scalar_one_or_none()
                market_b = market_b_result.scalar_one_or_none()

                if not market_a or not market_b:
                    return None

                opportunity = Opportunity(
                    opportunity_type=opportunity_type,
                    platform_a=result.market_a.platform,
                    market_a_id=market_a.id,
                    side_a=result.side_a,
                    price_a=result.price_a,
                    platform_b=result.market_b.platform,
                    market_b_id=market_b.id,
                    side_b=result.side_b,
                    price_b=result.price_b,
                    gross_spread=result.gross_spread,
                    estimated_fees=result.total_fees,
                    net_profit_pct=result.net_profit_pct,
                    position_size=result.position_size,
                )

                session.add(opportunity)
                await session.commit()

                return str(opportunity.id)

            except Exception as e:
                self.logger.error("Failed to store opportunity", error=str(e))
                return None

    async def _store_logical_opportunity(self, result) -> Optional[str]:
        """Store a logical arbitrage opportunity in the database."""
        async with async_session_factory() as session:
            try:
                rel = result.relationship
                market_a = rel.market_a
                market_b = rel.market_b

                market_a_query = select(Market).where(
                    Market.platform == market_a.platform,
                    Market.platform_market_id == market_a.platform_market_id,
                )

                market_a_result = await session.execute(market_a_query)
                market_a_db = market_a_result.scalar_one_or_none()

                if not market_a_db:
                    return None

                # For logical arb, market_b might be the same as market_a
                market_b_db = market_a_db
                if market_b.platform_market_id != market_a.platform_market_id:
                    market_b_query = select(Market).where(
                        Market.platform == market_b.platform,
                        Market.platform_market_id == market_b.platform_market_id,
                    )
                    market_b_result = await session.execute(market_b_query)
                    market_b_db = market_b_result.scalar_one_or_none() or market_a_db

                opportunity = Opportunity(
                    opportunity_type=f"logical_{rel.relationship_type.value}",
                    platform_a=market_a.platform,
                    market_a_id=market_a_db.id,
                    side_a="yes",
                    price_a=market_a.yes_price or Decimal("0"),
                    platform_b=market_b.platform,
                    market_b_id=market_b_db.id,
                    side_b="no",
                    price_b=market_b.no_price or Decimal("0"),
                    gross_spread=result.violation_amount,
                    estimated_fees=result.estimated_fees,
                    net_profit_pct=result.net_profit_pct,
                    position_size=Decimal(str(settings.max_position_size)),
                )

                session.add(opportunity)
                await session.commit()

                return str(opportunity.id)

            except Exception as e:
                self.logger.error("Failed to store logical opportunity", error=str(e))
                return None

    async def _send_logical_notification(self, result, opportunity_id: str) -> None:
        """Send Discord notification for logical arbitrage."""
        if not self.discord_bot or not self.discord_bot.alert_channel:
            return

        rel = result.relationship
        embed_content = (
            f"**Logical Arbitrage: {rel.relationship_type.value}**\n\n"
            f"Market: {rel.market_a.title[:100]}\n"
            f"Platform: {rel.market_a.platform}\n"
            f"Constraint: {rel.expected_constraint}\n"
            f"Violation: {float(result.violation_amount):.2%}\n"
            f"Net Profit: {float(result.net_profit_pct):.2f}%\n\n"
            f"Action: {result.recommended_action or 'See details'}\n"
            f"ID: {opportunity_id}"
        )

        try:
            message = await self.discord_bot.alert_channel.send(embed_content)
            await message.add_reaction("✅")
            await message.add_reaction("❌")
            self.discord_bot.pending_notifications[message.id] = opportunity_id
        except Exception as e:
            self.logger.error("Failed to send logical notification", error=str(e))


# Global scanner instance (for scheduled job)
_scanner: Optional[ArbitrageScanner] = None


async def run_scanner():
    """Run the scanner as a scheduled job."""
    global _scanner
    if _scanner is None:
        _scanner = ArbitrageScanner(
            enable_scraping=settings.poll_interval_seconds >= 60,  # Only scrape if interval >= 1min
            enable_logical=True,
        )
    await _scanner.run_scan()


async def main():
    """Main entry point for the scheduler.

    Runs automatic scans at the configured interval.
    Default: every 30 seconds (set POLL_INTERVAL_SECONDS to change).
    """
    global _scanner

    logger.info("Initializing arbitrage scanner scheduler")

    # Initialize database
    await init_db()

    # Create scanner
    # Only enable scraping if interval is long enough (scraping is slow)
    enable_scraping = settings.poll_interval_seconds >= 60
    _scanner = ArbitrageScanner(
        enable_scraping=enable_scraping,
        enable_logical=True,
    )

    logger.info(
        "Scanner configured",
        poll_interval=settings.poll_interval_seconds,
        scraping_enabled=enable_scraping,
        logical_enabled=True,
        min_profit_threshold=settings.min_net_spread_pct,
    )

    # Create scheduler
    scheduler = AsyncIOScheduler()

    # Add scan job
    scheduler.add_job(
        run_scanner,
        trigger=IntervalTrigger(seconds=settings.poll_interval_seconds),
        id="arbitrage_scan",
        name="Arbitrage Scanner",
        replace_existing=True,
        max_instances=1,  # Don't overlap scans
    )

    # Start scheduler
    scheduler.start()
    logger.info(
        "Scheduler started - scanning every %d seconds",
        settings.poll_interval_seconds,
    )

    # Run initial scan immediately
    logger.info("Running initial scan...")
    await run_scanner()

    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
