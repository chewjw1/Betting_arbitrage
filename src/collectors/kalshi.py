"""Kalshi API collector with authenticated API support.

Kalshi API Documentation: https://docs.kalshi.com/welcome

Kalshi's market hierarchy: Series -> Events -> Markets
Categories (Politics, Economics, Crypto, etc.) live at the Series level.
The /markets endpoint has NO category filter, so we must:
1. Fetch series tickers for desired categories via /series
2. Fetch markets per-series via /markets?series_ticker=X

Authentication uses RSA-PSS per-request signing.
Without auth: read-only, lower rate limits, public endpoints only.
With auth: higher rate limits, order book access, trading.
"""

import asyncio
import base64
import re
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings

# Module-level cache for series tickers (persists across collector instances)
_series_cache: dict[str, list[str]] = {}
_series_cache_time: Optional[datetime] = None
_SERIES_CACHE_TTL_HOURS = 6

# Cache which series had open markets (saves ~90% of API calls on subsequent runs)
_active_series_cache: set[str] = set()
_active_series_cache_time: Optional[datetime] = None
_ACTIVE_SERIES_CACHE_TTL_HOURS = 1  # Shorter TTL - markets open/close more often

# Adaptive rate limiting: persists across scans to learn the right pace
_rate_limit_delay: float = 0.15  # Current delay between requests (seconds)
_rate_limit_concurrency: int = 10  # Current concurrency level
_DELAY_MIN = 0.10  # Fastest we'll go (authenticated)
_DELAY_MAX = 2.0   # Slowest before we're wasting time
_DELAY_UNAUTH_MIN = 0.20  # Fastest for unauthenticated
_CONCURRENCY_MIN = 2
_CONCURRENCY_MAX_AUTH = 10
_CONCURRENCY_MAX_UNAUTH = 5


