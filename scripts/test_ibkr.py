#!/usr/bin/env python3
"""Test IBKR collector with both public and gateway APIs.

Usage:
    # Test public ForecastEx API only
    python scripts/test_ibkr.py

    # Test with gateway (must be running and authenticated)
    IBKR_GATEWAY_ENABLED=true python scripts/test_ibkr.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.collectors.ibkr import IBKRCollector
from src.config import get_settings


async def main():
    settings = get_settings()

    print("=" * 60)
    print("IBKR Collector Test")
    print("=" * 60)
    print(f"Gateway URL: {settings.ibkr_gateway_url}")
    print(f"Gateway enabled: {settings.ibkr_gateway_enabled}")
    print()

    collector = IBKRCollector()

    async with collector:
        print(f"Gateway authenticated: {collector.gateway_authenticated}")
        print()

        # Fetch markets
        markets = await collector.fetch_markets()

        print(f"Total markets found: {len(markets)}")
        print()

        # Group by source
        forecastex = [m for m in markets if not m.platform_market_id.startswith("CME-")]
        cme = [m for m in markets if m.platform_market_id.startswith("CME-")]

        print(f"ForecastEx markets: {len(forecastex)}")
        print(f"CME Event markets: {len(cme)}")
        print()

        # Show sample markets
        if forecastex:
            print("-" * 60)
            print("Sample ForecastEx Markets:")
            print("-" * 60)
            for m in forecastex[:5]:
                yes_pct = f"{float(m.yes_price)*100:.1f}%" if m.yes_price else "N/A"
                no_pct = f"{float(m.no_price)*100:.1f}%" if m.no_price else "N/A"
                print(f"  [{m.category}] {m.title[:50]}")
                print(f"    YES: {yes_pct} | NO: {no_pct}")
                print()

        if cme:
            print("-" * 60)
            print("CME Event Markets:")
            print("-" * 60)
            for m in cme[:10]:
                yes_pct = f"{float(m.yes_price)*100:.1f}%" if m.yes_price else "N/A"
                print(f"  [{m.category}] {m.title[:50]}")
                print(f"    YES: {yes_pct} | ID: {m.platform_market_id}")
                print()

        if not markets:
            print("No markets found!")
            if not collector.gateway_authenticated:
                print("\nTip: Gateway not authenticated. To access CME contracts:")
                print("  1. Start gateway: ~/workspaces/ibkr-gateway/bin/run-minimal.sh")
                print("  2. SSH tunnel: ssh -L 5000:localhost:5000 user@seedbox")
                print("  3. Login: https://localhost:5000")
                print("  4. Run this script again")


if __name__ == "__main__":
    asyncio.run(main())
