#!/usr/bin/env python3
"""Scan for current arbitrage opportunities and display results."""

import asyncio
from decimal import Decimal

from src.collectors import PredictItCollector, PolymarketCollector
from src.arbitrage.logical import LogicalArbitrageDetector
from src.arbitrage.cross_platform import CrossPlatformDetector
from src.matching.fuzzy_matcher import MarketMatcher


async def scan_predictit_logical():
    """Scan PredictIt for logical arbitrage opportunities."""
    print("\n" + "=" * 60)
    print("PREDICTIT LOGICAL ARBITRAGE SCAN")
    print("=" * 60)

    async with PredictItCollector() as collector:
        markets = await collector.fetch_markets()
        print(f"\nFetched {len(markets)} PredictIt markets")

    # Check for logical opportunities
    detector = LogicalArbitrageDetector(
        min_violation_pct=1.0,
        min_net_profit_pct=1.0,  # 1% minimum after fees
        min_relationship_confidence=0.7,
    )

    opportunities = detector.find_opportunities(markets)

    print(f"\n{'='*60}")
    print(f"RESULTS: {len(opportunities)} profitable opportunities (>1% net)")
    print("=" * 60)

    if opportunities:
        for i, opp in enumerate(opportunities[:10], 1):
            rel = opp.relationship
            print(f"\n{i}. {opp.subtype_display or 'Unknown'}")
            print(f"   Market: {rel.market_a.title[:60]}...")
            print(f"   Gross: {opp.profit_opportunity_pct:.2f}%")
            print(f"   Fees:  {float(opp.estimated_fees):.2f} ({float(opp.net_profit_pct) - float(opp.profit_opportunity_pct) + float(opp.estimated_fees):.2f}%)")
            print(f"   Net:   {opp.net_profit_pct:.2f}%")
            if opp.recommended_action:
                print(f"   Action: {opp.recommended_action}")
    else:
        print("\nNo profitable opportunities found after accounting for fees.")
        print("This is expected - PredictIt's 10% profit + 5% withdrawal fees are high.")

    # Show some examples of what was filtered out
    print(f"\n{'='*60}")
    print("EXAMPLES OF FILTERED OUT (fees too high):")
    print("=" * 60)

    # Get all relationships and check them
    relationships = detector.find_relationships(markets)
    complement_rels = [r for r in relationships if r.relationship_type.value == "complement"]

    filtered_count = 0
    for rel in complement_rels[:50]:  # Check first 50
        result = detector.check_relationship(rel)
        if result.profit_opportunity_pct > Decimal("0") and not result.is_profitable:
            filtered_count += 1
            if filtered_count <= 5:  # Show first 5 examples
                print(f"\n   Market: {rel.market_a.title[:50]}...")
                print(f"   YES: ${rel.market_a.yes_price:.2f}, NO: ${rel.market_a.no_price:.2f}")
                print(f"   Sum: ${float(rel.market_a.yes_price + rel.market_a.no_price):.2f}")
                print(f"   Gross spread: {result.profit_opportunity_pct:.2f}%")
                print(f"   After fees: {result.net_profit_pct:.2f}% <- NOT PROFITABLE")

    print(f"\nTotal filtered out due to fees: {filtered_count}")


async def scan_cross_platform():
    """Scan for cross-platform arbitrage."""
    print("\n" + "=" * 60)
    print("CROSS-PLATFORM ARBITRAGE SCAN")
    print("=" * 60)

    # Fetch from multiple platforms
    all_markets = []

    try:
        async with PredictItCollector() as collector:
            markets = await collector.fetch_markets()
            all_markets.extend(markets)
            print(f"PredictIt: {len(markets)} markets")
    except Exception as e:
        print(f"PredictIt error: {e}")

    try:
        async with PolymarketCollector() as collector:
            markets = await collector.fetch_markets()
            all_markets.extend(markets)
            print(f"Polymarket: {len(markets)} markets")
    except Exception as e:
        print(f"Polymarket error: {e}")

    print(f"\nTotal markets: {len(all_markets)}")

    # Find cross-platform opportunities
    detector = CrossPlatformDetector(
        min_net_spread_pct=1.0,
        matcher=MarketMatcher(min_confidence=0.75),
    )

    opportunities = await detector.find_opportunities(all_markets)

    print(f"\n{'='*60}")
    print(f"RESULTS: {len(opportunities)} cross-platform opportunities (>1% net)")
    print("=" * 60)

    for i, opp in enumerate(opportunities[:10], 1):
        print(f"\n{i}. {opp.market_a.platform} vs {opp.market_b.platform}")
        print(f"   {opp.market_a.title[:50]}...")
        print(f"   {opp.side_a.upper()} @ ${float(opp.price_a):.2f} vs {opp.side_b.upper()} @ ${float(opp.price_b):.2f}")
        print(f"   Gross: {float(opp.gross_spread * 100):.2f}%")
        print(f"   Net:   {float(opp.net_profit_pct):.2f}%")


async def main():
    """Run all scans."""
    print("\n" + "#" * 60)
    print("# ARBITRAGE OPPORTUNITY SCANNER")
    print("# With PredictIt withdrawal fee fix")
    print("#" * 60)

    await scan_predictit_logical()
    # Uncomment to also scan cross-platform:
    # await scan_cross_platform()

    print("\n" + "#" * 60)
    print("# SCAN COMPLETE")
    print("#" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
