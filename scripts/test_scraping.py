#!/usr/bin/env python3
"""Test scraping of DraftKings, FanDuel, and IBKR."""

import asyncio
from playwright.async_api import async_playwright


async def scrape_draftkings():
    """Scrape DraftKings Predictions."""
    print("=" * 60)
    print("DRAFTKINGS PREDICTIONS")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            print("Navigating to predictions.draftkings.com...")
            await page.goto("https://predictions.draftkings.com", timeout=30000)
            await page.wait_for_timeout(3000)

            # Get page title
            title = await page.title()
            print(f"Page title: {title}")

            # Take screenshot
            await page.screenshot(path="/tmp/dk_screenshot.png")
            print("Screenshot saved to /tmp/dk_screenshot.png")

            # Try to find market elements
            content = await page.content()
            print(f"Page content length: {len(content)} chars")

            # Look for any text that might be prices
            selectors = [
                '[class*="market"]',
                '[class*="prediction"]',
                '[class*="card"]',
                'article',
            ]

            for sel in selectors:
                elements = await page.query_selector_all(sel)
                if elements:
                    print(f"Found {len(elements)} elements with selector '{sel}'")

        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()


async def scrape_fanduel():
    """Scrape FanDuel Predicts."""
    print()
    print("=" * 60)
    print("FANDUEL PREDICTS")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            print("Navigating to fanduel.com/predicts...")
            await page.goto("https://www.fanduel.com/predicts", timeout=30000)
            await page.wait_for_timeout(3000)

            title = await page.title()
            print(f"Page title: {title}")

            # Check URL (might redirect)
            url = page.url
            print(f"Current URL: {url}")

            await page.screenshot(path="/tmp/fd_screenshot.png")
            print("Screenshot saved to /tmp/fd_screenshot.png")

            content = await page.content()
            print(f"Page content length: {len(content)} chars")

        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()


async def scrape_ibkr():
    """Scrape IBKR ForecastTrader."""
    print()
    print("=" * 60)
    print("IBKR FORECASTTRADER")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            print("Navigating to forecasttrader.interactivebrokers.com...")
            await page.goto("https://forecasttrader.interactivebrokers.com", timeout=30000)
            await page.wait_for_timeout(3000)

            title = await page.title()
            print(f"Page title: {title}")

            url = page.url
            print(f"Current URL: {url}")

            await page.screenshot(path="/tmp/ibkr_screenshot.png")
            print("Screenshot saved to /tmp/ibkr_screenshot.png")

            content = await page.content()
            print(f"Page content length: {len(content)} chars")

            # Try to find market elements
            selectors = [
                '[class*="market"]',
                '[class*="contract"]',
                '[class*="forecast"]',
                'table tr',
            ]

            for sel in selectors:
                elements = await page.query_selector_all(sel)
                if elements:
                    print(f"Found {len(elements)} elements with selector '{sel}'")

        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()


async def main():
    await scrape_draftkings()
    await scrape_fanduel()
    await scrape_ibkr()

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print("Screenshots saved to /tmp/")
    print("Check if the pages loaded correctly and contain market data.")


if __name__ == "__main__":
    asyncio.run(main())
