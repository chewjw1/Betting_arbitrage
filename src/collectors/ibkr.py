"""IBKR ForecastEx/CME collector with Client Portal Gateway support.

IBKR provides access to two types of prediction markets:
1. ForecastEx DCM contracts (Fed Funds, PCE, Recession, Climate)
2. CME Event Contracts (Bitcoin, Gold, S&P) - requires Client Portal Gateway

Zero commission on all contracts - excellent for arbitrage.

Data sources:
- Public ForecastEx API: No auth required, limited markets
- Client Portal Gateway: Requires authentication, full CME access

Setup for Gateway:
1. Run IBKR Gateway on seedbox: ~/workspaces/ibkr-gateway/bin/run-minimal.sh
2. SSH tunnel from local: ssh -L 5000:localhost:5000 user@seedbox
3. Log in via browser at https://localhost:5000
4. Gateway stays authenticated for ~24h
"""

from decimal import Decimal, InvalidOperation
from typing import Optional
import ssl

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings

settings = get_settings()

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
    """Collector for IBKR ForecastEx and CME Event Contracts.

    Uses two data sources:
    1. Public ForecastEx API (always available)
    2. Client Portal Gateway API (when authenticated)

    Zero commission makes IBKR ideal for arbitrage opportunities.
    """

    platform_name = "ibkr"

    def __init__(self):
        super().__init__()
        self.client: Optional[httpx.AsyncClient] = None
        self.gateway_client: Optional[httpx.AsyncClient] = None
        self.gateway_authenticated = False

    async def connect(self) -> None:
        """Initialize HTTP clients for both APIs."""
        # Public API client
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )

        # Gateway client (for CME contracts when authenticated)
        if settings.ibkr_gateway_enabled:
            # Create SSL context that doesn't verify (gateway uses self-signed cert)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            self.gateway_client = httpx.AsyncClient(
                base_url=settings.ibkr_gateway_url,
                timeout=30.0,
                verify=False,  # Gateway uses self-signed SSL
                headers={
                    "Accept": "application/json",
                    "User-Agent": "IBKR-Arbitrage/1.0",
                },
            )

            # Check if gateway is authenticated
            await self._check_gateway_auth()

        self.logger.info(
            "IBKR collector initialized",
            gateway_enabled=settings.ibkr_gateway_enabled,
            gateway_authenticated=self.gateway_authenticated,
        )

    async def _check_gateway_auth(self) -> bool:
        """Check if the Client Portal Gateway is authenticated."""
        if not self.gateway_client:
            return False

        try:
            response = await self.gateway_client.get("/v1/api/iserver/auth/status")
            if response.status_code == 200:
                data = response.json()
                self.gateway_authenticated = data.get("authenticated", False)
                if self.gateway_authenticated:
                    self.logger.info("IBKR Gateway authenticated - CME contracts available")
                else:
                    self.logger.debug("IBKR Gateway not authenticated - using public API only")
            else:
                self.gateway_authenticated = False
        except Exception as e:
            self.logger.debug("Gateway auth check failed", error=str(e))
            self.gateway_authenticated = False

        return self.gateway_authenticated

    async def disconnect(self) -> None:
        """Close HTTP clients."""
        if self.client:
            await self.client.aclose()
            self.client = None
        if self.gateway_client:
            await self.gateway_client.aclose()
            self.gateway_client = None

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

    async def _fetch_cme_events(self) -> list[dict]:
        """Fetch CME Event Contracts via the Client Portal Gateway.

        Requires authenticated gateway session.

        Returns:
            List of CME event contract data.
        """
        if not self.gateway_client or not self.gateway_authenticated:
            return []

        try:
            # Get category tree for event contracts
            response = await self.gateway_client.get("/v1/api/trsrv/event/category-tree")

            if response.status_code != 200:
                self.logger.debug(
                    "CME events endpoint returned non-200",
                    status=response.status_code,
                )
                return []

            data = response.json()
            events = []

            # Extract events from category tree
            for category in data.get("categories", []):
                category_name = category.get("name", "")
                for event in category.get("events", []):
                    event["category"] = category_name
                    events.append(event)

            self.logger.debug("Fetched CME events", count=len(events))
            return events

        except Exception as e:
            self.logger.debug("CME events fetch failed", error=str(e))
            return []

    async def _fetch_cme_quotes(self, conids: list[str]) -> dict[str, dict]:
        """Fetch real-time quotes for CME contracts.

        Args:
            conids: List of contract IDs to fetch quotes for.

        Returns:
            Dict mapping conid -> quote data.
        """
        if not self.gateway_client or not self.gateway_authenticated or not conids:
            return {}

        try:
            # Batch request for quotes
            conid_str = ",".join(conids[:50])  # Max 50 per request
            response = await self.gateway_client.get(
                f"/v1/api/md/snapshot",
                params={"conids": conid_str},
            )

            if response.status_code != 200:
                return {}

            data = response.json()
            quotes = {}
            for quote in data:
                conid = str(quote.get("conid", ""))
                if conid:
                    quotes[conid] = quote

            return quotes

        except Exception as e:
            self.logger.debug("CME quotes fetch failed", error=str(e))
            return {}

    async def fetch_markets(self, category: Optional[str] = None) -> list[MarketData]:
        """Fetch all available IBKR prediction markets.

        Combines:
        1. ForecastEx contracts (public API)
        2. CME Event Contracts (gateway, if authenticated)

        Args:
            category: Optional category filter (Economics, Politics, Climate).

        Returns:
            List of MarketData for available contracts.
        """
        markets = []

        # Always fetch ForecastEx (public, no auth needed)
        forecastex_data = await self._fetch_forecastex_data()
        for key, contract in forecastex_data.items():
            market = self._forecastex_to_market_data(key, contract)
            if market:
                if category and market.category.lower() != category.lower():
                    continue
                markets.append(market)

        # Fetch CME events if gateway is authenticated
        if self.gateway_authenticated:
            cme_events = await self._fetch_cme_events()
            conids = [str(e.get("conid")) for e in cme_events if e.get("conid")]

            # Get quotes for CME contracts
            quotes = await self._fetch_cme_quotes(conids) if conids else {}

            for event in cme_events:
                market = self._cme_to_market_data(event, quotes)
                if market:
                    if category and market.category.lower() != category.lower():
                        continue
                    markets.append(market)

        self.logger.info(
            "Fetched IBKR markets",
            total=len(markets),
            forecastex=len([m for m in markets if "FCEX" not in (m.platform_market_id or "")]),
            cme=len([m for m in markets if "CME" in (m.platform_market_id or "")]),
        )

        return markets

    def _forecastex_to_market_data(
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

    def _cme_to_market_data(
        self, event: dict, quotes: dict[str, dict]
    ) -> Optional[MarketData]:
        """Convert a CME Event Contract to MarketData.

        Args:
            event: Event data from category tree API.
            quotes: Quote data keyed by conid.

        Returns:
            MarketData or None if conversion fails.
        """
        conid = str(event.get("conid", ""))
        if not conid:
            return None

        title = event.get("name") or event.get("description")
        if not title:
            return None

        # Get quote data if available
        quote = quotes.get(conid, {})

        # CME contracts use last/bid/ask prices
        last_price = quote.get("lastPrice")
        bid_price = quote.get("bidPrice")
        ask_price = quote.get("askPrice")

        try:
            yes_price = Decimal(str(last_price)) / 100 if last_price else None
            yes_bid = Decimal(str(bid_price)) / 100 if bid_price else yes_price
            yes_ask = Decimal(str(ask_price)) / 100 if ask_price else yes_price
        except (ValueError, TypeError, InvalidOperation):
            yes_price = None
            yes_bid = None
            yes_ask = None

        # Calculate NO prices as complement
        no_price = Decimal("1") - yes_price if yes_price else None
        no_bid = Decimal("1") - yes_ask if yes_ask else None  # NO bid = 1 - YES ask
        no_ask = Decimal("1") - yes_bid if yes_bid else None  # NO ask = 1 - YES bid

        # Volume
        volume = quote.get("volume", 0)
        try:
            total_volume = Decimal(str(volume)) if volume else Decimal("0")
        except (ValueError, TypeError, InvalidOperation):
            total_volume = Decimal("0")

        category = event.get("category", "Economics")

        # Map CME category names
        category_map = {
            "Bitcoin": "Crypto",
            "Gold": "Commodities",
            "S&P 500": "Financials",
            "Indices": "Financials",
        }
        category = category_map.get(category, category)

        return MarketData(
            platform=self.platform_name,
            platform_market_id=f"CME-{conid}",  # Prefix to distinguish from ForecastEx
            title=title,
            description=event.get("description", title),
            category=category,
            status="open",
            url="https://www.interactivebrokers.com/en/trading/eventtrader.php",
            yes_price=yes_price,
            no_price=no_price,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            total_volume=total_volume,
        )

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ID.

        Args:
            market_id: IBKR contract ID (conid or CME-conid).

        Returns:
            MarketData if found.
        """
        # Check if it's a CME contract
        if market_id.startswith("CME-"):
            if self.gateway_authenticated:
                cme_events = await self._fetch_cme_events()
                target_conid = market_id.replace("CME-", "")
                for event in cme_events:
                    if str(event.get("conid")) == target_conid:
                        quotes = await self._fetch_cme_quotes([target_conid])
                        return self._cme_to_market_data(event, quotes)
            return None

        # ForecastEx contract
        data = await self._fetch_forecastex_data()
        for key, contract in data.items():
            if contract.get("underConid") == market_id:
                return self._forecastex_to_market_data(key, contract)

        return None
