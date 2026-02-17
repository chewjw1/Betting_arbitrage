"""DraftKings Predictions collector.

DraftKings Predictions: https://predictions.draftkings.com
- Uses reverse-engineered API (no auth required for market data)
- Catalog fetched from Remix turbo-stream (.data endpoints)
- Live bid/ask prices from polling endpoint
- CFTC-regulated through acquired Railbird Exchange (own DCM)
- NOT affiliated with Kalshi - independent price source

API Endpoints:
  Catalog: GET https://predictions.draftkings.com/en/{category}.data
  Prices:  POST https://api.draftkings.com/en/predict/v1/polling/clients/web/markets
"""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx
import structlog

from src.collectors.base import BaseCollector, MarketData

logger = structlog.get_logger()

# Categories available on DraftKings Predictions
# Sports categories are included but can be filtered out if desired
DK_CATEGORIES = [
    "economics",
    "sb",       # Super Bowl / NFL
    "olympics",
    "nba",
    "nhl",
    "cbb",      # College basketball
    "golf",
]

# Non-sports categories most useful for cross-platform arbitrage
DK_NON_SPORTS_CATEGORIES = ["economics"]

# Module-level catalog cache (persists across collector instances)
_catalog_cache: dict[str, list[dict]] = {}
_catalog_cache_time: Optional[datetime] = None
_CATALOG_CACHE_TTL_SECONDS = 300  # 5 minutes


def _decode_turbo_stream(raw_data: list) -> dict:
    """Decode Remix turbo-stream flat JSON array into structured data.

    The turbo-stream format is a flat JSON array where:
    - Objects use _N keys, where N is an array index for the field name
    - Values are either literals or array indices referencing other values
    - Negative indices are sentinel values (null/undefined)

    Returns dict with:
        market_groups: list of {ticker, title, market_tickers: [str], ...}
        markets: list of {ticker, title, group_ticker, group_title, close_date, ...}
    """
    arr = raw_data

    def resolve(idx):
        if isinstance(idx, int) and 0 <= idx < len(arr):
            return arr[idx]
        return idx

    def decode_obj(obj):
        if not isinstance(obj, dict):
            return obj
        result = {}
        for k, v in obj.items():
            k_idx = int(k.lstrip("_"))
            k_name = resolve(k_idx)
            if not isinstance(k_name, str):
                k_name = f"idx_{k_idx}"
            result[k_name] = resolve(v)
        return result

    # Step 1: Find marketGroups section
    groups_data = []
    mg_dict_idx = None
    for i, elem in enumerate(arr):
        if isinstance(elem, str) and elem == "marketGroups":
            mg_dict_idx = i + 1
            break

    if mg_dict_idx is not None and isinstance(arr[mg_dict_idx], dict):
        for _ref_key, group_idx in arr[mg_dict_idx].items():
            group_obj = decode_obj(arr[group_idx])
            ticker = group_obj.get("ticker", "")
            title = group_obj.get("title", "")

            # Get list of individual market tickers
            # The markets list contains array indices that need resolving
            market_tickers = []
            raw_markets = group_obj.get("markets", [])
            if isinstance(raw_markets, list):
                for item in raw_markets:
                    resolved_item = resolve(item) if isinstance(item, int) else item
                    if isinstance(resolved_item, str) and resolved_item.startswith("DKP"):
                        market_tickers.append(resolved_item)

            groups_data.append({
                "ticker": ticker,
                "title": title,
                "market_tickers": market_tickers,
            })

    # Step 2: Find detailed market catalog (section with individual market objects)
    # This is a dict mapping ticker indices to full market objects with titles like "At least 50,000"
    markets_data = []
    group_title_map = {g["ticker"]: g["title"] for g in groups_data}

    # Collect all known individual market tickers from groups
    all_group_market_tickers = set()
    for g in groups_data:
        all_group_market_tickers.update(g["market_tickers"])

    # Find the catalog dict - it's a large dict whose keys resolve to individual
    # market tickers (DKP2-...-XXXX or DKP3-...). It contains full market objects
    # with title, close date, strike info, etc.
    catalog_idx = None
    for i, elem in enumerate(arr):
        if isinstance(elem, dict) and len(elem) > 5:
            # Check if first key resolves to a known market ticker
            first_key = next(iter(elem), "")
            first_key_idx = int(first_key.lstrip("_")) if first_key.startswith("_") else -1
            if first_key_idx >= 0:
                first_key_val = resolve(first_key_idx)
                if isinstance(first_key_val, str) and first_key_val in all_group_market_tickers:
                    # Verify the value is an object (not just a price store)
                    first_val = resolve(elem[first_key])
                    if isinstance(first_val, dict) and len(first_val) > 5:
                        catalog_idx = i
                        break

    if catalog_idx is not None:
        catalog = arr[catalog_idx]
        for ref_key, obj_idx in catalog.items():
            ticker_idx = int(ref_key.lstrip("_"))
            ticker = resolve(ticker_idx)
            if not isinstance(ticker, str) or not ticker.startswith("DKP"):
                continue

            obj = decode_obj(arr[obj_idx])
            market_title = obj.get("title", "")
            group_ticker = obj.get("marketGroupTicker", "")
            close_date = obj.get("closedDateUtc", "")
            status = obj.get("status", "")
            vol24h = obj.get("volume24Hours", 0)
            floor_strike = obj.get("floorStrike")
            strike_type = obj.get("strikeType", "")

            # Build full title: "Group Title: Market Title"
            group_title = group_title_map.get(group_ticker, "")
            if group_title and market_title:
                full_title = f"{group_title}: {market_title}"
            elif group_title:
                full_title = group_title
            elif market_title:
                full_title = market_title
            else:
                full_title = ticker

            markets_data.append({
                "ticker": ticker,
                "title": full_title,
                "market_title": market_title,
                "group_ticker": group_ticker,
                "group_title": group_title,
                "close_date": close_date,
                "status": status,
                "volume_24h": vol24h,
                "floor_strike": floor_strike,
                "strike_type": strike_type,
            })

    return {
        "market_groups": groups_data,
        "markets": markets_data,
    }


