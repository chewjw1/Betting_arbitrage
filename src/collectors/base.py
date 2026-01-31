"""Base collector interface for all prediction market platforms."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

import structlog

logger = structlog.get_logger()


@dataclass
class MarketData:
    """Standardized market data from any platform."""

    platform: str
    platform_market_id: str
    title: str
    description: Optional[str] = None
    resolution_criteria: Optional[str] = None
    category: Optional[str] = None
    end_date: Optional[datetime] = None
    status: str = "open"
    url: Optional[str] = None

    # Current prices (0.0 to 1.0)
    yes_price: Optional[Decimal] = None
    no_price: Optional[Decimal] = None

    # Order book data
    yes_bid: Optional[Decimal] = None
    yes_ask: Optional[Decimal] = None
    no_bid: Optional[Decimal] = None
    no_ask: Optional[Decimal] = None

    # Volume
    yes_volume: Optional[Decimal] = None
    no_volume: Optional[Decimal] = None
    total_volume: Optional[Decimal] = None

    # Metadata
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        """Ensure prices are Decimals."""
        for attr in [
            "yes_price",
            "no_price",
            "yes_bid",
            "yes_ask",
            "no_bid",
            "no_ask",
            "yes_volume",
            "no_volume",
            "total_volume",
        ]:
            value = getattr(self, attr)
            if value is not None and not isinstance(value, Decimal):
                setattr(self, attr, Decimal(str(value)))


class BaseCollector(ABC):
    """Abstract base class for platform collectors."""

    platform_name: str = "unknown"

    def __init__(self):
        self.logger = logger.bind(platform=self.platform_name)

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection/authentication with the platform."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close any open connections."""
        pass

    @abstractmethod
    async def fetch_markets(self, category: Optional[str] = None) -> list[MarketData]:
        """Fetch all available markets from the platform.

        Args:
            category: Optional category filter.

        Returns:
            List of MarketData objects.
        """
        pass

    @abstractmethod
    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ID.

        Args:
            market_id: Platform-specific market identifier.

        Returns:
            MarketData if found, None otherwise.
        """
        pass

    async def fetch_prices(self, market_ids: list[str]) -> dict[str, MarketData]:
        """Fetch current prices for multiple markets.

        Args:
            market_ids: List of platform-specific market IDs.

        Returns:
            Dict mapping market_id to MarketData.
        """
        results = {}
        for market_id in market_ids:
            data = await self.fetch_market(market_id)
            if data:
                results[market_id] = data
        return results

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
