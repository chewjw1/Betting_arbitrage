"""DraftKings Predictions collector.

DraftKings Predictions: https://predictions.draftkings.com
- No official public API
- Uses Playwright browser automation for scraping
- Available in 38 states including Georgia
- CFTC-regulated through acquired Railbird Exchange (own DCM)
- NOT affiliated with Kalshi - independent price source
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional

from playwright.async_api import async_playwright, Browser, Page
import structlog

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings

logger = structlog.get_logger()


class DraftKingsCollector(BaseCollector):
    """Collector for DraftKings Predictions using Playwright scraping."""

    platform_name = "draftkings"

    BASE_URL = "https://predictions.draftkings.com"
    CATEGORIES = ["sports", "politics", "entertainment", "finance"]

    def __init__(self):
        """Initialize collector."""
        super().__init__()
        self.settings = get_settings()
        self.browser: Optional[Browser] = None
        self._playwright = None
        self.logger = logger.bind(component="DraftKingsCollector")

    async def connect(self) -> None:
        """Initialize Playwright browser."""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self.logger.info("Started Playwright browser for DraftKings scraping")

    async def disconnect(self) -> None:
        """Close browser."""
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _scroll_to_load_all(self, page: Page, max_scrolls: int = 10) -> None:
        """Scroll page to load all dynamic content."""
        for _ in range(max_scrolls):
            previous_height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == previous_height:
                break

    async def _scrape_page(self, page: Page, url: str) -> list[dict]:
        """Scrape markets from a page.

        Args:
            page: Playwright page.
            url: URL to scrape.

        Returns:
            List of raw market dicts.
        """
        markets = []

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Scroll to load all content
            await self._scroll_to_load_all(page)

            # Try multiple selector patterns (site structure may vary)
            selectors = [
                '[data-testid="market-card"]',
                '[class*="market-card"]',
                '[class*="MarketCard"]',
                '[class*="prediction-card"]',
                'article[class*="market"]',
            ]

            market_elements = []
            for selector in selectors:
                elements = await page.query_selector_all(selector)
                if elements:
                    market_elements = elements
                    self.logger.debug(f"Found {len(elements)} markets with selector: {selector}")
                    break

            for element in market_elements:
                try:
                    market = await self._parse_element(element)
                    if market and market.get("title"):
                        markets.append(market)
                except Exception as e:
                    self.logger.debug("Failed to parse element", error=str(e))

        except Exception as e:
            self.logger.warning("Failed to scrape page", url=url, error=str(e))

        return markets

    async def _parse_element(self, element) -> Optional[dict]:
        """Parse a market card element.

        Args:
            element: Market card element.

        Returns:
            Dict with market data or None.
        """
        # Try multiple patterns for title
        title = None
        title_selectors = [
            '[data-testid="market-title"]',
            '[class*="title"]',
            '[class*="Title"]',
            'h3', 'h4',
            '[class*="question"]',
        ]

        for selector in title_selectors:
            title_el = await element.query_selector(selector)
            if title_el:
                title = await title_el.inner_text()
                if title and len(title) > 5:
                    break

        if not title:
            return None

        # Try to find prices
        yes_price = None
        no_price = None

        # Look for price elements
        price_selectors = [
            ('[data-testid="yes-price"]', '[data-testid="no-price"]'),
            ('[class*="yes-price"]', '[class*="no-price"]'),
            ('[class*="YesPrice"]', '[class*="NoPrice"]'),
        ]

        for yes_sel, no_sel in price_selectors:
            yes_el = await element.query_selector(yes_sel)
            no_el = await element.query_selector(no_sel)
            if yes_el:
                yes_text = await yes_el.inner_text()
                yes_price = self._parse_price(yes_text)
            if no_el:
                no_text = await no_el.inner_text()
                no_price = self._parse_price(no_text)
            if yes_price is not None or no_price is not None:
                break

        # Fallback: look for any price-like elements
        if yes_price is None and no_price is None:
            price_elements = await element.query_selector_all('[class*="price"], [class*="Price"], [class*="cost"]')
            for i, el in enumerate(price_elements[:2]):
                text = await el.inner_text()
                price = self._parse_price(text)
                if price is not None:
                    if i == 0:
                        yes_price = price
                    else:
                        no_price = price

        # Get URL if available
        url = None
        link = await element.query_selector("a")
        if link:
            url = await link.get_attribute("href")
            if url and not url.startswith("http"):
                url = f"{self.BASE_URL}{url}"

        return {
            "title": title.strip(),
            "yes_price": yes_price,
            "no_price": no_price,
            "url": url,
        }

    def _parse_price(self, text: str) -> Optional[float]:
        """Parse price from text.

        Args:
            text: Price text (e.g., "45¢", "$0.45", "45%", "45").

        Returns:
            Float price in dollars (0.0 to 1.0) or None.
        """
        if not text:
            return None

        cleaned = text.strip().replace(",", "").replace(" ", "")

        try:
            # Handle cent notation (45¢)
            if "¢" in cleaned:
                return float(cleaned.replace("¢", "")) / 100

            # Handle dollar notation ($0.45)
            if "$" in cleaned:
                value = float(cleaned.replace("$", ""))
                return value if value <= 1 else value / 100

            # Handle percentage (45%)
            if "%" in cleaned:
                return float(cleaned.replace("%", "")) / 100

            # Handle plain number
            value = float(cleaned)
            # Assume cents if > 1
            return value / 100 if value > 1 else value

        except ValueError:
            return None

    def _to_market_data(self, raw: dict) -> MarketData:
        """Convert raw scraped data to MarketData.

        Args:
            raw: Raw market dict.

        Returns:
            MarketData object.
        """
        title = raw.get("title", "Unknown")
        market_id = raw.get("id") or str(hash(title))

        yes_price = None
        no_price = None

        if raw.get("yes_price") is not None:
            yes_price = Decimal(str(raw["yes_price"]))
        if raw.get("no_price") is not None:
            no_price = Decimal(str(raw["no_price"]))

        # Calculate complement if only one price available
        if yes_price is not None and no_price is None:
            no_price = Decimal("1") - yes_price
        elif no_price is not None and yes_price is None:
            yes_price = Decimal("1") - no_price

        return MarketData(
            platform=self.platform_name,
            platform_market_id=market_id,
            title=title,
            description=raw.get("description"),
            category=raw.get("category"),
            end_date=None,
            status="open",
            url=raw.get("url") or self.BASE_URL,
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
        if not self.browser:
            raise RuntimeError("Browser not initialized. Call connect() first.")

        page = await self.browser.new_page()
        all_raw_markets = []

        try:
            if category:
                # Scrape specific category
                url = f"{self.BASE_URL}/category/{category}"
                raw_markets = await self._scrape_page(page, url)
                for m in raw_markets:
                    m["category"] = category
                all_raw_markets.extend(raw_markets)
            else:
                # Scrape main page first
                raw_markets = await self._scrape_page(page, self.BASE_URL)
                all_raw_markets.extend(raw_markets)

                # Then try each category
                for cat in self.CATEGORIES:
                    await asyncio.sleep(1)  # Rate limiting
                    url = f"{self.BASE_URL}/category/{cat}"
                    raw_markets = await self._scrape_page(page, url)
                    for m in raw_markets:
                        m["category"] = cat
                    all_raw_markets.extend(raw_markets)

        finally:
            await page.close()

        # Deduplicate by title
        seen_titles = set()
        unique_markets = []
        for raw in all_raw_markets:
            title = raw.get("title", "").lower()
            if title and title not in seen_titles:
                seen_titles.add(title)
                unique_markets.append(raw)

        # Convert to MarketData
        markets = []
        for raw in unique_markets:
            try:
                market = self._to_market_data(raw)
                markets.append(market)
            except Exception as e:
                self.logger.warning("Failed to parse market", error=str(e))

        self.logger.info("Fetched DraftKings markets", count=len(markets))
        return markets

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ID."""
        markets = await self.fetch_markets()
        for market in markets:
            if market.platform_market_id == market_id:
                return market
        return None
