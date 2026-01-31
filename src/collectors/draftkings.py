"""DraftKings Predictions collector.

DraftKings Predictions: https://predictions.draftkings.com
- No official public API
- Uses Apify scraping service OR direct browser automation
- Available in 38 states including Georgia for sports contracts
- CFTC-regulated through acquired Railbird Exchange
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx
from playwright.async_api import async_playwright, Browser, Page
import structlog

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings

logger = structlog.get_logger()


class DraftKingsCollector(BaseCollector):
    """Collector for DraftKings Predictions using scraping.

    Supports two modes:
    1. Apify API (recommended) - Uses pre-built scraper
    2. Direct Playwright scraping (fallback)
    """

    platform_name = "draftkings"

    BASE_URL = "https://predictions.draftkings.com"
    APIFY_ACTOR_ID = "hypebridge~draftkings-predictions"

    def __init__(self, use_apify: bool = True):
        """Initialize collector.

        Args:
            use_apify: If True, use Apify API. If False, use direct scraping.
        """
        super().__init__()
        self.settings = get_settings()
        self.use_apify = use_apify and bool(self.settings.apify_api_token)
        self.http_client: Optional[httpx.AsyncClient] = None
        self.browser: Optional[Browser] = None
        self.logger = logger.bind(component="DraftKingsCollector")

    async def connect(self) -> None:
        """Initialize HTTP client or browser."""
        if self.use_apify:
            self.http_client = httpx.AsyncClient(
                base_url="https://api.apify.com/v2",
                timeout=120.0,
                headers={"Authorization": f"Bearer {self.settings.apify_api_token}"},
            )
            self.logger.info("Connected to Apify for DraftKings scraping")
        else:
            playwright = await async_playwright().start()
            self.browser = await playwright.chromium.launch(headless=True)
            self.logger.info("Started Playwright browser for DraftKings scraping")

    async def disconnect(self) -> None:
        """Close connections."""
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
        if self.browser:
            await self.browser.close()
            self.browser = None

    async def _fetch_via_apify(self, category: Optional[str] = None) -> list[dict]:
        """Fetch markets using Apify actor.

        Args:
            category: Optional category filter (e.g., 'sports', 'economics').

        Returns:
            Raw market data from Apify.
        """
        if not self.http_client:
            raise RuntimeError("Collector not connected")

        # Build input for Apify actor
        input_data = {
            "startUrls": [{"url": self.BASE_URL}],
        }

        if category:
            input_data["category"] = category

        # Run the actor synchronously and get results
        response = await self.http_client.post(
            f"/acts/{self.APIFY_ACTOR_ID}/run-sync-get-dataset-items",
            json=input_data,
            timeout=180.0,
        )
        response.raise_for_status()

        return response.json()

    async def _fetch_via_playwright(self, category: Optional[str] = None) -> list[dict]:
        """Fetch markets using direct browser scraping.

        Args:
            category: Optional category filter.

        Returns:
            Raw market data.
        """
        if not self.browser:
            raise RuntimeError("Browser not initialized")

        page = await self.browser.new_page()
        markets = []

        try:
            # Navigate to main page
            url = self.BASE_URL
            if category:
                url = f"{self.BASE_URL}/category/{category}"

            await page.goto(url, wait_until="networkidle")
            await page.wait_for_timeout(3000)  # Wait for dynamic content

            # Extract market cards
            # Note: Selectors may need updating as the site changes
            market_elements = await page.query_selector_all('[data-testid="market-card"]')

            for element in market_elements:
                try:
                    market = await self._parse_market_element(page, element)
                    if market:
                        markets.append(market)
                except Exception as e:
                    self.logger.warning("Failed to parse market element", error=str(e))

        finally:
            await page.close()

        return markets

    async def _parse_market_element(self, page: Page, element) -> Optional[dict]:
        """Parse a market card element.

        Args:
            page: Playwright page.
            element: Market card element.

        Returns:
            Dict with market data or None.
        """
        try:
            title = await element.query_selector('[data-testid="market-title"]')
            title_text = await title.inner_text() if title else None

            yes_price_el = await element.query_selector('[data-testid="yes-price"]')
            no_price_el = await element.query_selector('[data-testid="no-price"]')

            yes_price = await yes_price_el.inner_text() if yes_price_el else None
            no_price = await no_price_el.inner_text() if no_price_el else None

            # Parse prices (e.g., "45¢" -> 0.45)
            def parse_price(price_str: str) -> Optional[float]:
                if not price_str:
                    return None
                # Remove currency symbols and convert
                cleaned = price_str.replace("¢", "").replace("$", "").strip()
                try:
                    value = float(cleaned)
                    # If in cents, convert to dollars
                    if value > 1:
                        value = value / 100
                    return value
                except ValueError:
                    return None

            return {
                "title": title_text,
                "yes_price": parse_price(yes_price) if yes_price else None,
                "no_price": parse_price(no_price) if no_price else None,
                "url": await element.get_attribute("href"),
            }

        except Exception as e:
            self.logger.warning("Error parsing element", error=str(e))
            return None

    def _parse_market(self, raw: dict) -> MarketData:
        """Parse raw market data into MarketData.

        Args:
            raw: Raw market data from scraping.

        Returns:
            MarketData object.
        """
        # Handle different formats from Apify vs Playwright
        title = raw.get("title") or raw.get("question") or raw.get("name", "Unknown")
        market_id = raw.get("id") or raw.get("marketId") or str(hash(title))

        yes_price = raw.get("yes_price") or raw.get("yesPrice")
        no_price = raw.get("no_price") or raw.get("noPrice")

        if yes_price is not None:
            yes_price = Decimal(str(yes_price))
        if no_price is not None:
            no_price = Decimal(str(no_price))

        # If we only have one price, calculate the other
        if yes_price is not None and no_price is None:
            no_price = Decimal("1") - yes_price
        elif no_price is not None and yes_price is None:
            yes_price = Decimal("1") - no_price

        end_date = None
        if raw.get("endDate") or raw.get("expirationDate"):
            try:
                date_str = raw.get("endDate") or raw.get("expirationDate")
                end_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return MarketData(
            platform=self.platform_name,
            platform_market_id=str(market_id),
            title=title,
            description=raw.get("description"),
            category=raw.get("category"),
            end_date=end_date,
            status=raw.get("status", "open").lower(),
            url=raw.get("url") or f"{self.BASE_URL}/market/{market_id}",
            yes_price=yes_price,
            no_price=no_price,
        )

    async def fetch_markets(self, category: Optional[str] = None) -> list[MarketData]:
        """Fetch all available markets.

        Args:
            category: Optional category filter ('sports', 'economics', 'crypto').

        Returns:
            List of MarketData objects.
        """
        if self.use_apify:
            raw_markets = await self._fetch_via_apify(category)
        else:
            raw_markets = await self._fetch_via_playwright(category)

        markets = []
        for raw in raw_markets:
            try:
                market = self._parse_market(raw)
                markets.append(market)
            except Exception as e:
                self.logger.warning("Failed to parse market", error=str(e), raw=raw)

        self.logger.info("Fetched DraftKings markets", count=len(markets))
        return markets

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ID.

        Note: DraftKings doesn't have a single-market endpoint, so we
        fetch all and filter. Consider caching for efficiency.
        """
        markets = await self.fetch_markets()
        for market in markets:
            if market.platform_market_id == market_id:
                return market
        return None