class DraftKingsCollector(BaseCollector):
    """Collector for DraftKings Predictions using reverse-engineered API.

    No authentication required. Uses two endpoints:
    1. Catalog from Remix turbo-stream (market tickers + titles + close dates)
    2. Polling endpoint for live bid/ask prices
    """

    platform_name = "draftkings"

    CATALOG_URL = "https://predictions.draftkings.com/en/{category}.data"
    POLLING_URL = "https://api.draftkings.com/en/predict/v1/polling/clients/web/markets"
    BASE_URL = "https://predictions.draftkings.com"

    # Max tickers per polling request (API limit unknown, stay conservative)
    POLLING_BATCH_SIZE = 100

    def __init__(self, categories: Optional[list[str]] = None, include_sports: bool = True):
        """Initialize collector.

        Args:
            categories: List of category slugs to fetch. If None, uses all available.
            include_sports: If False, only fetch non-sports categories (economics, etc.)
        """
        super().__init__()
        if categories:
            self.categories = categories
        elif include_sports:
            self.categories = list(DK_CATEGORIES)
        else:
            self.categories = list(DK_NON_SPORTS_CATEGORIES)
        self.client: Optional[httpx.AsyncClient] = None
        self.logger = logger.bind(component="DraftKingsCollector")

    async def connect(self) -> None:
        """Initialize HTTP client."""
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
        self.logger.info("Connected to DraftKings Predictions API")

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _fetch_catalog(self, category: str) -> list[dict]:
        """Fetch market catalog from turbo-stream for a category.

        Args:
            category: Category slug (e.g., 'economics', 'nba').

        Returns:
            List of market dicts with ticker, title, close_date, etc.
        """
        global _catalog_cache, _catalog_cache_time

        # Check cache
        now = datetime.utcnow()
        cache_valid = (
            _catalog_cache_time is not None
            and (now - _catalog_cache_time).total_seconds() < _CATALOG_CACHE_TTL_SECONDS
        )
        if cache_valid and category in _catalog_cache:
            return _catalog_cache[category]

        url = self.CATALOG_URL.format(category=category)
        try:
            response = await self.client.get(
                url,
                headers={"Accept": "text/x-turbo"},
            )
            response.raise_for_status()
            raw_data = json.loads(response.text)
            decoded = _decode_turbo_stream(raw_data)
            markets = decoded["markets"]

            # Cache the result
            _catalog_cache[category] = markets
            _catalog_cache_time = now

            self.logger.info(
                "Fetched DK catalog",
                category=category,
                groups=len(decoded["market_groups"]),
                markets=len(markets),
            )
            return markets

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self.logger.debug("Category not found", category=category)
                return []
            self.logger.warning(
                "Failed to fetch DK catalog",
                category=category,
                status=e.response.status_code,
            )
            return []
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            self.logger.warning(
                "Failed to parse DK turbo-stream",
                category=category,
                error=str(e),
            )
            return []

    async def _fetch_prices(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch live bid/ask prices for market tickers.

        Args:
            tickers: List of market ticker strings.

        Returns:
            Dict mapping ticker to price data {yesAsk, yesBid, noAsk, noBid, volume, lastPrice}.
        """
        all_prices = {}

        # Batch the requests
        for i in range(0, len(tickers), self.POLLING_BATCH_SIZE):
            batch = tickers[i : i + self.POLLING_BATCH_SIZE]
            try:
                response = await self.client.post(
                    self.POLLING_URL,
                    json={"marketTickers": batch, "languageCode": "en"},
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://predictions.draftkings.com",
                    },
                )
                response.raise_for_status()
                data = response.json()

                for ticker, market_data in data.get("markets", {}).items():
                    binary = market_data.get("details", {}).get("binary", {})
                    all_prices[ticker] = {
                        "yes_ask": binary.get("yesAsk", 0),
                        "yes_bid": binary.get("yesBid", 0),
                        "no_ask": binary.get("noAsk", 0),
                        "no_bid": binary.get("noBid", 0),
                        "volume": market_data.get("volume", 0),
                        "last_price": market_data.get("lastPrice", 0),
                    }

            except httpx.HTTPStatusError as e:
                self.logger.warning(
                    "DK polling request failed",
                    status=e.response.status_code,
                    batch_size=len(batch),
                )
            except Exception as e:
                self.logger.warning("DK polling error", error=str(e))

            # Small delay between batches
            if i + self.POLLING_BATCH_SIZE < len(tickers):
                await asyncio.sleep(0.2)

        return all_prices

    def _to_market_data(self, catalog_entry: dict, prices: dict[str, dict]) -> Optional[MarketData]:
        """Convert catalog entry + prices into MarketData.

        Args:
            catalog_entry: Market dict from catalog.
            prices: Price data dict from polling endpoint.

        Returns:
            MarketData or None if no price data.
        """
        ticker = catalog_entry["ticker"]
        price_data = prices.get(ticker)

        # Skip markets with no price data at all
        if not price_data:
            return None

        # Prices are in cents (0-100), convert to decimals (0.00-1.00)
        yes_ask_cents = price_data.get("yes_ask", 0)
        yes_bid_cents = price_data.get("yes_bid", 0)
        no_ask_cents = price_data.get("no_ask", 0)
        no_bid_cents = price_data.get("no_bid", 0)

        # Skip markets where all prices are 0 (no orders)
        if yes_ask_cents == 0 and yes_bid_cents == 0 and no_ask_cents == 0 and no_bid_cents == 0:
            return None

        yes_ask = Decimal(str(yes_ask_cents)) / 100 if yes_ask_cents else None
        yes_bid = Decimal(str(yes_bid_cents)) / 100 if yes_bid_cents else None
        no_ask = Decimal(str(no_ask_cents)) / 100 if no_ask_cents else None
        no_bid = Decimal(str(no_bid_cents)) / 100 if no_bid_cents else None

        # Calculate midpoint price for yes/no
        if yes_bid and yes_ask:
            yes_price = (yes_bid + yes_ask) / 2
        elif yes_ask:
            yes_price = yes_ask
        elif yes_bid:
            yes_price = yes_bid
        else:
            yes_price = None

        if no_bid and no_ask:
            no_price = (no_bid + no_ask) / 2
        elif yes_price is not None:
            no_price = Decimal("1") - yes_price
        else:
            no_price = None

        # Parse close date
        end_date = None
        close_str = catalog_entry.get("close_date", "")
        if close_str:
            try:
                # Handle .NET style datetime: "2026-02-11T13:29:00.0000000Z"
                clean = close_str.replace("Z", "+00:00")
                if "." in clean:
                    # Truncate fractional seconds to 6 digits
                    dot_idx = clean.index(".")
                    plus_idx = clean.index("+", dot_idx) if "+" in clean[dot_idx:] else len(clean)
                    frac = clean[dot_idx + 1 : plus_idx][:6]
                    clean = clean[:dot_idx + 1] + frac + clean[plus_idx:]
                end_date = datetime.fromisoformat(clean)
            except (ValueError, AttributeError):
                pass

        # Build URL
        group_ticker = catalog_entry.get("group_ticker", "")
        url = f"{self.BASE_URL}/en/details/{group_ticker}" if group_ticker else self.BASE_URL

        # Volume from polling endpoint
        volume = price_data.get("volume", 0)
        vol_24h = catalog_entry.get("volume_24h", 0)

        return MarketData(
            platform=self.platform_name,
            platform_market_id=ticker,
            title=catalog_entry["title"],
            description=catalog_entry.get("market_title", ""),
            category=catalog_entry.get("group_title", ""),
            end_date=end_date,
            status="open" if catalog_entry.get("status") == "Open" else "closed",
            url=url,
            yes_price=yes_price,
            no_price=no_price,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            total_volume=Decimal(str(volume)) if volume else None,
            volume_24h=Decimal(str(vol_24h)) if vol_24h else None,
        )

    async def fetch_markets(
        self,
        category: Optional[str] = None,
        max_markets: int = 5000,
    ) -> list[MarketData]:
        """Fetch all available markets from DraftKings Predictions.

        Args:
            category: Optional single category to fetch. If None, fetches all configured categories.
            max_markets: Maximum number of markets to return.

        Returns:
            List of MarketData objects with live bid/ask prices.
        """
        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        categories = [category] if category else self.categories
        all_catalog_entries = []

        # Step 1: Fetch catalogs for all categories
        for cat in categories:
            entries = await self._fetch_catalog(cat)
            # Only include open markets
            open_entries = [e for e in entries if e.get("status") == "Open"]
            all_catalog_entries.extend(open_entries)

            if len(all_catalog_entries) >= max_markets:
                all_catalog_entries = all_catalog_entries[:max_markets]
                break

        if not all_catalog_entries:
            self.logger.info("No DraftKings markets found in catalog")
            return []

        # Step 2: Fetch live prices for all tickers
        tickers = [e["ticker"] for e in all_catalog_entries]
        prices = await self._fetch_prices(tickers)

        # Step 3: Build MarketData objects
        markets = []
        for entry in all_catalog_entries:
            market = self._to_market_data(entry, prices)
            if market:
                markets.append(market)

        self.logger.info(
            "Fetched DraftKings markets",
            categories=categories,
            catalog_count=len(all_catalog_entries),
            priced_count=len(markets),
        )
        return markets

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ticker.

        Args:
            market_id: Market ticker (e.g., 'DKP2-ECNFPG6-M50000').

        Returns:
            MarketData if found, None otherwise.
        """
        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        # Fetch prices for this single ticker
        prices = await self._fetch_prices([market_id])
        if market_id not in prices:
            return None

        # We need catalog info too - check cache or fetch the relevant category
        for cat_entries in _catalog_cache.values():
            for entry in cat_entries:
                if entry["ticker"] == market_id:
                    return self._to_market_data(entry, prices)

        # If not in cache, create a minimal entry
        price_data = prices[market_id]
        return self._to_market_data(
            {
                "ticker": market_id,
                "title": market_id,
                "status": "Open",
                "close_date": "",
            },
            prices,
        )
