"""IBKR ForecastEx collector using the public web API.

IBKR's ForecastTrader provides access to ForecastEx DCM contracts (regulated
prediction markets) with zero commission - excellent for arbitrage.

This collector uses the public API endpoint that powers the ForecastTrader
website, requiring NO authentication for read-only market data.

Public API: https://www.interactivebrokers.com/response_handlers/fcastex/

Categories available:
- Economics: Fed Funds, PCE/Inflation, Recession
- Politics: House/Senate control
- Climate: Temperature records

Note: CME Event Contracts (Bitcoin, Gold, indices) require the Client Portal
Gateway for access - they are not available through this public endpoint.
"""

from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, MarketData

# Public API endpoint - no auth required
FORECASTEX_API_URL = "https://www.interactivebrokers.com/response_handlers/fcastex/"

# Category mapping based on contract keys
CATEGORY_MAP = {
    "recession": "Economics",
    "consumption": "Economics",
    "usfed": "Economics",
    "feddecision": "Economics",
    "climate": "Climate",
    "republican": "Politics",
    "democrat": "Politics",
}


class IBKRCollector(BaseCollector):
    """Collector for IBKR ForecastEx using the public web API.

    No authentication required - uses the same endpoint as the public website.
    Zero commission makes IBKR ideal for arbitrage opportunities.
    """

    platform_name = "ibkr"

    def __init__(self):
        super().__init__()
        self.client: Optional[httpx.AsyncClient] = None

    async def connect(self) -> None:
        """Initialize HTTP client."""
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        self.logger.info("IBKR ForecastEx collector initialized (public API)")

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def _fetch_forecastex_data(self) -> dict:
        """Fetch all ForecastEx contracts from the public API.

        Returns:
            Dict of contract key -> contract data.
        """
        try:
            response = await self.client.get(FORECASTEX_API_URL)

            if response.status_code == 200:
                return response.json()
            else:
                self.logger.warning(
                    "ForecastEx API returned non-200",
                    status=response.status_code,
                )
                return {}

        except Exception as e:
            self.logger.warning("ForecastEx API fetch failed", error=str(e))
            return {}

    async def fetch_markets(self, category: Optional[str] = None) -> list[MarketData]:
        """Fetch all available ForecastEx markets.

        Args:
            category: Optional category filter (Economics, Politics, Climate).

        Returns:
            List of MarketData for available contracts.
        """
        data = await self._fetch_forecastex_data()

        if not data:
            self.logger.debug("No ForecastEx data received")
            return []

        markets = []

        for key, contract in data.items():
            market = self._contract_to_market_data(key, contract)
            if market:
                # Apply category filter if specified
                if category and market.category.lower() != category.lower():
                    continue
                markets.append(market)

        self.logger.info(
            "Fetched IBKR ForecastEx markets",
            count=len(markets),
        )

        return markets

    def _contract_to_market_data(
        self, key: str, contract: dict
    ) -> Optional[MarketData]:
        """Convert a ForecastEx API contract to MarketData.

        Args:
            key: Contract key (e.g., "recession", "usfed").
            contract: Contract data from API.

        Returns:
            MarketData or None if conversion fails.
        """
        # Extract title/question
        title = contract.get("name") or contract.get("question")
        if not title:
            return None

        # Get contract ID (use underlying conid as primary identifier)
        conid = contract.get("underConid")
        if not conid:
            return None

        # Parse YES/NO prices (API returns as strings "0"-"100")
        yes_str = contract.get("yes", "0")
        no_str = contract.get("no", "0")

        try:
            # Prices are in cents (0-100), normalize to 0-1
            yes_price = Decimal(str(yes_str).strip()) / 100
            no_price = Decimal(str(no_str).strip()) / 100
        except (ValueError, TypeError, InvalidOperation):
            yes_price = None
            no_price = None

        # Also check yesQuote/noQuote for real-time prices
        yes_quote = contract.get("yesQuote")
        no_quote = contract.get("noQuote")
        try:
            if yes_quote is not None:
                yq = float(yes_quote)
                if yq > 0:
                    yes_price = Decimal(str(yq)) / 100
        except (ValueError, TypeError, InvalidOperation):
            pass
        try:
            if no_quote is not None:
                nq = float(no_quote)
                if nq > 0:
                    no_price = Decimal(str(nq)) / 100
        except (ValueError, TypeError, InvalidOperation):
            pass

        # Parse open interest/volume
        interest_str = contract.get("interest", "0")
        volume = contract.get("volume", 0)

        try:
            # Interest comes as formatted string like "128,000"
            if isinstance(interest_str, str):
                interest = int(interest_str.replace(",", ""))
            else:
                interest = int(interest_str)
        except (ValueError, TypeError):
            interest = 0

        try:
            if volume:
                total_volume = Decimal(str(volume))
            else:
                total_volume = Decimal(str(interest))
        except (ValueError, TypeError, InvalidOperation):
            total_volume = Decimal("0")

        # Determine category
        category = CATEGORY_MAP.get(key, "Economics")

        # Build URL
        url = "https://forecasttrader.interactivebrokers.com/en/home.php"

        return MarketData(
            platform=self.platform_name,
            platform_market_id=str(conid),
            title=title,
            description=title,
            category=category,
            status="open",
            url=url,
            yes_price=yes_price,
            no_price=no_price,
            # For ForecastEx, bid/ask spread is typically tight
            # Use the prices as both bid and ask (conservative estimate)
            yes_bid=yes_price,
            yes_ask=yes_price,
            no_bid=no_price,
            no_ask=no_price,
            total_volume=total_volume,
        )

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific ForecastEx contract by conid.

        Args:
            market_id: IBKR contract ID (conid).

        Returns:
            MarketData if found.
        """
        data = await self._fetch_forecastex_data()

        for key, contract in data.items():
            if contract.get("underConid") == market_id:
                return self._contract_to_market_data(key, contract)

        return None
