"""Price history logging service."""

from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.collectors.base import MarketData
from src.database.models import PriceHistory

logger = structlog.get_logger()


class PriceHistoryService:
    """Service for logging and querying price history."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.logger = logger.bind(component="PriceHistoryService")

    async def log_price(self, market: MarketData) -> PriceHistory:
        """Log a price snapshot to history.

        Args:
            market: Market data to log.

        Returns:
            Created PriceHistory record.
        """
        record = PriceHistory(
            platform=market.platform,
            platform_market_id=market.platform_market_id,
            market_title=market.title,
            yes_price=market.yes_price,
            no_price=market.no_price,
            yes_bid=market.yes_bid,
            yes_ask=market.yes_ask,
            volume_24h=market.volume_24h,
            total_volume=market.total_volume,
            open_interest=market.open_interest,
            recorded_at=datetime.utcnow(),
        )
        self.session.add(record)
        return record

    async def log_prices(self, markets: list[MarketData]) -> int:
        """Log multiple price snapshots.

        Args:
            markets: List of market data to log.

        Returns:
            Number of records created.
        """
        count = 0
        for market in markets:
            if market.yes_price is not None:  # Only log markets with prices
                await self.log_price(market)
                count += 1
        await self.session.flush()
        self.logger.info("Logged price history", count=count)
        return count

    async def get_price_history(
        self,
        platform: str,
        market_id: str,
        since: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[PriceHistory]:
        """Get price history for a specific market.

        Args:
            platform: Platform name.
            market_id: Platform-specific market ID.
            since: Only get records since this time.
            limit: Maximum records to return.

        Returns:
            List of PriceHistory records.
        """
        query = (
            select(PriceHistory)
            .where(PriceHistory.platform == platform)
            .where(PriceHistory.platform_market_id == market_id)
            .order_by(PriceHistory.recorded_at.desc())
            .limit(limit)
        )
        if since:
            query = query.where(PriceHistory.recorded_at >= since)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_recent_prices(
        self,
        platform: Optional[str] = None,
        limit: int = 100,
    ) -> list[PriceHistory]:
        """Get most recent price records.

        Args:
            platform: Optional platform filter.
            limit: Maximum records to return.

        Returns:
            List of recent PriceHistory records.
        """
        query = (
            select(PriceHistory)
            .order_by(PriceHistory.recorded_at.desc())
            .limit(limit)
        )
        if platform:
            query = query.where(PriceHistory.platform == platform)

        result = await self.session.execute(query)
        return list(result.scalars().all())


async def log_all_prices(session: AsyncSession, markets: list[MarketData]) -> int:
    """Convenience function to log prices.

    Args:
        session: Database session.
        markets: Markets to log.

    Returns:
        Number of records created.
    """
    service = PriceHistoryService(session)
    return await service.log_prices(markets)
