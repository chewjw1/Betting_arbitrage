"""IBKR ForecastEx and CME Event Contracts collector using Client Portal Web API.

IBKR's ForecastTrader product provides access to:
1. ForecastEx DCM contracts (regulated prediction markets) - zero commission
2. CME Event Contracts (daily Bitcoin, Gold, equity index events)

ForecastEx contracts are modeled as OPTIONS in IBKR:
- secType="OPT"
- exchange="FORECASTX"
- right="C" for YES positions, "P" for NO positions
- $1 payout, $0.01 increments

CME Event Contracts are modeled as FUTURES OPTIONS in IBKR:
- secType="FOP"
- exchange="CME" (or COMEX for metals)
- Trading class prefix "EC" (e.g., ECBTC for Bitcoin)
- $20 payout (Bitcoin), $0.25 increments
- Daily expiration

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

# Known ForecastEx symbol roots and their categories (FORECASTX exchange, OPT sectype)
FORECASTEX_SYMBOLS = {
    # Federal Reserve / Interest Rates
    "FF": {"name": "Fed Funds Rate", "category": "Economics"},
    "FOMC": {"name": "FOMC Decision", "category": "Economics"},
    # Inflation
    "CPI": {"name": "Consumer Price Index", "category": "Economics"},
    "CPIM": {"name": "CPI Monthly", "category": "Economics"},
    "PCE": {"name": "Personal Consumption Expenditures", "category": "Economics"},
    # Employment
    "UNEMP": {"name": "Unemployment Rate", "category": "Economics"},
    "NFP": {"name": "Non-Farm Payrolls", "category": "Economics"},
    # GDP
    "GDP": {"name": "GDP Growth", "category": "Economics"},
    "GDPQ": {"name": "GDP Quarterly", "category": "Economics"},
    # Climate
    "TEMP": {"name": "Temperature Record", "category": "Climate"},
    "CO2": {"name": "Atmospheric CO2", "category": "Climate"},
    # Elections (when available, US residents only)
    "PRES": {"name": "Presidential Election", "category": "Politics"},
    "HOUSE": {"name": "House Control", "category": "Politics"},
    "SENATE": {"name": "Senate Control", "category": "Politics"},
}

# CME Event Contract symbols (FOP sectype, various CME exchanges)
# Trading class prefix "EC" + underlying symbol
CME_EVENT_SYMBOLS = {
    # Crypto - daily price events
    "ECBTC": {
        "name": "Bitcoin Daily Price",
        "category": "Crypto",
        "exchange": "CME",
        "underlying": "BTC",
        "payout": Decimal("20"),  # $20 per contract
        "tick": Decimal("0.25"),  # $0.25 increments
    },
    "ECETH": {
        "name": "Ether Daily Price",
        "category": "Crypto",
        "exchange": "CME",
        "underlying": "ETH",
        "payout": Decimal("20"),
        "tick": Decimal("0.25"),
    },
    # Equity indices
    "ECNQ": {
        "name": "Nasdaq-100 Daily",
        "category": "Financials",
        "exchange": "CME",
        "underlying": "NQ",
        "payout": Decimal("20"),
        "tick": Decimal("0.25"),
    },
    "ECES": {
        "name": "S&P 500 E-mini Daily",
        "category": "Financials",
        "exchange": "CME",
        "underlying": "ES",
        "payout": Decimal("20"),
        "tick": Decimal("0.25"),
    },
    # Metals
    "ECGC": {
        "name": "Gold Daily Price",
        "category": "Commodities",
        "exchange": "COMEX",
        "underlying": "GC",
        "payout": Decimal("20"),
        "tick": Decimal("0.25"),
    },
    # Energy
    "ECCL": {
        "name": "Crude Oil Daily",
        "category": "Commodities",
        "exchange": "NYMEX",
        "underlying": "CL",
        "payout": Decimal("20"),
        "tick": Decimal("0.25"),
    },
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
    async def _search_forecastex_symbol(self, symbol: str) -> list[dict[str, Any]]:
        """Search for ForecastEx contracts by symbol root.

        Args:
            symbol: Symbol root (e.g., "FF", "CPI", "UNEMP")

        Returns:
            List of contract definitions.
        """
        if not self._gateway_available:
            return []

        try:
            # Search for the symbol with OPT (options) security type
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
                    "ForecastEx symbol search returned non-200",
                    symbol=symbol,
                    status=response.status_code,
                )
                return []

        except Exception as e:
            self.logger.warning(
                "ForecastEx symbol search failed",
                symbol=symbol,
                error=str(e),
            )
            return []

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def _search_cme_event_symbol(
        self, symbol: str, exchange: str
    ) -> list[dict[str, Any]]:
        """Search for CME Event Contracts by trading class symbol.

        Args:
            symbol: Trading class symbol (e.g., "ECBTC", "ECGC")
            exchange: Exchange (e.g., "CME", "COMEX", "NYMEX")

        Returns:
            List of contract definitions.
        """
        if not self._gateway_available:
            return []

        try:
            # CME event contracts are FOP (futures options)
            # First try searching for the underlying with EC prefix
            response = await self.client.get(
                "/v1/api/iserver/secdef/search",
                params={"symbol": symbol},
            )

            if response.status_code == 200:
                results = response.json()
                # Filter for the expected exchange and event contracts
                cme_contracts = [
                    c for c in results
                    if c.get("exchange", "").upper() == exchange.upper()
                    or "event" in c.get("description", "").lower()
                    or symbol.upper() in c.get("symbol", "").upper()
                ]
                return cme_contracts
            else:
                self.logger.debug(
                    "CME event symbol search returned non-200",
                    symbol=symbol,
                    exchange=exchange,
                    status=response.status_code,
                )
                return []

        except Exception as e:
            self.logger.warning(
                "CME event symbol search failed",
                symbol=symbol,
                exchange=exchange,
                error=str(e),
            )
            return []

    async def _get_strikes_for_contract(
        self, conid: int, exchange: str, sectype: str, month: str
    ) -> dict[str, list[float]]:
        """Get available strikes for an options/event contract.

        Args:
            conid: Underlying contract ID
            exchange: Exchange (FORECASTX, CME, etc.)
            sectype: Security type (OPT or FOP)
            month: Expiry month (e.g., "MAR26")

        Returns:
            Dict with 'call' and 'put' lists of strike prices.
        """
        if not self._gateway_available:
            return {"call": [], "put": []}

        try:
            response = await self.client.get(
                "/v1/api/iserver/secdef/strikes",
                params={
                    "conid": conid,
                    "exchange": exchange,
                    "sectype": sectype,
                    "month": month,
                },
            )

            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self.logger.debug(
                "Strikes fetch failed",
                conid=conid,
                error=str(e),
            )

        return {"call": [], "put": []}

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
        """Fetch all available ForecastEx and CME Event Contract markets.

        Args:
            category: Optional category filter (Economics, Politics, Crypto, etc.).

        Returns:
            List of MarketData for available contracts.
        """
        if not self._gateway_available:
            self.logger.debug("IBKR Gateway not available, skipping event contract fetch")
            return []

        markets = []

        # Fetch both ForecastEx and CME event contracts
        forecastex_markets = await self._fetch_forecastex_markets(category)
        cme_markets = await self._fetch_cme_event_markets(category)

        markets.extend(forecastex_markets)
        markets.extend(cme_markets)

        self.logger.info(
            "Fetched IBKR event contract markets",
            forecastex_count=len(forecastex_markets),
            cme_count=len(cme_markets),
            total=len(markets),
        )

        return markets

    async def _fetch_forecastex_markets(
        self, category: Optional[str] = None
    ) -> list[MarketData]:
        """Fetch ForecastEx DCM contracts.

        Args:
            category: Optional category filter.

        Returns:
            List of MarketData for ForecastEx contracts.
        """
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
            contracts = await self._search_forecastex_symbol(symbol)
            for contract in contracts:
                contract["_symbol_root"] = symbol
                contract["_source"] = "forecastex"
                all_contracts.append(contract)
            await asyncio.sleep(0.1)  # Rate limiting

        if not all_contracts:
            self.logger.debug("No ForecastEx contracts found")
            return []

        # Get market data for all contracts
        conids = [c.get("conid") for c in all_contracts if c.get("conid")]
        market_data = await self._get_market_data(conids)

        # Convert to MarketData objects
        for contract in all_contracts:
            market = self._contract_to_market_data(
                contract, market_data, is_cme=False
            )
            if market:
                markets.append(market)

        return markets

    async def _fetch_cme_event_markets(
        self, category: Optional[str] = None
    ) -> list[MarketData]:
        """Fetch CME Event Contracts (Bitcoin, Gold, indices, etc.).

        Args:
            category: Optional category filter.

        Returns:
            List of MarketData for CME event contracts.
        """
        markets = []
        all_contracts = []

        # Search for all known CME event symbols
        symbols_to_search = list(CME_EVENT_SYMBOLS.items())
        if category:
            symbols_to_search = [
                (s, info) for s, info in CME_EVENT_SYMBOLS.items()
                if info["category"].lower() == category.lower()
            ]

        # Search for contracts (with rate limiting)
        for symbol, info in symbols_to_search:
            contracts = await self._search_cme_event_symbol(
                symbol, info["exchange"]
            )
            for contract in contracts:
                contract["_symbol_root"] = symbol
                contract["_source"] = "cme"
                contract["_cme_info"] = info
                all_contracts.append(contract)
            await asyncio.sleep(0.1)  # Rate limiting

        if not all_contracts:
            self.logger.debug("No CME event contracts found")
            return []

        # Get market data for all contracts
        conids = [c.get("conid") for c in all_contracts if c.get("conid")]
        market_data = await self._get_market_data(conids)

        # Convert to MarketData objects
        for contract in all_contracts:
            market = self._contract_to_market_data(
                contract, market_data, is_cme=True
            )
            if market:
                markets.append(market)

        return markets

    def _contract_to_market_data(
        self,
        contract: dict[str, Any],
        market_data: dict[int, dict[str, Any]],
        is_cme: bool,
    ) -> Optional[MarketData]:
        """Convert an IBKR contract to MarketData.

        Args:
            contract: Contract definition from search.
            market_data: Market data snapshots by conid.
            is_cme: True for CME event contracts, False for ForecastEx.

        Returns:
            MarketData or None if conversion fails.
        """
        conid = contract.get("conid")
        if not conid:
            return None

        symbol_root = contract.get("_symbol_root", "")

        if is_cme:
            symbol_info = CME_EVENT_SYMBOLS.get(symbol_root, {})
            payout = symbol_info.get("payout", Decimal("20"))
        else:
            symbol_info = FORECASTEX_SYMBOLS.get(symbol_root, {})
            payout = Decimal("1")

        # Extract contract details
        description = contract.get("description", "")
        strike = contract.get("strike")
        expiry = contract.get("expiry")  # Format: YYYYMMDD
        right = contract.get("right", "").upper()  # C or P

        # Build title from contract info
        name = symbol_info.get("name", symbol_root)
        title = description or name
        if strike is not None:
            if is_cme:
                # CME event contracts: "Bitcoin > $95,000"
                title = f"{name} > ${strike:,.0f}" if strike >= 100 else f"{name} > {strike}"
            else:
                title = f"{name} > {strike}"
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
        # 31 = Last Price, 84 = Bid, 85 = Ask, 86/87 = Bid/Ask Size, 88 = Volume
        last_price = md.get("31")
        bid = md.get("84")
        ask = md.get("85")
        bid_size = md.get("86")
        ask_size = md.get("87")
        volume = md.get("88")

        def normalize_price(p, contract_payout: Decimal) -> Optional[Decimal]:
            """Normalize price to 0-1 probability range."""
            if p is None:
                return None
            price = Decimal(str(p))
            # CME event contracts: price is in dollars (0-20 for $20 payout)
            # ForecastEx: price is in cents (0-100) or dollars (0-1)
            if contract_payout > 1:
                # CME: normalize by payout
                return price / contract_payout
            elif price > 1:
                # ForecastEx in cents: divide by 100
                return price / 100
            return price

        yes_price = normalize_price(last_price, payout)
        yes_bid = normalize_price(bid, payout)
        yes_ask = normalize_price(ask, payout)

        # NO price is 1 - YES price
        no_price = Decimal("1") - yes_price if yes_price is not None else None
        no_bid = Decimal("1") - yes_ask if yes_ask is not None else None
        no_ask = Decimal("1") - yes_bid if yes_bid is not None else None

        # If this is a PUT contract, it represents NO (swap YES/NO)
        if right == "P":
            yes_price, no_price = no_price, yes_price
            yes_bid, no_bid = no_bid, yes_bid
            yes_ask, no_ask = no_ask, yes_ask

        # Determine URL and category
        if is_cme:
            url = "https://www.cmegroup.com/markets/cryptocurrencies/bitcoin/bitcoin.html"
            if symbol_root in ("ECGC",):
                url = "https://www.cmegroup.com/markets/metals/precious/gold.html"
            elif symbol_root in ("ECNQ", "ECES"):
                url = "https://www.cmegroup.com/trading/equity-index/us-index.html"
        else:
            url = "https://forecasttrader.interactivebrokers.com/en/home.php"

        return MarketData(
            platform=self.platform_name,
            platform_market_id=str(conid),
            title=title,
            description=description,
            category=symbol_info.get("category", "Economics"),
            end_date=end_date,
            status="open",
            url=url,
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
