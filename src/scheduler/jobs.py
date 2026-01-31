"""Scheduled jobs for data collection and arbitrage detection."""

import asyncio
from datetime import datetime
from decimal import Decimal
import time
from typing import Optional
import uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.arbitrage import CrossPlatformDetector, ArbitrageResult
from src.collectors import KalshiCollector, PolymarketCollector, PredictItCollector
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
    """Main scanner that coordinates data collection and arbitrage detection."""

    def __init__(
        self,
        discord_bot=None,
        min_net_spread_pct: Optional[float] = None,
    ):
        """Initialize scanner.

        Args:
            discord_bot: Optional Discord bot for notifications.
            min_net_spread_pct: Override minimum profit threshold.
        """
        self.discord_bot = discord_bot
        self.min_net_spread_pct = min_net_spread_pct or settings.min_net_spread_pct
        self.detector = CrossPlatformDetector(
            min_net_spread_pct=self.min_net_spread_pct,
            min_match_confidence=0.8,
            default_position_size=settings.max_position_size,
        )
        self.logger = logger.bind(component="ArbitrageScanner")

        # Initialize collectors
        self.collectors = {
            "kalshi": KalshiCollector(),
            "polymarket": PolymarketCollector(),
            "predictit": PredictItCollector(),
        }

    async def run_scan(self) -> list[ArbitrageResult]:
        """Run a complete scan across all platforms.

        Returns:
            List of profitable ArbitrageResult objects.
        """
        start_time = time.time()
        self.logger.info("Starting arbitrage scan")

        # Collect market data from all platforms
        markets_by_platform: dict[str, list[MarketData]] = {}

        for platform, collector in self.collectors.items():
            try:
                async with collector:
                    markets = await collector.fetch_markets()
                    markets_by_platform[platform] = markets
                    self.logger.info(
                        "Collected markets",
                        platform=platform,
                        count=len(markets),
                    )
            except Exception as e:
                self.logger.error(
                    "Failed to collect from platform",
                    platform=platform,
                    error=str(e),
                )
                markets_by_platform[platform] = []

        # Store markets and prices in database
        await self._store_market_data(markets_by_platform)

        # Detect arbitrage opportunities
        opportunities = self.detector.find_opportunities(markets_by_platform)

        # Store opportunities and send notifications
        for opp in opportunities:
            opportunity_id = await self._store_opportunity(opp)

            if self.discord_bot and opportunity_id:
                await self.discord_bot.send_opportunity(opp, opportunity_id)

        scan_time = time.time() - start_time
        self.logger.info(
            "Scan complete",
            duration_seconds=scan_time,
            opportunities_found=len(opportunities),
        )

        # Send summary to Discord
        if self.discord_bot:
            await self.discord_bot.send_scan_summary(opportunities, scan_time)

        return opportunities

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

    async def _store_opportunity(self, result: ArbitrageResult) -> Optional[str]:
        """Store an opportunity in the database.

        Returns:
            The opportunity ID as a string, or None if storage failed.
        """
        async with async_session_factory() as session:
            try:
                # Look up market IDs
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
                    self.logger.warning(
                        "Could not find markets for opportunity",
                        platform_a=result.market_a.platform,
                        platform_b=result.market_b.platform,
                    )
                    return None

                opportunity = Opportunity(
                    opportunity_type="cross_platform",
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


async def run_scanner():
    """Run the scanner as a scheduled job."""
    scanner = ArbitrageScanner()
    await scanner.run_scan()


async def main():
    """Main entry point for the scheduler."""
    logger.info("Initializing scheduler")

    # Initialize database
    await init_db()

    # Create scheduler
    scheduler = AsyncIOScheduler()

    # Add scan job
    scheduler.add_job(
        run_scanner,
        trigger=IntervalTrigger(seconds=settings.poll_interval_seconds),
        id="arbitrage_scan",
        name="Arbitrage Scanner",
        replace_existing=True,
    )

    # Start scheduler
    scheduler.start()
    logger.info(
        "Scheduler started",
        interval_seconds=settings.poll_interval_seconds,
    )

    # Run initial scan
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
