"""Polymarket API collector.

Polymarket Documentation: https://docs.polymarket.com/
- Gamma API: Market metadata and indexing
- CLOB API: Order book and trading
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings


class PolymarketCollector(BaseCollector):
    """Collector for Polymarket prediction market API."""

    platform_name = "polymarket"

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self.gamma_client: Optional[httpx.AsyncClient] = None
        self.clob_client: Optional[httpx.AsyncClient] = None

    async def connect(self) -> None:
        """Initialize HTTP clients."""
        # Gamma API for market metadata
        self.gamma_client = httpx.AsyncClient(
            base_url=self.settings.polymarket_gamma_host,
            timeout=30.0,
        )

        # CLOB API for order book data
        self.clob_client = httpx.AsyncClient(
            base_url=self.settings.polymarket_api_host,
            timeout=30.0,
        )

        self.logger.info("Connected to Polymarket APIs")

    async def disconnect(self) -> None:
        """Close HTTP clients."""
        if self.gamma_client:
            await self.gamma_client.aclose()
            self.gamma_client = None
        if self.clob_client:
            await self.clob_client.aclose()
            self.clob_client = None

    def _parse_gamma_market(self, data: dict[str, Any]) -> MarketData:
        """Parse Gamma API market response into MarketData."""
        # Extract prices from outcomes
        yes_price = None
        no_price = None

        outcomes = data.get("outcomes", [])
        if isinstance(outcomes, list) and len(outcomes) >= 2:
            # Polymarket uses outcome prices as decimals
            for outcome in outcomes:
                # Skip if outcome is not a dict
                if not isinstance(outcome, dict):
                    continue
                outcome_name = outcome.get("outcome", "").lower()
                if outcome_name == "yes":
                    if outcome.get("price"):
                        yes_price = Decimal(str(outcome["price"]))
                elif outcome_name == "no":
                    if outcome.get("price"):
                        no_price = Decimal(str(outcome["price"]))

        # Fall back to outcomePrices if available
        if yes_price is None and data.get("outcomePrices"):
            prices = data["outcomePrices"]
            # outcomePrices might be a JSON string or a list
            if isinstance(prices, str):
                import json
                try:
                    prices = json.loads(prices)
                except (json.JSONDecodeError, TypeError):
                    prices = []
            if isinstance(prices, list) and len(prices) >= 1:
                yes_price = Decimal(str(prices[0]))
            if isinstance(prices, list) and len(prices) >= 2:
                no_price = Decimal(str(prices[1]))

        # Parse end date
        end_date = None
        if data.get("endDate"):
            try:
                end_date = datetime.fromisoformat(
                    data["endDate"].replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        # Determine status
        status = "open"
        if data.get("closed"):
            status = "closed"
        elif data.get("resolved"):
            status = "resolved"

        # Construct URL - slug is the URL-friendly identifier
        # conditionId is a hex string that won't work in URLs
        slug = data.get("slug")
        if slug:
            url = f"https://polymarket.com/event/{slug}"
        else:
            # Fall back to searching by title on Polymarket
            url = "https://polymarket.com"

        return MarketData(
            platform=self.platform_name,
            platform_market_id=data.get("conditionId", data.get("id", "")),
            title=data.get("question", data.get("title", "")),
            description=data.get("description"),
            resolution_criteria=data.get("resolutionSource"),
            category=data.get("category"),
            end_date=end_date,
            status=status,
            url=url,
            yes_price=yes_price,
            no_price=no_price,
            total_volume=Decimal(str(data["volume"])) if data.get("volume") else None,
        )

    def _parse_clob_market(self, data: dict[str, Any]) -> dict:
        """Parse CLOB API market response for order book data."""
        return {
            "condition_id": data.get("condition_id"),
            "tokens": data.get("tokens", []),
            "min_size": data.get("minimum_order_size"),
            "min_tick": data.get("minimum_tick_size"),
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def fetch_markets(
        self,
        category: Optional[str] = None,
        max_markets: int = 1000,
    ) -> list[MarketData]:
        """Fetch available markets from Polymarket.

        Args:
            category: Optional category filter.
            max_markets: Maximum markets to fetch (default 1000 to avoid overwhelming DB).

        Returns:
            List of MarketData objects.
        """
        if not self.gamma_client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        markets = []
        offset = 0
        limit = 100

        while len(markets) < max_markets:
            params = {
                "limit": limit,
                "offset": offset,
                "active": True,
                "closed": False,
            }
            if category:
                params["category"] = category

            response = await self.gamma_client.get("/markets", params=params)
            response.raise_for_status()
            data = response.json()

            if not data:
                break

            for market in data:
                # Skip if market is a string (just a condition ID) instead of a dict
                if isinstance(market, str):
                    continue
                if not isinstance(market, dict):
                    continue

                try:
                    parsed = self._parse_gamma_market(market)
                    # Only add markets with valid prices
                    if parsed.yes_price is not None and parsed.yes_price > 0:
                        markets.append(parsed)
                except Exception as e:
                    market_id = market.get("conditionId", "unknown") if isinstance(market, dict) else str(market)[:20]
                    self.logger.warning(
                        "Failed to parse market",
                        market_id=market_id,
                        error=str(e),
                    )

            if len(data) < limit:
                break

            offset += limit

        self.logger.info("Fetched markets from Polymarket", count=len(markets))
        return markets

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by condition ID.

        Args:
            market_id: Polymarket condition ID.

        Returns:
            MarketData if found, None otherwise.
        """
        if not self.gamma_client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        try:
            response = await self.gamma_client.get(f"/markets/{market_id}")
            response.raise_for_status()
            data = response.json()
            return self._parse_gamma_market(data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def fetch_orderbook(self, token_id: str) -> dict:
        """Fetch orderbook for a specific token.

        Args:
            token_id: Polymarket token ID (YES or NO token).

        Returns:
            Orderbook data with bids and asks.
        """
        if not self.clob_client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        response = await self.clob_client.get(f"/book", params={"token_id": token_id})
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def fetch_price(self, token_id: str) -> Optional[Decimal]:
        """Fetch current midpoint price for a token.

        Args:
            token_id: Polymarket token ID.

        Returns:
            Current price as Decimal, or None if unavailable.
        """
        if not self.clob_client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        try:
            response = await self.clob_client.get(f"/midpoint", params={"token_id": token_id})
            response.raise_for_status()
            data = response.json()
            if data.get("mid"):
                return Decimal(str(data["mid"]))
            return None
        except Exception as e:
            self.logger.warning("Failed to fetch price", token_id=token_id, error=str(e))
            return None
