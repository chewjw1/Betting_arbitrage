#!/usr/bin/env python3
"""Health check script - tests all data sources and reports status.

Usage:
    python scripts/health_check.py

This will test:
- API collectors: PredictIt, Polymarket, Kalshi
- Scraping collectors: DraftKings, FanDuel, IBKR (if Playwright available)
"""

import asyncio
import sys
import time
from datetime import datetime


def test_api_sources():
    """Test API-based data sources (no Playwright needed)."""
    import ssl
    import urllib.request
    import json

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    results = {}

    # PredictIt
    print("\n[1/3] Testing PredictIt API...")
    try:
        start = time.time()
        req = urllib.request.Request("https://www.predictit.org/api/marketdata/all/")
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            markets = data.get("markets", [])
            contracts = sum(len(m.get("contracts", [])) for m in markets)
            elapsed = time.time() - start
            results["predictit"] = {"status": "OK", "markets": len(markets), "contracts": contracts, "time": f"{elapsed:.1f}s"}
            print(f"  ✓ PredictIt: {contracts} contracts in {elapsed:.1f}s")
    except Exception as e:
        results["predictit"] = {"status": "FAILED", "error": str(e)}
        print(f"  ✗ PredictIt: {e}")

    # Polymarket
    print("\n[2/3] Testing Polymarket API...")
    try:
        start = time.time()
        req = urllib.request.Request("https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            count = len(data) if isinstance(data, list) else 0
            elapsed = time.time() - start
            results["polymarket"] = {"status": "OK", "markets": count, "time": f"{elapsed:.1f}s"}
            print(f"  ✓ Polymarket: {count} markets in {elapsed:.1f}s")
    except Exception as e:
        results["polymarket"] = {"status": "FAILED", "error": str(e)}
        print(f"  ✗ Polymarket: {e}")

    # Kalshi
    print("\n[3/3] Testing Kalshi API...")
    try:
        start = time.time()
        req = urllib.request.Request("https://api.elections.kalshi.com/trade-api/v2/markets?limit=100&status=open")
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            count = len(data.get("markets", []))
            elapsed = time.time() - start
            results["kalshi"] = {"status": "OK", "markets": count, "time": f"{elapsed:.1f}s"}
            print(f"  ✓ Kalshi: {count} markets in {elapsed:.1f}s")
    except Exception as e:
        results["kalshi"] = {"status": "FAILED", "error": str(e)}
        print(f"  ✗ Kalshi: {e}")

    return results


async def test_scraping_sources():
    """Test scraping-based data sources (needs Playwright)."""
    results = {}

    # Check if Playwright is available
    try:
        from playwright.async_api import async_playwright
        print("\n✓ Playwright is installed")
    except ImportError:
        print("\n✗ Playwright not installed - scraping unavailable")
        print("  Install with: pip install playwright && playwright install chromium")
        return {
            "draftkings": {"status": "SKIPPED", "reason": "Playwright not installed"},
            "fanduel": {"status": "SKIPPED", "reason": "Playwright not installed"},
            "ibkr": {"status": "SKIPPED", "reason": "Playwright not installed"},
        }

    # Test browser launch
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            print("✓ Chromium browser launched successfully")

            # DraftKings
            print("\n[1/3] Testing DraftKings scraping...")
            try:
                start = time.time()
                page = await browser.new_page()
                await page.goto("https://pick6.draftkings.com/", timeout=30000)
                await page.wait_for_timeout(2000)
                title = await page.title()
                content_len = len(await page.content())
                await page.close()
                elapsed = time.time() - start

                if content_len > 1000:
                    results["draftkings"] = {"status": "OK", "page_title": title, "content_size": f"{content_len} chars", "time": f"{elapsed:.1f}s"}
                    print(f"  ✓ DraftKings: Page loaded ({content_len} chars) in {elapsed:.1f}s")
                else:
                    results["draftkings"] = {"status": "BLOCKED", "reason": "Page too small, likely blocked"}
                    print(f"  ⚠ DraftKings: May be blocked (only {content_len} chars)")
            except Exception as e:
                results["draftkings"] = {"status": "FAILED", "error": str(e)}
                print(f"  ✗ DraftKings: {e}")

            # FanDuel
            print("\n[2/3] Testing FanDuel scraping...")
            try:
                start = time.time()
                page = await browser.new_page()
                await page.goto("https://www.fanduel.com/", timeout=30000)
                await page.wait_for_timeout(2000)
                title = await page.title()
                content_len = len(await page.content())
                await page.close()
                elapsed = time.time() - start

                if content_len > 1000:
                    results["fanduel"] = {"status": "OK", "page_title": title, "content_size": f"{content_len} chars", "time": f"{elapsed:.1f}s"}
                    print(f"  ✓ FanDuel: Page loaded ({content_len} chars) in {elapsed:.1f}s")
                else:
                    results["fanduel"] = {"status": "BLOCKED", "reason": "Page too small"}
                    print(f"  ⚠ FanDuel: May be blocked (only {content_len} chars)")
            except Exception as e:
                results["fanduel"] = {"status": "FAILED", "error": str(e)}
                print(f"  ✗ FanDuel: {e}")

            # IBKR
            print("\n[3/3] Testing IBKR ForecastTrader scraping...")
            try:
                start = time.time()
                page = await browser.new_page()
                await page.goto("https://www.interactivebrokers.com/en/trading/forecasttrader.php", timeout=30000)
                await page.wait_for_timeout(2000)
                title = await page.title()
                content_len = len(await page.content())
                await page.close()
                elapsed = time.time() - start

                if content_len > 1000:
                    results["ibkr"] = {"status": "OK", "page_title": title, "content_size": f"{content_len} chars", "time": f"{elapsed:.1f}s"}
                    print(f"  ✓ IBKR: Page loaded ({content_len} chars) in {elapsed:.1f}s")
                else:
                    results["ibkr"] = {"status": "BLOCKED", "reason": "Page too small"}
                    print(f"  ⚠ IBKR: May be blocked (only {content_len} chars)")
            except Exception as e:
                results["ibkr"] = {"status": "FAILED", "error": str(e)}
                print(f"  ✗ IBKR: {e}")

            await browser.close()

    except Exception as e:
        print(f"\n✗ Browser launch failed: {e}")
        print("  You may need to install browser dependencies:")
        print("  sudo apt-get install -y libnss3 libatk1.0-0 libgbm1 libasound2")
        return {
            "draftkings": {"status": "FAILED", "error": str(e)},
            "fanduel": {"status": "FAILED", "error": str(e)},
            "ibkr": {"status": "FAILED", "error": str(e)},
        }

    return results


def main():
    print("=" * 60)
    print("PREDICTION MARKET DATA SOURCE HEALTH CHECK")
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 60)

    # Test API sources
    print("\n" + "-" * 60)
    print("API DATA SOURCES")
    print("-" * 60)
    api_results = test_api_sources()

    # Test scraping sources
    print("\n" + "-" * 60)
    print("SCRAPING DATA SOURCES (requires Playwright)")
    print("-" * 60)
    scraping_results = asyncio.run(test_scraping_sources())

    # Summary
    all_results = {**api_results, **scraping_results}

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    api_ok = sum(1 for k in ["predictit", "polymarket", "kalshi"] if all_results.get(k, {}).get("status") == "OK")
    scrape_ok = sum(1 for k in ["draftkings", "fanduel", "ibkr"] if all_results.get(k, {}).get("status") == "OK")
    scrape_skip = sum(1 for k in ["draftkings", "fanduel", "ibkr"] if all_results.get(k, {}).get("status") == "SKIPPED")

    print(f"\nAPI Sources:      {api_ok}/3 working")
    print(f"Scraping Sources: {scrape_ok}/3 working" + (f" ({scrape_skip} skipped)" if scrape_skip else ""))

    print("\nDetailed Status:")
    for source, result in all_results.items():
        status = result.get("status", "UNKNOWN")
        if status == "OK":
            extra = result.get("markets") or result.get("contracts") or result.get("content_size", "")
            print(f"  ✓ {source:12} OK - {extra}")
        elif status == "SKIPPED":
            print(f"  ⊘ {source:12} SKIPPED - {result.get('reason', '')}")
        else:
            print(f"  ✗ {source:12} {status} - {result.get('error', result.get('reason', ''))}")

    # Exit code
    if api_ok < 2:
        print("\n⚠ WARNING: Less than 2 API sources working!")
        sys.exit(1)

    print("\n✓ Health check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
