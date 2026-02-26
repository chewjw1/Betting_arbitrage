"""IBKR ForecastEx collector using Client Portal Web API.

IBKR's ForecastTrader product provides access to ForecastEx DCM contracts
(regulated prediction markets) and CME event contracts. Zero commission.

ForecastEx contracts are modeled as OPTIONS in IBKR:
- secType="OPT"
- exchange="FORECASTEX" (note: may vary, also try "FORECASTX")
- right="C" for YES positions
- right="P" for NO positions
- Strike represents the threshold (e.g., 0.5 for binary Yes/No)

Client Portal API requires:
1. Running Client Portal Gateway (localhost:5000 by default)
2. Authenticated session (via browser login or IBeam automation)

If Gateway is unavailable, collector gracefully disables itself.

API Docs: https://www.interactivebrokers.com/api/doc.html
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings

# Known ForecastEx symbol roots and their categories
FORECASTEX_SYMBOLS = {
    # Federal Reserve / Interest Rates
    "FF": {"name": "Fed Funds Rate", "category": "Economics"},
    "FOMC": {"name": "FOMC Decision", "category": "Economics"},
    # Inflation
    "CPI": {"name": "Consumer Price Index", "category": "Economics"},
    "CPIM": {"name": "CPI Monthly", "category": "Economics"},
    # Employment
    "UNEMP": {"name": "Unemployment Rate", "category": "Economics"},
    "NFP": {"name": "Non-Farm Payrolls", "category": "Economics"},
    # GDP
    "GDP": {"name": "GDP Growth", "category": "Economics"},
    "GDPQ": {"name": "GDP Quarterly", "category": "Economics"},
    # Elections (when available)
    "PRES": {"name": "Presidential Election", "category": "Politics"},
    "HOUSE": {"name": "House Control", "category": "Politics"},
    "SENATE": {"name": "Senate Control", "category": "Politics"},
}


class IBKRCollector(BaseCollector):
    """Collector for IBKR ForecastEx via Client Portal Web API.

    Requires the Client Portal Gateway to be running and authenticated.
    If Gateway is unavailable, collector gracefully disables itself.
    """

    platform_name = "ibkr"

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self.client: Optional[httpx.AsyncClient] = None
        self._gateway_available = False
        self._account_id: Optional[str] = None
        self._conid_cache: dict[str, int] = {}  # symbol -> conid mapping

    async def connect(self) -> None:
        """Initialize HTTP client and check Gateway availability."""
        self.client = httpx.AsyncClient(
            base_url=self.settings.ibkr_gateway_url,
            timeout=30.0,
            # Gateway uses self-signed cert
            verify=False,
        )

        # Check if Gateway is available and authenticated
        try:
            # Tickle endpoint keeps session alive and returns auth status
            response = await self.client.post("/v1/api/tickle")
            if response.status_code == 200:
                data = response.json()
                if data.get("iserver", {}).get("authStatus", {}).get("authenticated"):
                    self._gateway_available = True
                    self.logger.info("IBKR Gateway connected and authenticated")

                    # Get account ID
                    await self._get_account_id()
                else:
                    self.logger.warning(
                        "IBKR Gateway running but not authenticated. "
                        "Login via browser at https://localhost:5000"
                    )
            else:
                self.logger.warning(
                    "IBKR Gateway not responding",
                    status=response.status_code,
                )
        except httpx.ConnectError:
            self.logger.info(
                "IBKR Gateway not available (not running). "
                "Start Gateway or IBeam to enable ForecastEx collection."
            )
        except Exception as e:
            self.logger.warning(
                "IBKR Gateway check failed",
                error=str(e),
            )

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _get_account_id(self) -> None:
        """Get the user's account ID."""
        if not self._gateway_available:
            return

        try:
            response = await self.client.get("/v1/api/portfolio/accounts")
            if response.status_code == 200:
                accounts = response.json()
                if accounts:
                    self._account_id = accounts[0].get("id")
                    acct_masked = self._account_id[:4] + "***"
                    self.logger.info("IBKR account ID retrieved", account=acct_masked)
        except Exception as e:
            self.logger.warning("Failed to get IBKR account ID", error=str(e))

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def _search_symbol(self, symbol: str) -> list[dict[str, Any]]:
        """Search for ForecastEx contracts by symbol root.

        Args:
            symbol: Symbol root (e.g., "FF", "CPI", "UNEMP")

        Returns:
            List of contract definitions.
        """
        if not self._gateway_available:
            return []

        try:
            # Search for the symbol
            response = await self.client.get(
                "/v1/api/iserver/secdef/search",
                params={"symbol": symbol, "secType": "OPT"},
            )

            if response.status_code == 200:
                results = response.json()
                # Filter for ForecastEx exchange
                forecastex_contracts = [
                    c for c in results
                    if c.get("exchange", "").upper() in ("FORECASTEX", "FORECASTX", "FORECAST")
                    or "forecast" in c.get("description", "").lower()
                ]
                return forecastex_contracts
            else:
                self.logger.debug(
                    "Symbol search returned non-200",
                    symbol=symbol,
                    status=response.status_code,
                )
                return []

        except Exception as e:
            self.logger.warning(
                "Symbol search failed",
                symbol=symbol,
                error=str(e),
            )
            return []

    async def _get_contract_details(self, conid: int) -> Optional[dict[str, Any]]:
        """Get detailed contract information.

        Args:
            conid: IBKR contract ID.

        Returns:
            Contract details dict or None.
        """
        if not self._gateway_available:
            return None

        try:
            response = await self.client.get(
                "/v1/api/iserver/contract/{conid}/info".format(conid=conid)
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self.logger.debug("Contract details fetch failed", conid=conid, error=str(e))

        return None

    async def _get_market_data(self, conids: list[int]) -> dict[int, dict[str, Any]]:
        """Get market data snapshots for multiple contracts.

        Args:
            conids: List of contract IDs.

        Returns:
            Dict mapping conid to market data.
        """
        if not self._gateway_available or not conids:
            return {}

        results = {}

        try:
            # Request market data for batch of conids
            conid_str = ",".join(str(c) for c in conids[:50])  # Max 50 per request

            response = await self.client.get(
                "/v1/api/iserver/marketdata/snapshot",
                params={"conids": conid_str, "fields": "31,84,85,86,87,88"},
            )

            if response.status_code == 200:
                snapshots = response.json()
                for snap in snapshots:
                    conid = snap.get("conid")
                    if conid:
                        results[conid] = snap
            else:
                self.logger.debug(
                    "Market data snapshot failed",
                    status=response.status_code,
                )

        except Exception as e:
            self.logger.warning("Market data fetch failed", error=str(e))

        return results

    async def fetch_markets(self, category: Optional[str] = None) -> list[MarketData]:
        """Fetch all available ForecastEx markets.

        Args:
            category: Optional category filter (Economics, Politics).

        Returns:
            List of MarketData for available ForecastEx contracts.
        """
        if not self._gateway_available:
            self.logger.debug("IBKR Gateway not available, skipping ForecastEx fetch")
            return []

        markets = []
        all_contracts = []

        # Search for all known ForecastEx symbols
        symbols_to_search = list(FORECASTEX_SYMBOLS.keys())
        if category:
            symbols_to_search = [
                s for s, info in FORECASTEX_SYMBOLS.items()
                if info["category"].lower() == category.lower()
            ]

        # Search for contracts (with rate limiting)
        for symbol in symbols_to_search:
            contracts = await self._search_symbol(symbol)
            for contract in contracts:
                contract["_symbol_root"] = symbol
                all_contracts.append(contract)
            await asyncio.sleep(0.1)  # Rate limiting

        if not all_contracts:
            self.logger.info("No ForecastEx contracts found")
            return []

        # Get market data for all contracts
        conids = [c.get("conid") for c in all_contracts if c.get("conid")]
        market_data = await self._get_market_data(conids)

        # Convert to MarketData objects
        for contract in all_contracts:
            conid = contract.get("conid")
            if not conid:
                continue

            symbol_root = contract.get("_symbol_root", "")
            symbol_info = FORECASTEX_SYMBOLS.get(symbol_root, {})

            # Extract contract details
            description = contract.get("description", "")
            strike = contract.get("strike")
            expiry = contract.get("expiry")  # Format: YYYYMMDD
            right = contract.get("right", "").upper()  # C or P

            # Build title from contract info
            title = description or f"{symbol_info.get('name', symbol_root)}"
            if strike is not None:
                title += f" > {strike}"
            if expiry:
                title += f" (exp {expiry})"

            # Parse expiry date
            end_date = None
            if expiry:
                try:
                    end_date = datetime.strptime(str(expiry), "%Y%m%d")
                except ValueError:
                    pass

            # Get prices from market data
            md = market_data.get(conid, {})

            # IBKR snapshot fields:
            # 31 = Last Price
            # 84 = Bid
            # 85 = Ask
            # 86 = Bid Size
            # 87 = Ask Size
            # 88 = Volume

            last_price = md.get("31")
            bid = md.get("84")
            ask = md.get("85")
            bid_size = md.get("86")
            ask_size = md.get("87")
            volume = md.get("88")

            # ForecastEx prices are typically 0-100 (cents) or 0-1 (dollars)
            # Normalize to 0-1 range
            def normalize_price(p):
                if p is None:
                    return None
                p = Decimal(str(p))
                if p > 1:
                    p = p / 100  # Convert from cents to dollars
                return p

            yes_price = normalize_price(last_price)
            yes_bid = normalize_price(bid)
            yes_ask = normalize_price(ask)

            # For ForecastEx, NO price is typically 1 - YES price
            no_price = Decimal("1") - yes_price if yes_price is not None else None
            no_bid = Decimal("1") - yes_ask if yes_ask is not None else None
            no_ask = Decimal("1") - yes_bid if yes_bid is not None else None

            # If this is a PUT contract, swap YES/NO
            if right == "P":
                yes_price, no_price = no_price, yes_price
                yes_bid, no_bid = no_bid, yes_bid
                yes_ask, no_ask = no_ask, yes_ask

            market = MarketData(
                platform=self.platform_name,
                platform_market_id=str(conid),
                title=title,
                description=description,
                category=symbol_info.get("category", "Economics"),
                end_date=end_date,
                status="open",
                url="https://www.interactivebrokers.com/en/trading/forecasttrader.php",
                yes_price=yes_price,
                no_price=no_price,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                yes_bid_size=Decimal(str(bid_size)) if bid_size else None,
                yes_ask_size=Decimal(str(ask_size)) if ask_size else None,
                total_volume=Decimal(str(volume)) if volume else None,
            )
            markets.append(market)

        self.logger.info(
            "Fetched ForecastEx markets",
            count=len(markets),
            symbols_searched=len(symbols_to_search),
        )

        return markets

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific ForecastEx contract by conid.

        Args:
            market_id: IBKR contract ID (conid).

        Returns:
            MarketData if found.
        """
        if not self._gateway_available:
            return None

        try:
            conid = int(market_id)
        except ValueError:
            return None

        # Get contract details
        details = await self._get_contract_details(conid)
        if not details:
            return None

        # Get market data
        market_data = await self._get_market_data([conid])
        md = market_data.get(conid, {})

        # Build MarketData (simplified version)
        description = details.get("description", details.get("symbol", ""))

        last_price = md.get("31")
        bid = md.get("84")
        ask = md.get("85")

        def normalize_price(p):
            if p is None:
                return None
            p = Decimal(str(p))
            if p > 1:
                p = p / 100
            return p

        yes_price = normalize_price(last_price)
        yes_bid = normalize_price(bid)
        yes_ask = normalize_price(ask)
        no_price = Decimal("1") - yes_price if yes_price is not None else None

        return MarketData(
            platform=self.platform_name,
            platform_market_id=market_id,
            title=description,
            description=description,
            category="Economics",
            status="open",
            url="https://www.interactivebrokers.com/en/trading/forecasttrader.php",
            yes_price=yes_price,
            no_price=no_price,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
        )
