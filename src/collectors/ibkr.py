"""Interactive Brokers ForecastTrader collector.

IBKR ForecastTrader: https://forecasttrader.interactivebrokers.com
- CFTC-regulated via ForecastEx DCM
- Zero commission on forecast contracts
- Supports TWS API, Web API, FIX protocol
- Contracts from ForecastEx and CME

API Documentation: https://www.interactivebrokers.com/en/trading/ib-api.php
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx
from playwright.async_api import async_playwright, Browser
import structlog

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings

logger = structlog.get_logger()


class IBKRCollector(BaseCollector):
    """Collector for Interactive Brokers ForecastTrader.

    IBKR provides multiple API options:
    1. TWS API (requires desktop app running)
    2. Client Portal API (web-based, requires authentication)
    3. Web scraping (fallback)

    This implementation uses web scraping of the public ForecastTrader
    site for price discovery, as the TWS/Portal APIs require authentication.
    """

    platform_name = "ibkr"

    BASE_URL = "https://forecasttrader.interactivebrokers.com"
    MARKETS_URL = f"{BASE_URL}/eventtrader/"

    # Categories available on IBKR
    CATEGORIES = [
        "elections",
        "economics",
        "climate",
        "crypto",
        "equities",
        "energy",
        "metals",
    ]

    def __init__(self, use_api: bool = False):
        """Initialize collector.

        Args:
            use_api: If True, attempt to use IBKR Client Portal API.
                    Requires IBKR credentials configured.
        """
        super().__init__()
        self.settings = get_settings()
        self.use_api = use_api
        self.browser: Optional[Browser] = None
        self.http_client: Optional[httpx.AsyncClient] = None
        self.logger = logger.bind(component="IBKRCollector")

    async def connect(self) -> None:
        """Initialize browser or API client."""
        if self.use_api and self.settings.ibkr_username:
            # Client Portal API setup
            self.http_client = httpx.AsyncClient(
                base_url="https://localhost:5000/v1/api",
                verify=False,  # Client Portal uses self-signed certs
                timeout=30.0,
            )
            self.logger.info("Initialized IBKR Client Portal API client")
        else:
            # Fallback to scraping
            playwright = await async_playwright().start()
            self.browser = await playwright.chromium.launch(headless=True)
            self.logger.info("Started browser for IBKR ForecastTrader scraping")

    async def disconnect(self) -> None:
        """Close connections."""
        if self.http_client:
            await self.http_client.aclose()
            self.http_client = None
        if self.browser:
            await self.browser.close()
            self.browser = None

    async def _fetch_via_scraping(self, category: Optional[str] = None) -> list[dict]:
        """Fetch markets by scraping the ForecastTrader website.

        Args:
            category: Optional category filter.

        Returns:
            List of raw market dicts.
        """
        if not self.browser:
            raise RuntimeError("Browser not initialized")

        page = await self.browser.new_page()
        markets = []

        try:
            # Navigate to markets page
            await page.goto(self.MARKETS_URL, wait_until="networkidle")
            await page.wait_for_timeout(3000)

            # If category specified, try to filter
            if category:
                try:
                    # Look for category filter/tab
                    cat_selector = f'[data-category="{category}"], button:has-text("{category}")'
                    cat_button = await page.query_selector(cat_selector)
                    if cat_button:
                        await cat_button.click()
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass  # Category filter not found, continue with all

            # Find market rows/cards
            # IBKR typically uses table layouts
            rows = await page.query_selector_all(
                'tr[class*="market"], tr[class*="contract"], [class*="market-row"]'
            )

            if not rows:
                # Try card-based layout
                rows = await page.query_selector_all(
                    '[class*="market-card"], [class*="contract-card"]'
                )

            for row in rows:
                try:
                    market = await self._parse_row(row)
                    if market:
                        markets.append(market)
                except Exception as e:
                    self.logger.debug("Failed to parse row", error=str(e))

        finally:
            await page.close()

        return markets

    async def _parse_row(self, row) -> Optional[dict]:
        """Parse a market row element.

        Args:
            row: Row element.

        Returns:
            Dict with market data or None.
        """
        # Get title/question
        title_el = await row.query_selector(
            'td:first-child, [class*="title"], [class*="name"], [class*="question"]'
        )
        title = await title_el.inner_text() if title_el else None

        if not title or len(title) < 5:
            return None

        # Get prices
        price_cells = await row.query_selector_all(
            'td[class*="price"], [class*="bid"], [class*="ask"], [class*="last"]'
        )

        yes_price = None
        no_price = None

        for cell in price_cells:
            text = await cell.inner_text()
            price = self._parse_price(text)
            if price is not None:
                if yes_price is None:
                    yes_price = price
                else:
                    no_price = price
                    break

        # Get contract ID if available
        contract_id = await row.get_attribute("data-contract-id")
        if not contract_id:
            contract_id = await row.get_attribute("data-id")
        if not contract_id:
            contract_id = str(hash(title))

        return {
            "id": contract_id,
            "title": title.strip(),
            "yes_price": yes_price,
            "no_price": no_price,
        }

    def _parse_price(self, text: str) -> Optional[float]:
        """Parse price from text.

        IBKR prices are typically in USD 0.02 to 0.99 format.
        """
        if not text:
            return None

        cleaned = text.strip().replace("$", "").replace(",", "")

        try:
            value = float(cleaned)
            # IBKR quotes in dollars, but verify range
            if 0 < value <= 1:
                return value
            elif value > 1:
                return value / 100  # Was in cents
            return None
        except ValueError:
            return None

    def _parse_market(self, raw: dict) -> MarketData:
        """Convert raw data to MarketData.

        Args:
            raw: Raw market dict.

        Returns:
            MarketData object.
        """
        title = raw.get("title", "Unknown")

        yes_price = None
        no_price = None

        if raw.get("yes_price") is not None:
            yes_price = Decimal(str(raw["yes_price"]))
        if raw.get("no_price") is not None:
            no_price = Decimal(str(raw["no_price"]))

        if yes_price is not None and no_price is None:
            no_price = Decimal("1") - yes_price
        elif no_price is not None and yes_price is None:
            yes_price = Decimal("1") - no_price

        return MarketData(
            platform=self.platform_name,
            platform_market_id=str(raw.get("id", hash(title))),
            title=title,
            description=raw.get("description"),
            category=raw.get("category"),
            end_date=None,
            status="open",
            url=raw.get("url") or self.MARKETS_URL,
            yes_price=yes_price,
            no_price=no_price,
        )

    async def fetch_markets(self, category: Optional[str] = None) -> list[MarketData]:
        """Fetch all available markets.

        Args:
            category: Optional category filter.

        Returns:
            List of MarketData objects.
        """
        raw_markets = await self._fetch_via_scraping(category)

        markets = []
        for raw in raw_markets:
            try:
                market = self._parse_market(raw)
                markets.append(market)
            except Exception as e:
                self.logger.warning("Failed to parse market", error=str(e))

        self.logger.info("Fetched IBKR markets", count=len(markets))
        return markets

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ID."""
        markets = await self.fetch_markets()
        for market in markets:
            if market.platform_market_id == market_id:
                return market
        return None
