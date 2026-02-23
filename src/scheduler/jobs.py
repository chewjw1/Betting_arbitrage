"""Scheduled jobs for data collection and arbitrage detection.

This module runs automatic scans at configurable intervals:
- API platforms (Kalshi, Polymarket, PredictIt, DraftKings): every 10 minutes (default)
- Detects both cross-platform AND logical arbitrage
- Uses LLM validation to filter false positive matches (if OPENAI_API_KEY set)
- Only notifies Discord for NEW opportunities (permanent deduplication via database)
- Publishes opportunities to Redis/queue for Discord bot delivery
- Stores opportunities in database for dashboard viewing

Usage:
    python -m src.scheduler.jobs

Or via Docker:
    docker-compose up collector
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
from src.notifications.redis_bridge import OpportunityPublisher

logger = structlog.get_logger()

# Try to import dashboard health update (optional, won't fail if API not running)
try:
    from src.api.routes.health import update_scan_status
except ImportError:
    def update_scan_status(*args, **kwargs):
        pass  # API not available
settings = get_settings()


class ArbitrageScanner:
    """Main scanner that coordinates data collection and arbitrage detection."""

    def __init__(
        self,
        publisher: Optional[OpportunityPublisher] = None,
        min_net_spread_pct: Optional[float] = None,
    ):
        """Initialize scanner.

        Args:
            publisher: Redis publisher for sending opportunities to Discord bot.
            min_net_spread_pct: Override minimum profit threshold.
        """
        self.publisher = publisher
        self.min_net_spread_pct = min_net_spread_pct or settings.min_net_spread_pct
        self.logger = logger.bind(component="ArbitrageScanner")
        self._scan_count = 0  # Track scans for periodic price history logging

        # LLM validator for cross-platform matching (optional)
        llm_validator = None
        if settings.llm_validation_enabled and settings.openai_api_key:
            from src.matching.llm_validator import LLMMatchValidator
            llm_validator = LLMMatchValidator(
                api_key=settings.openai_api_key,
                model=settings.llm_model,
            )
            self.logger.info("LLM match validation enabled", model=settings.llm_model)

        # Cross-platform detector
        self.cross_platform_detector = CrossPlatformDetector(
            min_net_spread_pct=self.min_net_spread_pct,
            min_match_confidence=settings.min_match_confidence,
            default_position_size=settings.max_position_size,
            llm_validator=llm_validator,
        )

        # Logical arbitrage detector
        self.logical_detector = LogicalArbitrageDetector(
            min_violation_pct=settings.min_logical_violation_pct,
            min_net_profit_pct=self.min_net_spread_pct,
            min_relationship_confidence=settings.min_match_confidence,
        )

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

    async def run_scan(self) -> dict:
        """Run scan across all API platforms.

        Returns:
            Dict with scan results.
        """
        start_time = time.time()
        self.logger.info("Starting scan")

        # All collectors are API-based now
        collectors = {
            "kalshi": KalshiCollector(),
            "polymarket": PolymarketCollector(),
            "predictit": PredictItCollector(),
            "draftkings": DraftKingsCollector(include_sports=False),
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

        # Process and detect opportunities
        return await self._process_markets(markets_by_platform, start_time)

    async def _process_markets(
        self,
        markets_by_platform: dict[str, list[MarketData]],
        start_time: float,
    ) -> dict:
        """Process collected markets: detect opportunities, store, notify.

        Args:
            markets_by_platform: Collected market data.
            start_time: Scan start time.

        Returns:
            Dict with results.
        """
        # --- Cross-Platform Arbitrage Detection ---
        cross_platform_opps = await self.cross_platform_detector.find_opportunities(
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

        # Store opportunities in database and publish to Discord only for NEW ones
        notifications_sent = 0
        notifications_skipped = 0

        for opp in cross_platform_opps:
            opportunity_id, is_new = await self._store_opportunity(opp, "cross_platform")

            # Only notify on first detection — never re-alert for existing opportunities
            if opportunity_id and is_new:
                if self.publisher:
                    await self.publisher.publish(opp, opportunity_id, "cross_platform")
                notifications_sent += 1
            elif opportunity_id:
                notifications_skipped += 1

        for opp in logical_opps:
            opportunity_id, is_new = await self._store_logical_opportunity(opp)

            if opportunity_id and is_new:
                if self.publisher:
                    await self.publisher.publish(opp, opportunity_id, "logical")
                notifications_sent += 1
            elif opportunity_id:
                notifications_skipped += 1

        scan_time = time.time() - start_time

        # Update dashboard health status
        total_opps = len(cross_platform_opps) + len(logical_opps)
        update_scan_status("api", markets_by_platform, total_opps)

        self.logger.info(
            "Scan complete",
            duration_seconds=round(scan_time, 2),
            cross_platform_found=len(cross_platform_opps),
            logical_found=len(logical_opps),
            notifications_sent=notifications_sent,
            notifications_skipped=notifications_skipped,
            total_markets=sum(len(m) for m in markets_by_platform.values()),
        )

        return {
            "cross_platform": cross_platform_opps,
            "logical": logical_opps,
            "notifications_sent": notifications_sent,
            "notifications_skipped": notifications_skipped,
            "scan_time_seconds": scan_time,
            "markets_by_platform": {
                p: len(m) for p, m in markets_by_platform.items()
            },
        }

    async def _get_or_create_market(
        self,
        session,
        market_data: MarketData,
    ) -> Market:
        """Get existing market or create new one."""
        query = select(Market).where(
            Market.platform == market_data.platform,
            Market.platform_market_id == market_data.platform_market_id,
        )
        result = await session.execute(query)
        market = result.scalar_one_or_none()

        if not market:
            market = Market(
                platform=market_data.platform,
                platform_market_id=market_data.platform_market_id,
                title=market_data.title,
                description=market_data.description,
                url=market_data.url,
                status="open",
            )
            session.add(market)
            await session.flush()

        return market

    async def _store_opportunity(
        self,
        result: ArbitrageResult,
        opportunity_type: str = "cross_platform",
    ) -> tuple[Optional[str], bool]:
        """Store or update a cross-platform opportunity in the database.

        If the same opportunity (same markets) already exists and is active,
        updates it instead of creating a duplicate.

        Returns:
            Tuple of (opportunity_id, is_new) — is_new is True only for first detection.
        """
        async with async_session_factory() as session:
            try:
                # Get or create markets (only stores markets involved in opportunities)
                market_a = await self._get_or_create_market(session, result.market_a)
                market_b = await self._get_or_create_market(session, result.market_b)

                # Check for existing active opportunity with same markets
                existing_query = select(Opportunity).where(
                    Opportunity.market_a_id == market_a.id,
                    Opportunity.market_b_id == market_b.id,
                    Opportunity.opportunity_type == opportunity_type,
                    Opportunity.status == "active",
                )
                existing_result = await session.execute(existing_query)
                opportunity = existing_result.scalar_one_or_none()

                is_new = opportunity is None

                if opportunity:
                    # Update existing opportunity - track persistence
                    opportunity.side_a = result.side_a
                    opportunity.price_a = result.price_a
                    opportunity.side_b = result.side_b
                    opportunity.price_b = result.price_b
                    opportunity.gross_spread = result.gross_spread
                    opportunity.estimated_fees = result.total_fees
                    opportunity.net_profit_pct = result.net_profit_pct
                    opportunity.position_size = result.position_size
                    opportunity.detected_at = datetime.utcnow()
                    opportunity.times_seen = (opportunity.times_seen or 1) + 1
                    if opportunity.times_seen >= settings.spread_persistence_threshold:
                        opportunity.spread_persistent = True
                else:
                    # Create new opportunity
                    now = datetime.utcnow()
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
                        first_detected_at=now,
                        times_seen=1,
                    )
                    session.add(opportunity)

                await session.commit()
                return str(opportunity.id), is_new

            except Exception as e:
                self.logger.error("Failed to store opportunity", error=str(e))
                return None, False

    async def _store_logical_opportunity(self, result) -> tuple[Optional[str], bool]:
        """Store or update a logical arbitrage opportunity in the database.

        Returns:
            Tuple of (opportunity_id, is_new) — is_new is True only for first detection.
        """
        async with async_session_factory() as session:
            try:
                rel = result.relationship
                market_a_data = rel.market_a
                market_b_data = rel.market_b
                opp_type = f"logical_{rel.relationship_type.value}"

                # Get or create markets
                market_a_db = await self._get_or_create_market(session, market_a_data)

                # For same-market logical arbitrage (complement), both are the same
                if market_b_data.platform_market_id == market_a_data.platform_market_id:
                    market_b_db = market_a_db
                else:
                    market_b_db = await self._get_or_create_market(session, market_b_data)

                # Check for existing active opportunity with same markets
                existing_query = select(Opportunity).where(
                    Opportunity.market_a_id == market_a_db.id,
                    Opportunity.market_b_id == market_b_db.id,
                    Opportunity.opportunity_type == opp_type,
                    Opportunity.status == "active",
                )
                existing_result = await session.execute(existing_query)
                opportunity = existing_result.scalar_one_or_none()

                is_new = opportunity is None

                # Use prices from the detector result (not raw market data)
                # The detector computes the correct price for each side
                price_a = result.price_a if result.price_a is not None else Decimal("0")
                price_b = result.price_b if result.price_b is not None else Decimal("0")

                if opportunity:
                    # Update existing
                    opportunity.side_a = result.side_a or "yes"
                    opportunity.price_a = price_a
                    opportunity.side_b = result.side_b or "no"
                    opportunity.price_b = price_b
                    opportunity.gross_spread = result.violation_amount
                    opportunity.estimated_fees = result.estimated_fees
                    opportunity.net_profit_pct = result.net_profit_pct
                    opportunity.detected_at = datetime.utcnow()
                    opportunity.times_seen = (opportunity.times_seen or 1) + 1
                    if opportunity.times_seen >= settings.spread_persistence_threshold:
                        opportunity.spread_persistent = True
                else:
                    # Create new
                    now = datetime.utcnow()
                    opportunity = Opportunity(
                        opportunity_type=opp_type,
                        opportunity_subtype=result.subtype,
                        opportunity_subtype_display=result.subtype_display,
                        platform_a=market_a_data.platform,
                        market_a_id=market_a_db.id,
                        side_a=result.side_a or "yes",
                        price_a=price_a,
                        platform_b=market_b_data.platform,
                        market_b_id=market_b_db.id,
                        side_b=result.side_b or "no",
                        price_b=price_b,
                        gross_spread=result.violation_amount,
                        estimated_fees=result.estimated_fees,
                        net_profit_pct=result.net_profit_pct,
                        position_size=Decimal(str(settings.max_position_size)),
                        first_detected_at=now,
                        times_seen=1,
                    )
                    session.add(opportunity)

                await session.commit()
                return str(opportunity.id), is_new

            except Exception as e:
                self.logger.error("Failed to store logical opportunity", error=str(e))
                return None, False

# Global scanner instance
_scanner: Optional[ArbitrageScanner] = None
_publisher: Optional[OpportunityPublisher] = None


async def run_scan():
    """Run scan (called every interval)."""
    global _scanner, _publisher
    if _scanner is None:
        _publisher = OpportunityPublisher()
        await _publisher.connect()
        _scanner = ArbitrageScanner(publisher=_publisher)
    await _scanner.run_scan()


async def main():
    """Main entry point for the scheduler.

    Runs scans at configurable intervals (default 10 minutes).
    All platforms are API-based - no browser scraping required.
    Publishes opportunities to Redis for the Discord bot to deliver.
    """
    global _scanner, _publisher

    logger.info("Initializing arbitrage scanner scheduler")

    # Initialize database
    await init_db()

    # Initialize Redis publisher for Discord notifications
    _publisher = OpportunityPublisher()
    await _publisher.connect()

    # Create scanner with publisher
    _scanner = ArbitrageScanner(publisher=_publisher)

    logger.info(
        "Scanner configured",
        scan_interval=settings.api_poll_interval_seconds,
        notification_cooldown=settings.notification_cooldown_seconds,
        min_profit_threshold=settings.min_net_spread_pct,
        llm_validation=settings.llm_validation_enabled and bool(settings.openai_api_key),
        redis_notifications=True,
    )

    # Create scheduler
    scheduler = AsyncIOScheduler()

    # Single scan job (all API-based)
    scheduler.add_job(
        run_scan,
        trigger=IntervalTrigger(seconds=settings.api_poll_interval_seconds),
        id="arbitrage_scan",
        name="Arbitrage Scanner",
        replace_existing=True,
        max_instances=1,
    )

    # Start scheduler
    scheduler.start()
    logger.info(
        "Scheduler started",
        scan_interval=f"{settings.api_poll_interval_seconds}s",
    )

    # Run initial scan immediately
    logger.info("Running initial scan...")
    await run_scan()

    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        if _publisher:
            await _publisher.close()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
