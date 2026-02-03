"""Scheduled jobs for data collection and arbitrage detection.

This module runs automatic scans at configurable intervals:
- API platforms (Kalshi, Polymarket, PredictIt): every 60 seconds (default)
- Scraping platforms (DraftKings, FanDuel, IBKR): every 180 seconds (default)
- Detects both cross-platform AND logical arbitrage
- Deduplicates notifications (won't re-alert for same pair within cooldown)
- Sends Discord notifications for opportunities above threshold
- Stores ALL opportunities in PostgreSQL for dashboard viewing

Usage:
    python -m src.scheduler.jobs

Or via Docker:
    docker-compose up scheduler
"""

import asyncio
from datetime import datetime, timedelta
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
from src.database.price_history import log_all_prices

logger = structlog.get_logger()

# Try to import dashboard health update (optional, won't fail if API not running)
try:
    from src.api.routes.health import update_scan_status
except ImportError:
    def update_scan_status(*args, **kwargs):
        pass  # API not available
settings = get_settings()


class NotificationDeduplicator:
    """Track recently notified opportunities to avoid spam."""

    def __init__(self, cooldown_seconds: int = 300):
        """Initialize deduplicator.

        Args:
            cooldown_seconds: Don't re-notify for same pair within this window.
        """
        self.cooldown_seconds = cooldown_seconds
        # Key: "platform_a:market_id_a:platform_b:market_id_b" -> last_notified_time
        self._notified: dict[str, datetime] = {}
        self.logger = logger.bind(component="Deduplicator")

    def _make_key(self, opp) -> str:
        """Create a unique key for an opportunity."""
        if hasattr(opp, 'market_a'):
            # Cross-platform ArbitrageResult
            parts = sorted([
                f"{opp.market_a.platform}:{opp.market_a.platform_market_id}",
                f"{opp.market_b.platform}:{opp.market_b.platform_market_id}",
            ])
        elif hasattr(opp, 'relationship'):
            # LogicalArbitrageResult
            rel = opp.relationship
            parts = [
                f"{rel.market_a.platform}:{rel.market_a.platform_market_id}",
                f"{rel.relationship_type.value}",
            ]
            if rel.market_b.platform_market_id != rel.market_a.platform_market_id:
                parts.append(f"{rel.market_b.platform}:{rel.market_b.platform_market_id}")
        else:
            return str(hash(str(opp)))

        return ":".join(parts)

    def should_notify(self, opp) -> bool:
        """Check if we should send a notification for this opportunity.

        Args:
            opp: ArbitrageResult or LogicalArbitrageResult.

        Returns:
            True if we should notify, False if recently notified.
        """
        key = self._make_key(opp)
        now = datetime.utcnow()

        # Clean up old entries
        self._cleanup()

        if key in self._notified:
            last_notified = self._notified[key]
            age = (now - last_notified).total_seconds()
            if age < self.cooldown_seconds:
                self.logger.debug(
                    "Skipping duplicate notification",
                    key=key,
                    age_seconds=age,
                )
                return False

        return True

    def mark_notified(self, opp) -> None:
        """Mark an opportunity as notified."""
        key = self._make_key(opp)
        self._notified[key] = datetime.utcnow()
        self.logger.debug("Marked as notified", key=key)

    def _cleanup(self) -> None:
        """Remove expired entries."""
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=self.cooldown_seconds * 2)
        expired = [k for k, v in self._notified.items() if v < cutoff]
        for k in expired:
            del self._notified[k]


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
        self.logger = logger.bind(component="ArbitrageScanner")
        self._scan_count = 0  # Track scans for periodic price history logging

        # Deduplicator for notifications
        self.deduplicator = NotificationDeduplicator(
            cooldown_seconds=settings.notification_cooldown_seconds
        )

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

        # Cached markets from last scrape (used when running API-only scans)
        self._cached_scrape_markets: dict[str, list[MarketData]] = {}
        self._last_scrape_time: Optional[datetime] = None

    async def _collect_from_platform(
        self,
        platform: str,
        collector,
    ) -> tuple[str, list[MarketData]]:
        """Collect markets from a single platform."""
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

    async def run_api_scan(self) -> dict:
        """Run scan for API-based platforms only (fast, every 60s).

        Returns:
            Dict with scan results.
        """
        start_time = time.time()
        self.logger.info("Starting API scan")

        # API collectors only
        collectors = {
            "kalshi": KalshiCollector(),
            "polymarket": PolymarketCollector(),
            "predictit": PredictItCollector(),
        }

        # Collect in parallel
        tasks = [
            self._collect_from_platform(platform, collector)
            for platform, collector in collectors.items()
        ]
        results = await asyncio.gather(*tasks)

        # Build markets dict
        markets_by_platform: dict[str, list[MarketData]] = {}
        for platform, markets in results:
            markets_by_platform[platform] = markets

        # Include cached scrape data if available
        if self._cached_scrape_markets:
            markets_by_platform.update(self._cached_scrape_markets)

        # Store and detect
        return await self._process_markets(markets_by_platform, start_time, "api")

    async def run_scrape_scan(self) -> dict:
        """Run scan for scraping-based platforms (slow, every 3 min).

        Returns:
            Dict with scan results.
        """
        start_time = time.time()
        self.logger.info("Starting scrape scan")

        # Scraping collectors
        collectors = {
            "draftkings": DraftKingsCollector(),
            "fanduel": FanDuelCollector(),
            "ibkr": IBKRCollector(),
        }

        # Collect in parallel
        tasks = [
            self._collect_from_platform(platform, collector)
            for platform, collector in collectors.items()
        ]
        results = await asyncio.gather(*tasks)

        # Build and cache markets
        scrape_markets: dict[str, list[MarketData]] = {}
        for platform, markets in results:
            scrape_markets[platform] = markets

        self._cached_scrape_markets = scrape_markets
        self._last_scrape_time = datetime.utcnow()

        # Combine with API data for full scan
        # First, get fresh API data
        api_collectors = {
            "kalshi": KalshiCollector(),
            "polymarket": PolymarketCollector(),
            "predictit": PredictItCollector(),
        }
        api_tasks = [
            self._collect_from_platform(platform, collector)
            for platform, collector in api_collectors.items()
        ]
        api_results = await asyncio.gather(*api_tasks)

        markets_by_platform = dict(api_results)
        markets_by_platform.update(scrape_markets)

        # Store and detect
        return await self._process_markets(markets_by_platform, start_time, "full")

    async def _process_markets(
        self,
        markets_by_platform: dict[str, list[MarketData]],
        start_time: float,
        scan_type: str,
    ) -> dict:
        """Process collected markets: store, detect, notify.

        Args:
            markets_by_platform: Collected market data.
            start_time: Scan start time.
            scan_type: "api" or "full".

        Returns:
            Dict with results.
        """
        # Store markets and prices in database
        await self._store_market_data(markets_by_platform)

        # --- Cross-Platform Arbitrage Detection ---
        cross_platform_opps = self.cross_platform_detector.find_opportunities(
            markets_by_platform
        )

        # --- Logical Arbitrage Detection ---
        all_markets = []
        for markets in markets_by_platform.values():
            all_markets.extend(markets)

        logical_opps = self.logical_detector.find_opportunities(
            all_markets,
            position_size=Decimal(str(settings.max_position_size)),
        )

        # Store ALL opportunities in database (for dashboard)
        # But only notify if not recently notified
        notifications_sent = 0

        for opp in cross_platform_opps:
            opportunity_id = await self._store_opportunity(opp, "cross_platform")

            # Check deduplication before notifying
            if self.discord_bot and opportunity_id and self.deduplicator.should_notify(opp):
                await self.discord_bot.send_opportunity(opp, opportunity_id)
                self.deduplicator.mark_notified(opp)
                notifications_sent += 1

        for opp in logical_opps:
            opportunity_id = await self._store_logical_opportunity(opp)

            if self.discord_bot and opportunity_id and self.deduplicator.should_notify(opp):
                await self._send_logical_notification(opp, opportunity_id)
                self.deduplicator.mark_notified(opp)
                notifications_sent += 1

        scan_time = time.time() - start_time

        # Update dashboard health status
        total_opps = len(cross_platform_opps) + len(logical_opps)
        update_scan_status(scan_type, markets_by_platform, total_opps)

        self.logger.info(
            "Scan complete",
            scan_type=scan_type,
            duration_seconds=round(scan_time, 2),
            cross_platform_found=len(cross_platform_opps),
            logical_found=len(logical_opps),
            notifications_sent=notifications_sent,
            total_markets=sum(len(m) for m in markets_by_platform.values()),
        )

        return {
            "scan_type": scan_type,
            "cross_platform": cross_platform_opps,
            "logical": logical_opps,
            "notifications_sent": notifications_sent,
            "scan_time_seconds": scan_time,
            "markets_by_platform": {
                p: len(m) for p, m in markets_by_platform.items()
            },
        }

    async def _store_market_data(
        self,
        markets_by_platform: dict[str, list[MarketData]],
    ) -> None:
        """Store collected market data in the database.

        Price history is only logged every 5th scan (~5 min) to avoid
        overwhelming SQLite with 25k+ inserts every 60 seconds.
        """
        self._scan_count += 1
        log_history = (self._scan_count % 5 == 0)  # Every 5th scan

        async with async_session_factory() as session:
            # Only log price history periodically (every ~5 minutes)
            if log_history:
                all_markets = []
                for markets in markets_by_platform.values():
                    all_markets.extend(markets)
                history_count = await log_all_prices(session, all_markets)
                self.logger.info("Logged price history", count=history_count)

            # Store/update markets and latest prices
            for platform, markets in markets_by_platform.items():
                for market_data in markets:
                    try:
                        # Ensure prices are Decimal, not float
                        yes_price = market_data.yes_price
                        no_price = market_data.no_price
                        if isinstance(yes_price, float):
                            yes_price = Decimal(str(yes_price))
                        if isinstance(no_price, float):
                            no_price = Decimal(str(no_price))

                        query = select(Market).where(
                            Market.platform == market_data.platform,
                            Market.platform_market_id == market_data.platform_market_id,
                        )
                        result = await session.execute(query)
                        market = result.scalar_one_or_none()

                        if market:
                            market.title = market_data.title
                            market.description = market_data.description
                            market.status = market_data.status
                            market.end_date = market_data.end_date
                            market.url = market_data.url
                            market.updated_at = datetime.utcnow()
                        else:
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

                        if yes_price is not None:
                            price = Price(
                                market_id=market.id,
                                yes_price=yes_price,
                                no_price=no_price,
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
        """Store a cross-platform opportunity in the database.

        ALL opportunities are stored for dashboard viewing.
        """
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


# Global scanner instance
_scanner: Optional[ArbitrageScanner] = None


async def run_api_scan():
    """Run API-only scan (called every 60s)."""
    global _scanner
    if _scanner is None:
        _scanner = ArbitrageScanner()
    await _scanner.run_api_scan()


async def run_scrape_scan():
    """Run full scan including scraping (called every 180s)."""
    global _scanner
    if _scanner is None:
        _scanner = ArbitrageScanner()
    await _scanner.run_scrape_scan()


async def main():
    """Main entry point for the scheduler.

    Runs two separate schedules:
    - API scan: every 60 seconds (Kalshi, Polymarket, PredictIt)
    - Scrape scan: every 180 seconds (DraftKings, FanDuel, IBKR)
    """
    global _scanner

    logger.info("Initializing arbitrage scanner scheduler")

    # Initialize database
    await init_db()

    # Create scanner
    _scanner = ArbitrageScanner()

    logger.info(
        "Scanner configured",
        api_interval=settings.api_poll_interval_seconds,
        scrape_interval=settings.scrape_poll_interval_seconds,
        notification_cooldown=settings.notification_cooldown_seconds,
        min_profit_threshold=settings.min_net_spread_pct,
    )

    # Create scheduler
    scheduler = AsyncIOScheduler()

    # API scan job (every 60 seconds by default)
    scheduler.add_job(
        run_api_scan,
        trigger=IntervalTrigger(seconds=settings.api_poll_interval_seconds),
        id="api_scan",
        name="API Arbitrage Scanner",
        replace_existing=True,
        max_instances=1,
    )

    # Scrape scan job (every 180 seconds by default)
    scheduler.add_job(
        run_scrape_scan,
        trigger=IntervalTrigger(seconds=settings.scrape_poll_interval_seconds),
        id="scrape_scan",
        name="Scrape Arbitrage Scanner",
        replace_existing=True,
        max_instances=1,
    )

    # Start scheduler
    scheduler.start()
    logger.info(
        "Scheduler started",
        api_interval=f"{settings.api_poll_interval_seconds}s",
        scrape_interval=f"{settings.scrape_poll_interval_seconds}s",
    )

    # Run initial scans immediately
    logger.info("Running initial scrape scan (includes all platforms)...")
    await run_scrape_scan()

    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
