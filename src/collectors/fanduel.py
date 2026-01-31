"""FanDuel Predicts collector.

FanDuel Predicts: https://www.fanduel.com/predicts
- Joint venture between FanDuel and CME Group
- Contracts listed by CME Group derivatives exchanges
- CFTC-regulated
- No official public API - requires scraping
- Available in AL, AK, SC, ND, SD initially, expanding 2026
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from playwright.async_api import async_playwright, Browser, Page
import structlog

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings

logger = structlog.get_logger()


class FanDuelCollector(BaseCollector):
    """Collector for FanDuel Predicts using browser scraping.

    Since FanDuel Predicts uses CME Group contracts, prices should
    theoretically match CME directly. However, spreads may differ
    due to FanDuel's interface and user base.
    """

    platform_name = "fanduel"

    BASE_URL = "https://www.fanduel.com/predicts"
    CATEGORIES = ["sports", "finance", "economics", "crypto"]

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self.browser: Optional[Browser] = None
        self.logger = logger.bind(component="FanDuelCollector")

    async def connect(self) -> None:
        """Initialize Playwright browser."""
        playwright = await async_playwright().start()
        self.browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self.logger.info("Started Playwright browser for FanDuel scraping")

    async def disconnect(self) -> None:
        """Close browser."""
        if self.browser:
            await self.browser.close()
            self.browser = None

    async def _scrape_category(self, page: Page, category: str) -> list[dict]:
        """Scrape markets from a category page.

        Args:
            page: Playwright page.
            category: Category name.

        Returns:
            List of raw market dicts.
        """
        markets = []

        try:
            url = f"{self.BASE_URL}/{category}"
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_timeout(2000)

            # Scroll to load all markets
            await self._scroll_to_load_all(page)

            # Extract market data
            # Note: These selectors are approximate and may need adjustment
            market_cards = await page.query_selector_all(
                '[class*="market-card"], [class*="event-card"], [data-testid*="market"]'
            )

            for card in market_cards:
                try:
                    market = await self._parse_card(card, category)
                    if market:
                        markets.append(market)
                except Exception as e:
                    self.logger.debug("Failed to parse card", error=str(e))

        except Exception as e:
            self.logger.warning(
                "Failed to scrape category",
                category=category,
                error=str(e),
            )

        return markets

    async def _scroll_to_load_all(self, page: Page, max_scrolls: int = 10) -> None:
        """Scroll page to load all dynamic content.

        Args:
            page: Playwright page.
            max_scrolls: Maximum scroll attempts.
        """
        for _ in range(max_scrolls):
            previous_height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            new_height = await page.evaluate("document.body.scrollHeight")

            if new_height == previous_height:
                break

    async def _parse_card(self, card, category: str) -> Optional[dict]:
        """Parse a market card element.

        Args:
            card: Card element.
            category: Market category.

        Returns:
            Dict with market data or None.
        """
        # Get title
        title_el = await card.query_selector(
            'h3, h4, [class*="title"], [class*="question"]'
        )
        title = await title_el.inner_text() if title_el else None

        if not title:
            return None

        # Get prices - look for yes/no price elements
        yes_price = None
        no_price = None

        # Try common patterns
        price_elements = await card.query_selector_all(
            '[class*="price"], [class*="odds"], [class*="cost"]'
        )

        for i, el in enumerate(price_elements[:2]):
            text = await el.inner_text()
            price = self._parse_price_text(text)
            if price is not None:
                if i == 0:
                    yes_price = price
                else:
                    no_price = price

        # Get URL if available
        link = await card.query_selector("a")
        url = await link.get_attribute("href") if link else None
        if url and not url.startswith("http"):
            url = f"https://www.fanduel.com{url}"

        return {
            "title": title.strip(),
            "yes_price": yes_price,
            "no_price": no_price,
            "category": category,
            "url": url,
        }

    def _parse_price_text(self, text: str) -> Optional[float]:
        """Parse price from text.

        Args:
            text: Price text (e.g., "45¢", "$0.45", "45%").

        Returns:
            Float price in dollars or None.
        """
        if not text:
            return None

        # Clean the text
        cleaned = text.strip().replace(",", "")

        try:
            # Handle cent notation (45¢)
            if "¢" in cleaned:
                return float(cleaned.replace("¢", "")) / 100

            # Handle dollar notation ($0.45)
            if "$" in cleaned:
                return float(cleaned.replace("$", ""))

            # Handle percentage (45%)
            if "%" in cleaned:
                return float(cleaned.replace("%", "")) / 100

            # Handle plain number
            value = float(cleaned)
            # Assume cents if > 1
            return value / 100 if value > 1 else value

        except ValueError:
            return None

    def _parse_market(self, raw: dict) -> MarketData:
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
            end_date=None,  # Would need to parse from page
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
        all_markets = []

        try:
            categories = [category] if category else self.CATEGORIES

            for cat in categories:
                self.logger.debug("Scraping category", category=cat)
                raw_markets = await self._scrape_category(page, cat)

                for raw in raw_markets:
                    try:
                        market = self._parse_market(raw)
                        all_markets.append(market)
                    except Exception as e:
                        self.logger.warning("Failed to parse market", error=str(e))

                # Rate limit between categories
                await asyncio.sleep(2)

        finally:
            await page.close()

        self.logger.info("Fetched FanDuel markets", count=len(all_markets))
        return all_markets

    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ID."""
        markets = await self.fetch_markets()
        for market in markets:
            if market.platform_market_id == market_id:
                return market
        return None
