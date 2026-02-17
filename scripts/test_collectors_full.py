#!/usr/bin/env python3
"""Test all API collectors using the actual collector classes.

Usage:
    python scripts/test_collectors_full.py
"""

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Patch settings to avoid needing a full .env
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test.db")
os.environ.setdefault("DATABASE_URL_SYNC", "sqlite:///test.db")


async def test_collector(name, collector_cls, **kwargs):
    """Test a single collector."""
    print(f"\n{'='*60}")
    print(f"  {name.upper()}")
    print(f"{'='*60}")

    try:
        collector = collector_cls(**kwargs)
        async with collector:
            markets = await collector.fetch_markets()

        print(f"  Markets fetched: {len(markets)}")

        if not markets:
            print(f"  RESULT: FAIL (0 markets)")
            return False

        # Show stats
        with_bid_ask = sum(1 for m in markets if m.yes_bid is not None)
        with_volume = sum(1 for m in markets if m.total_volume is not None)
        with_end_date = sum(1 for m in markets if m.end_date is not None)

        print(f"  With bid/ask:    {with_bid_ask}/{len(markets)}")
        print(f"  With volume:     {with_volume}/{len(markets)}")
        print(f"  With end_date:   {with_end_date}/{len(markets)}")

        # Show 3 samples
        print(f"\n  Top 3 markets:")
        for i, m in enumerate(markets[:3]):
            print(f"  [{i+1}] {m.title[:70]}")
            print(f"      YES: {float(m.yes_price):.2f}" if m.yes_price else "      YES: N/A", end="")
            if m.yes_bid is not None:
                print(f"  (bid:{float(m.yes_bid):.2f} ask:{float(m.yes_ask):.2f})" if m.yes_ask else f"  (bid:{float(m.yes_bid):.2f})", end="")
            print()
            if m.url:
                print(f"      URL: {m.url[:70]}")

        # Show categories if available
        cats = set(m.category for m in markets if m.category)
        if cats:
            print(f"\n  Categories: {', '.join(sorted(cats)[:5])}")

        print(f"\n  RESULT: PASS")
        return True

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        print(f"\n  RESULT: FAIL")
        return False


async def main():
    print("=" * 60)
    print("  FULL COLLECTOR VALIDATION")
    print(f"  {datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    from src.collectors import (
        KalshiCollector,
        PolymarketCollector,
        PredictItCollector,
        DraftKingsCollector,
    )

    results = {}
    results["polymarket"] = await test_collector("Polymarket", PolymarketCollector)
    results["predictit"] = await test_collector("PredictIt", PredictItCollector)
    results["kalshi"] = await test_collector("Kalshi", KalshiCollector)
    results["draftkings"] = await test_collector("DraftKings", DraftKingsCollector, include_sports=False)

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    for platform, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  {platform:15s} {status}")
    passing = sum(1 for v in results.values() if v)
    print(f"\n  {passing}/{len(results)} collectors working")

    if passing < len(results):
        failed = [k for k, v in results.items() if not v]
        print(f"  FAILED: {', '.join(failed)}")


if __name__ == "__main__":
    asyncio.run(main())