class KalshiCollector(BaseCollector):
    """Collector for Kalshi prediction market API.

    Fetches markets from configured categories (Politics, Economics, Crypto, etc.)
    by first discovering series tickers, then fetching markets per-series.
    This avoids the sports flood that dominates the unfiltered /markets endpoint.

    Supports both authenticated and unauthenticated modes:
    - Unauthenticated: read-only, lower rate limits
    - Authenticated: higher rate limits, order book depth, trading access
    """

    platform_name = "kalshi"

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self.client: Optional[httpx.AsyncClient] = None
        self._private_key = None
        self._api_key: str = self.settings.kalshi_api_key
        self._authenticated: bool = False
        self._rate_limit_429_count: int = 0  # 429 hits per fetch_markets call

    async def connect(self) -> None:
        """Initialize HTTP client and authenticate if credentials available."""
        self.client = httpx.AsyncClient(
            base_url=self.settings.kalshi_api_host,
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        # Try to set up authentication
        if self._api_key:
            try:
                self._load_private_key()
                self._authenticated = True
                self.logger.info("Kalshi API authenticated (RSA-PSS signing enabled)")
            except FileNotFoundError as e:
                self.logger.warning(
                    "Kalshi private key not found, running unauthenticated",
                    error=str(e),
                )
            except Exception as e:
                self.logger.warning(
                    "Kalshi authentication setup failed, running unauthenticated",
                    error=str(e),
                )
        else:
            self.logger.info("Kalshi API connected (unauthenticated, read-only)")

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    def _load_private_key(self) -> None:
        """Load RSA private key from file."""
        if self._private_key is not None:
            return

        from cryptography.hazmat.primitives import serialization

        key_path = Path(self.settings.kalshi_private_key_path)
        if not key_path.exists():
            raise FileNotFoundError(
                f"Kalshi private key not found at {key_path}. "
                "Generate an API key at https://kalshi.com and download the private key."
            )
        with open(key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(),
                password=None,
            )

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        """Create signed headers for an authenticated Kalshi API request.

        Per Kalshi docs, the message to sign is:
            {timestamp_ms}{HTTP_METHOD}{path_without_query_params}

        Uses RSA-PSS with SHA256 and DIGEST_LENGTH salt.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: Request path (query params will be stripped).

        Returns:
            Dict of authentication headers.
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp_ms = str(int(time.time() * 1000))

        # Strip query parameters from path for signing
        path_no_query = path.split("?")[0]

        message = f"{timestamp_ms}{method.upper()}{path_no_query}"

        signature = self._private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        signature_b64 = base64.b64encode(signature).decode()

        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-SIGNATURE": signature_b64,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> httpx.Response:
        """Make an API request, with authentication if available.

        Args:
            method: HTTP method.
            path: API path.
            params: Query parameters.
            json: JSON body for POST/PUT.

        Returns:
            httpx.Response
        """
        headers = {}
        if self._authenticated:
            # Build full path with query params for the URL, but sign without them
            full_path = path
            if params:
                query_string = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
                if query_string:
                    full_path = f"{path}?{query_string}"
            headers = self._sign_request(method, path)

        response = await self.client.request(
            method=method,
            url=path,
            params=params,
            json=json,
            headers=headers,
        )
        return response

    async def _fetch_series_for_category(self, category: str) -> list[str]:
        """Fetch all series tickers for a given category.

        Args:
            category: Kalshi category name (e.g., "Politics", "Economics").

        Returns:
            List of series ticker strings.
        """
        try:
            response = await self._request(
                "GET",
                "/trade-api/v2/series",
                params={"category": category},
            )
            response.raise_for_status()
            data = response.json()
            tickers = [s["ticker"] for s in data.get("series", []) if s.get("ticker")]
            self.logger.debug(
                "Fetched series for category",
                category=category,
                series_count=len(tickers),
            )
            return tickers
        except httpx.HTTPStatusError as e:
            self.logger.warning(
                "Failed to fetch series",
                category=category,
                status_code=e.response.status_code,
            )
            return []

    async def _get_series_tickers(self, categories: list[str]) -> list[str]:
        """Get series tickers for target categories, using cache when possible.

        Args:
            categories: List of Kalshi category names.

        Returns:
            List of all series tickers across the requested categories.
        """
        global _series_cache, _series_cache_time

        # Check if cache is still valid
        now = datetime.utcnow()
        cache_valid = (
            _series_cache_time is not None
            and (now - _series_cache_time).total_seconds() < _SERIES_CACHE_TTL_HOURS * 3600
        )

        all_tickers = []
        categories_to_fetch = []

        for category in categories:
            if cache_valid and category in _series_cache:
                all_tickers.extend(_series_cache[category])
            else:
                categories_to_fetch.append(category)

        if categories_to_fetch:
            self.logger.info(
                "Fetching series tickers from Kalshi",
                categories=categories_to_fetch,
            )
            for category in categories_to_fetch:
                tickers = await self._fetch_series_for_category(category)
                _series_cache[category] = tickers
                all_tickers.extend(tickers)
                await asyncio.sleep(0.15)  # Rate limit between category fetches

            _series_cache_time = now

        self.logger.info(
            "Series tickers ready",
            total_series=len(all_tickers),
            categories=len(categories),
            from_cache=len(categories) - len(categories_to_fetch),
        )
        return all_tickers

    @staticmethod
    def _is_junk_market(data: dict[str, Any]) -> bool:
        """Check if a market is junk data (no price, parlay format, etc.)."""
        # No price data = useless for arbitrage
        if not data.get("yes_bid") and not data.get("last_price"):
            return True

        title = data.get("title", "")
        title_lower = title.lower()

        # Parlay-style titles start with bet leg prefixes
        if title_lower.startswith("yes ") or title_lower.startswith("no "):
            return True

        # Multi-leg parlays: multiple commas with stat lines
        if title.count(",") >= 2 and re.search(r'\d+[+\-.]', title):
            return True

        return False

    @staticmethod
    def _build_kalshi_url(data: dict[str, Any]) -> str:
        """Build a working Kalshi market URL.

        Kalshi web URLs follow: /markets/{series_ticker}
        This redirects to the latest event in the series (e.g., /markets/kxfed
        redirects to /markets/kxfed/fed-funds-rate/kxfed-26mar).
        The series_ticker is the most reliable short URL format.
        """
        series_ticker = data.get("series_ticker", "")
        event_ticker = data.get("event_ticker", "")
        ticker = data.get("ticker", "")

        if series_ticker:
            return f"https://kalshi.com/markets/{series_ticker.lower()}"
        elif event_ticker:
            # Extract series from event ticker (e.g., "KXFED-26MAR" -> "kxfed")
            series = event_ticker.split("-")[0].lower() if "-" in event_ticker else event_ticker.lower()
            return f"https://kalshi.com/markets/{series}"
        elif ticker:
            series = ticker.split("-")[0].lower() if "-" in ticker else ticker.lower()
            return f"https://kalshi.com/markets/{series}"
        return "https://kalshi.com/browse"

    def _parse_market(self, data: dict[str, Any]) -> MarketData:
        """Parse Kalshi API market response into MarketData."""
        yes_price = None
        no_price = None

        # Use midpoint of bid/ask as the quoted price (not just bid!)
        # The bid is what you'd receive selling; ask is what you'd pay buying.
        # Using bid alone makes markets look cheaper than they are.
        yes_bid_raw = data.get("yes_bid")
        yes_ask_raw = data.get("yes_ask")

        if yes_bid_raw and yes_ask_raw:
            yes_price = (Decimal(str(yes_bid_raw)) + Decimal(str(yes_ask_raw))) / 200
        elif yes_ask_raw:
            yes_price = Decimal(str(yes_ask_raw)) / 100
        elif yes_bid_raw:
            yes_price = Decimal(str(yes_bid_raw)) / 100
        elif data.get("last_price"):
            yes_price = Decimal(str(data["last_price"])) / 100

        if yes_price is not None:
            no_price = Decimal("1") - yes_price

        end_date = None
        if data.get("close_time"):
            try:
                end_date = datetime.fromisoformat(
                    data["close_time"].replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        return MarketData(
            platform=self.platform_name,
            platform_market_id=data.get("ticker", data.get("id", "")),
            title=data.get("title", ""),
            description=data.get("subtitle", data.get("rules_primary", "")),
            resolution_criteria=data.get("rules_primary"),
            category=data.get("category"),
            end_date=end_date,
            status=data.get("status", "open"),
            url=self._build_kalshi_url(data),
            yes_price=yes_price,
            no_price=no_price,
            yes_bid=Decimal(str(data["yes_bid"])) / 100 if data.get("yes_bid") else None,
            yes_ask=Decimal(str(data["yes_ask"])) / 100 if data.get("yes_ask") else None,
            no_bid=Decimal(str(data["no_bid"])) / 100 if data.get("no_bid") else None,
            no_ask=Decimal(str(data["no_ask"])) / 100 if data.get("no_ask") else None,
            total_volume=Decimal(str(data["volume"])) if data.get("volume") else None,
            open_interest=Decimal(str(data["open_interest"])) if data.get("open_interest") else None,
        )

    async def _fetch_markets_for_series(
        self, series_ticker: str, max_per_series: int = 200
    ) -> list[MarketData]:
        """Fetch open markets for a specific series ticker with retry on 429.

        Args:
            series_ticker: The series ticker (e.g., "KXFED", "KXBTC").
            max_per_series: Max markets to fetch per series.

        Returns:
            List of MarketData for this series.
        """
        markets = []
        cursor = None
        retries_on_429 = 0

        while len(markets) < max_per_series:
            params = {
                "series_ticker": series_ticker,
                "status": "open",
                "limit": min(1000, max_per_series),
            }
            if cursor:
                params["cursor"] = cursor

            try:
                response = await self._request("GET", "/trade-api/v2/markets", params=params)
                response.raise_for_status()
                retries_on_429 = 0  # Reset on success
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    break  # Series has no markets
                if e.response.status_code == 429 and retries_on_429 < 3:
                    retries_on_429 += 1
                    self._rate_limit_429_count += 1
                    wait_time = 2 ** retries_on_429  # 2s, 4s, 8s
                    await asyncio.sleep(wait_time)
                    continue
                self.logger.warning(
                    "Kalshi API error for series",
                    series_ticker=series_ticker,
                    status_code=e.response.status_code,
                )
                break

            data = response.json()
            batch = data.get("markets", [])
            if not batch:
                break

            for market in batch:
                try:
                    if self._is_junk_market(market):
                        continue
                    markets.append(self._parse_market(market))
                except Exception as e:
                    self.logger.warning(
                        "Failed to parse market",
                        market_id=market.get("ticker"),
                        error=str(e),
                    )

            cursor = data.get("cursor")
            if not cursor:
                break

        return markets

    async def fetch_markets(
        self,
        category: Optional[str] = None,
        max_markets: int = 2000,
    ) -> list[MarketData]:
        """Fetch available markets from Kalshi for configured categories.

        Uses a two-step approach:
        1. Fetch series tickers for target categories (cached for 6 hours)
        2. Fetch open markets per-series (concurrent with semaphore)

        Prioritizes series known to have open markets from previous runs,
        then checks remaining series. Uses a semaphore to limit concurrency
        and avoid 429 rate limits.

        Args:
            category: Single category override. If None, uses KALSHI_CATEGORIES from config.
            max_markets: Maximum markets to fetch (default 2000).

        Returns:
            List of MarketData objects.
        """
        global _active_series_cache, _active_series_cache_time

        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        self._rate_limit_429_count = 0  # Reset per-scan counter

        # Determine categories to fetch
        if category:
            categories = [category]
        else:
            categories = [
                c.strip()
                for c in self.settings.kalshi_categories.split(",")
                if c.strip()
            ]

        max_markets = min(max_markets, self.settings.kalshi_max_markets)

        # Step 1: Get series tickers for target categories
        series_tickers = await self._get_series_tickers(categories)

        if not series_tickers:
            self.logger.warning("No series tickers found for categories", categories=categories)
            return []

        # Prioritize series known to have open markets (from previous runs)
        now = datetime.utcnow()
        active_cache_valid = (
            _active_series_cache_time is not None
            and (now - _active_series_cache_time).total_seconds()
            < _ACTIVE_SERIES_CACHE_TTL_HOURS * 3600
        )

        if active_cache_valid and _active_series_cache:
            # Put known-active series first, then the rest
            known_active = [t for t in series_tickers if t in _active_series_cache]
            unknown = [t for t in series_tickers if t not in _active_series_cache]
            series_tickers = known_active + unknown
            self.logger.info(
                "Prioritizing known-active series",
                known_active=len(known_active),
                unknown=len(unknown),
            )

        # Step 2: Fetch markets per-series with adaptive rate control
        global _rate_limit_delay, _rate_limit_concurrency

        concurrency_max = _CONCURRENCY_MAX_AUTH if self._authenticated else _CONCURRENCY_MAX_UNAUTH
        concurrency = min(_rate_limit_concurrency, concurrency_max)
        delay = max(_rate_limit_delay, _DELAY_UNAUTH_MIN if not self._authenticated else _DELAY_MIN)

        markets = []
        series_with_markets = 0
        api_calls = 0
        new_active_series: set[str] = set()
        semaphore = asyncio.Semaphore(concurrency)
        stop_flag = False

        async def fetch_one(ticker: str) -> tuple[str, list[MarketData]]:
            nonlocal api_calls
            async with semaphore:
                if stop_flag:
                    return ticker, []
                await asyncio.sleep(delay)
                api_calls += 1
                result = await self._fetch_markets_for_series(
                    ticker, max_per_series=200
                )
                return ticker, result

        # Process in batches of 50 to check market cap periodically
        batch_size = 50
        for batch_start in range(0, len(series_tickers), batch_size):
            if len(markets) >= max_markets:
                break

            batch = series_tickers[batch_start : batch_start + batch_size]
            tasks = [fetch_one(ticker) for ticker in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    self.logger.warning("Series fetch failed", error=str(result))
                    continue
                ticker, series_markets = result
                if series_markets:
                    markets.extend(series_markets)
                    series_with_markets += 1
                    new_active_series.add(ticker)

            if len(markets) >= max_markets:
                stop_flag = True
                break

            # Brief pause between batches
            await asyncio.sleep(0.5)

        # Update active series cache
        _active_series_cache = new_active_series
        _active_series_cache_time = now

        # Trim to max
        if len(markets) > max_markets:
            markets = markets[:max_markets]

        # Adaptive rate adjustment based on 429s this scan
        prev_delay = delay
        prev_concurrency = concurrency
        if self._rate_limit_429_count >= 5:
            # Heavy throttling — slow down significantly
            _rate_limit_delay = min(delay * 2.0, _DELAY_MAX)
            _rate_limit_concurrency = max(concurrency - 2, _CONCURRENCY_MIN)
        elif self._rate_limit_429_count >= 1:
            # Light throttling — nudge slower
            _rate_limit_delay = min(delay * 1.3, _DELAY_MAX)
            _rate_limit_concurrency = max(concurrency - 1, _CONCURRENCY_MIN)
        elif api_calls > 20 and self._rate_limit_429_count == 0:
            # Clean scan with meaningful traffic — try speeding up
            delay_min = _DELAY_UNAUTH_MIN if not self._authenticated else _DELAY_MIN
            conc_max = _CONCURRENCY_MAX_AUTH if self._authenticated else _CONCURRENCY_MAX_UNAUTH
            _rate_limit_delay = max(delay * 0.85, delay_min)
            _rate_limit_concurrency = min(concurrency + 1, conc_max)

        self.logger.info(
            "Fetched markets from Kalshi",
            count=len(markets),
            authenticated=self._authenticated,
            series_checked=min(len(series_tickers), api_calls),
            series_with_markets=series_with_markets,
            api_calls=api_calls,
            rate_limit_429s=self._rate_limit_429_count,
            delay=f"{prev_delay:.2f}s→{_rate_limit_delay:.2f}s",
            concurrency=f"{prev_concurrency}→{_rate_limit_concurrency}",
            categories=categories,
        )
        return markets

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ticker."""
        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        try:
            response = await self._request("GET", f"/trade-api/v2/markets/{market_id}")
            response.raise_for_status()
            data = response.json()
            return self._parse_market(data.get("market", data))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def fetch_orderbook(self, market_id: str) -> dict:
        """Fetch orderbook for a specific market.

        Returns order book with bids and asks, each containing
        price (in cents) and quantity.

        Args:
            market_id: Market ticker.

        Returns:
            Dict with 'yes' and 'no' keys, each containing
            list of [price, quantity] entries.
        """
        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        response = await self._request("GET", f"/trade-api/v2/markets/{market_id}/orderbook")
        response.raise_for_status()
        data = response.json()

        # Parse into standardized format
        orderbook = data.get("orderbook", data)
        return {
            "yes_bids": [(entry[0], entry[1]) for entry in orderbook.get("yes", []) if len(entry) >= 2],
            "no_bids": [(entry[0], entry[1]) for entry in orderbook.get("no", []) if len(entry) >= 2],
        }

    async def fetch_market_with_depth(self, market_id: str) -> Optional[MarketData]:
        """Fetch a market and enrich with order book depth data.

        Fetches both market data and order book, then populates
        the depth fields (yes_ask_size, etc.) for slippage estimation.

        Args:
            market_id: Market ticker.

        Returns:
            MarketData with depth fields populated, or None.
        """
        market = await self.fetch_market(market_id)
        if not market:
            return None

        try:
            book = await self.fetch_orderbook(market_id)

            # Best ask = lowest price someone is selling at
            # For YES: look at yes_bids (they sell, you buy)
            # Kalshi orderbook: "yes" array has bids for YES contracts
            yes_bids = book.get("yes_bids", [])
            no_bids = book.get("no_bids", [])

            if yes_bids:
                # Best YES ask size (top of book)
                best = yes_bids[0]
                market.yes_ask_size = Decimal(str(best[1]))

            if no_bids:
                best = no_bids[0]
                market.no_ask_size = Decimal(str(best[1]))

        except Exception as e:
            self.logger.debug("Order book fetch failed", market_id=market_id, error=str(e))

        return market
