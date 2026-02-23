"""PredictIt API collector.

PredictIt API: https://www.predictit.org/api/marketdata/all/
- Public, read-only API
- No authentication required
- Rate limit: 1 request/second
- Updates approximately every 60 seconds
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings


class PredictItCollector(BaseCollector):
    """Collector for PredictIt prediction market API."""

    platform_name = "predictit"

    # Rate limit: 1 request per second
    _last_request_time: float = 0
    _rate_limit_seconds: float = 1.0

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self.client: Optional[httpx.AsyncClient] = None
        self._cached_markets: dict[str, MarketData] = {}
        self._cache_timestamp: Optional[datetime] = None

    async def connect(self) -> None:
        """Initialize HTTP client."""
        self.client = httpx.AsyncClient(
            base_url=self.settings.predictit_api_host,
            timeout=30.0,
        )
        self.logger.info("Connected to PredictIt API")

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _rate_limit(self) -> None:
        """Enforce rate limiting."""
        import time

        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._rate_limit_seconds:
            await asyncio.sleep(self._rate_limit_seconds - elapsed)
        self._last_request_time = time.time()

    def _parse_contract(self, market_data: dict[str, Any], contract: dict[str, Any]) -> MarketData:
        """Parse PredictIt contract into MarketData.

        PredictIt markets can have multiple contracts (e.g., different candidates).
        Each contract is treated as a separate market for arbitrage purposes.
        """
        # Prices are in dollars (0.01 to 0.99)
        yes_price = None
        no_price = None

        if contract.get("lastTradePrice"):
            yes_price = Decimal(str(contract["lastTradePrice"]))
            no_price = Decimal("1") - yes_price

        # Use best buy prices if available (more accurate for arbitrage)
        yes_bid = None
        yes_ask = None
        no_bid = None
        no_ask = None

        if contract.get("bestBuyYesCost"):
            yes_ask = Decimal(str(contract["bestBuyYesCost"]))
        if contract.get("bestSellYesCost"):
            yes_bid = Decimal(str(contract["bestSellYesCost"]))
        if contract.get("bestBuyNoCost"):
            no_ask = Decimal(str(contract["bestBuyNoCost"]))
        if contract.get("bestSellNoCost"):
            no_bid = Decimal(str(contract["bestSellNoCost"]))

        # Derive NO prices from YES if PredictIt didn't provide them directly
        if no_ask is None and yes_bid is not None:
            no_ask = Decimal("1") - yes_bid
        if no_bid is None and yes_ask is not None:
            no_bid = Decimal("1") - yes_ask

        # Parse end date
        end_date = None
        if market_data.get("dateEnd"):
            try:
                end_date = datetime.fromisoformat(
                    market_data["dateEnd"].replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        # Determine status
        status = contract.get("status", "Open").lower()

        # Create unique ID combining market and contract
        market_id = str(market_data.get("id", ""))
        contract_id = str(contract.get("id", ""))
        combined_id = f"{market_id}_{contract_id}"

        return MarketData(
            platform=self.platform_name,
            platform_market_id=combined_id,
            title=f"{market_data.get('name', '')} - {contract.get('name', '')}",
            description=market_data.get("shortName"),
            resolution_criteria=None,  # PredictIt doesn't expose this via API
            category=None,
            end_date=end_date,
            status=status,
            url=market_data.get("url", f"https://www.predictit.org/markets/detail/{market_id}"),
            yes_price=yes_price,
            no_price=no_price,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def fetch_markets(self, category: Optional[str] = None) -> list[MarketData]:
        """Fetch all available markets from PredictIt.

        Note: PredictIt API returns all markets in a single endpoint.
        Category filtering is done client-side since the API doesn't support it.

        Args:
            category: Optional category filter (not supported by API, ignored).

        Returns:
            List of MarketData objects.
        """
        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        await self._rate_limit()

        response = await self.client.get("/api/marketdata/all/")
        response.raise_for_status()
        data = response.json()

        markets = []
        self._cached_markets = {}

        for market in data.get("markets", []):
            for contract in market.get("contracts", []):
                try:
                    market_data = self._parse_contract(market, contract)
                    markets.append(market_data)
                    self._cached_markets[market_data.platform_market_id] = market_data
                except Exception as e:
                    self.logger.warning(
                        "Failed to parse contract",
                        market_id=market.get("id"),
                        contract_id=contract.get("id"),
                        error=str(e),
                    )

        self._cache_timestamp = datetime.utcnow()
        self.logger.info("Fetched markets from PredictIt", count=len(markets))
        return markets

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ID.

        Note: PredictIt doesn't have a single-market endpoint,
        so we return from cache or refresh all markets.

        Args:
            market_id: Combined market_contract ID (e.g., '12345_67890').

        Returns:
            MarketData if found, None otherwise.
        """
        # Check cache first (valid for 60 seconds)
        if self._cache_timestamp:
            age = (datetime.utcnow() - self._cache_timestamp).total_seconds()
            if age < 60 and market_id in self._cached_markets:
                return self._cached_markets[market_id]

        # Refresh cache
        await self.fetch_markets()
        return self._cached_markets.get(market_id)

    async def get_market_by_name(self, search_term: str) -> list[MarketData]:
        """Search markets by name.

        Args:
            search_term: Text to search for in market titles.

        Returns:
            List of matching MarketData objects.
        """
        if not self._cached_markets or self._cache_timestamp is None:
            await self.fetch_markets()

        search_lower = search_term.lower()
        return [
            market
            for market in self._cached_markets.values()
            if search_lower in market.title.lower()
        ]
