#!/usr/bin/env python3
"""Run a live scan and validate cross-platform matches.

This script:
1. Fetches markets from all 4 API platforms
2. Runs fuzzy matching to find candidates
3. Validates candidates through LLM (if OPENAI_API_KEY set)
4. Shows all validated opportunities with prices

Usage:
    OPENAI_API_KEY=sk-... python scripts/validate_scan.py
"""

import asyncio
import os
import sys
from decimal import Decimal
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    print("=" * 80)
    print("PREDICTION MARKET ARBITRAGE - LIVE VALIDATION SCAN")
    print(f"Time: {datetime.utcnow().isoformat()}Z")
    print("=" * 80)

    # Check for OpenAI key
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        print(f"\n✓ OpenAI API key found (using GPT-4o-mini for validation)")
    else:
        print(f"\n⚠ No OPENAI_API_KEY - will skip LLM validation")

    # Import collectors
    from src.collectors import (
        KalshiCollector,
        PolymarketCollector,
        PredictItCollector,
        DraftKingsCollector,
    )
    from src.matching.fuzzy_matcher import MarketMatcher
    from src.arbitrage.calculator import ArbitrageCalculator

    # Optional LLM validator
    llm_validator = None
    if openai_key:
        from src.matching.llm_validator import LLMMatchValidator
        llm_validator = LLMMatchValidator(api_key=openai_key, model="gpt-4o-mini")

    # Collect from all platforms
    print("\n" + "-" * 40)
    print("STEP 1: Collecting markets from APIs")
    print("-" * 40)

    markets_by_platform = {}

    collectors = [
        ("kalshi", KalshiCollector()),
        ("polymarket", PolymarketCollector()),
        ("predictit", PredictItCollector()),
        ("draftkings", DraftKingsCollector(include_sports=False)),
    ]

    for platform, collector in collectors:
        try:
            async with collector:
                markets = await collector.fetch_markets()
                markets_by_platform[platform] = markets
                print(f"  ✓ {platform}: {len(markets)} markets")
        except Exception as e:
            print(f"  ✗ {platform}: ERROR - {str(e)[:60]}")
            markets_by_platform[platform] = []

    total_markets = sum(len(m) for m in markets_by_platform.values())
    print(f"\n  Total: {total_markets} markets across {len(markets_by_platform)} platforms")

    # Find cross-platform matches
    print("\n" + "-" * 40)
    print("STEP 2: Finding cross-platform matches")
    print("-" * 40)

    matcher = MarketMatcher(min_confidence=0.65, llm_validator=llm_validator)

    platforms = list(markets_by_platform.keys())
    all_matches = []
    llm_calls = 0
    llm_rejections = 0

    for i, platform_a in enumerate(platforms):
        for platform_b in platforms[i + 1:]:
            markets_a = markets_by_platform[platform_a]
            markets_b = markets_by_platform[platform_b]

            if not markets_a or not markets_b:
                continue

            print(f"\n  Comparing {platform_a} ({len(markets_a)}) vs {platform_b} ({len(markets_b)})...")

            # First get fuzzy matches (no LLM yet)
            fuzzy_matches = matcher.find_matches(markets_a, markets_b)
            print(f"    Fuzzy candidates: {len(fuzzy_matches)}")

            if fuzzy_matches and llm_validator:
                # Now validate with LLM
                validated = []
                for market_a, market_b, confidence in fuzzy_matches:
                    llm_calls += 1
                    is_same = await llm_validator.validate_match(
                        title_a=market_a.title,
                        platform_a=market_a.platform,
                        title_b=market_b.title,
                        platform_b=market_b.platform,
                    )
                    if is_same:
                        validated.append((market_a, market_b, confidence))
                    else:
                        llm_rejections += 1
                        print(f"      ✗ LLM rejected: '{market_a.title[:40]}...' vs '{market_b.title[:40]}...'")

                print(f"    LLM validated: {len(validated)} (rejected {len(fuzzy_matches) - len(validated)})")
                all_matches.extend(validated)
            else:
                all_matches.extend(fuzzy_matches)

    print(f"\n  Total validated matches: {len(all_matches)}")
    if llm_validator:
        print(f"  LLM calls made: {llm_calls}")
        print(f"  LLM rejections: {llm_rejections}")
        if llm_calls > 0:
            print(f"  Rejection rate: {llm_rejections/llm_calls*100:.1f}%")

    # Calculate arbitrage opportunities
    print("\n" + "-" * 40)
    print("STEP 3: Calculating arbitrage opportunities")
    print("-" * 40)

    calculator = ArbitrageCalculator(min_net_spread_pct=0.0)  # Show all, even negative

    opportunities = []
    for market_a, market_b, confidence in all_matches:
        result = calculator.calculate_cross_platform(market_a, market_b)
        if result:
            opportunities.append((result, confidence))

    # Sort by net profit
    opportunities.sort(key=lambda x: x[0].net_profit_pct, reverse=True)

    # Show results
    print(f"\n  Found {len(opportunities)} cross-platform comparisons")

    # Categorize by profitability
    profitable_3pct = [o for o in opportunities if o[0].net_profit_pct >= 3]
    profitable_1pct = [o for o in opportunities if 1 <= o[0].net_profit_pct < 3]
    breakeven = [o for o in opportunities if 0 <= o[0].net_profit_pct < 1]
    negative = [o for o in opportunities if o[0].net_profit_pct < 0]

    print(f"\n  Breakdown:")
    print(f"    ≥3% net profit (actionable): {len(profitable_3pct)}")
    print(f"    1-3% net profit (marginal):  {len(profitable_1pct)}")
    print(f"    0-1% net profit (breakeven): {len(breakeven)}")
    print(f"    <0% net profit (no arb):     {len(negative)}")

    # Show top opportunities
    if opportunities:
        print("\n" + "-" * 40)
        print("TOP OPPORTUNITIES (by net profit %)")
        print("-" * 40)

        for i, (result, confidence) in enumerate(opportunities[:15]):
            status = "🟢" if result.net_profit_pct >= 3 else "🟡" if result.net_profit_pct >= 1 else "⚪"
            print(f"\n{status} #{i+1}: Net {float(result.net_profit_pct):.2f}% | Gross {float(result.gross_spread)*100:.1f}% | Fees ${float(result.total_fees):.2f}")
            print(f"   {result.market_a.platform}: {result.side_a.upper()} @ {float(result.price_a):.2f} - {result.market_a.title[:60]}")
            print(f"   {result.market_b.platform}: {result.side_b.upper()} @ {float(result.price_b):.2f} - {result.market_b.title[:60]}")
            print(f"   Match confidence: {confidence:.1%}")
            if result.market_a.url:
                print(f"   URL A: {result.market_a.url}")
            if result.market_b.url:
                print(f"   URL B: {result.market_b.url}")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Markets scanned:        {total_markets}")
    print(f"Cross-platform matches: {len(all_matches)}")
    print(f"Actionable (≥3% net):   {len(profitable_3pct)}")

    if llm_validator:
        stats = llm_validator.get_cache_stats()
        print(f"LLM cache size:         {stats['memory_cache_size']}")

    if len(profitable_3pct) == 0:
        print("\n💡 No actionable opportunities found. Markets appear efficiently priced.")
        print("   This is normal - real arbitrage is rare. Keep scanning!")
    elif len(profitable_3pct) > 10:
        print("\n⚠️  High number of opportunities - may indicate false positives.")
        print("   Review the matches above to verify they're the same events.")
    else:
        print(f"\n✅ Found {len(profitable_3pct)} potential opportunities to review.")

    return opportunities


if __name__ == "__main__":
    results = asyncio.run(main())
