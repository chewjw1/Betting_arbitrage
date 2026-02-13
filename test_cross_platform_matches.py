"""Test script: Fetch markets from Kalshi, Polymarket, PredictIt
and run cross-platform matching to identify matches and bugs.

Usage:
    python test_cross_platform_matches.py
"""

import asyncio
import sys
import os
import traceback
from decimal import Decimal

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Override DB URL to avoid needing postgres
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test.db")
os.environ.setdefault("DATABASE_URL_SYNC", "sqlite:///test.db")

# Stub out playwright before any collector imports touch it
import types
playwright_stub = types.ModuleType("playwright")
playwright_stub.async_api = types.ModuleType("playwright.async_api")
for name in ["async_playwright", "Browser", "Page", "BrowserContext", "Playwright"]:
    setattr(playwright_stub.async_api, name, None)
sys.modules["playwright"] = playwright_stub
sys.modules["playwright.async_api"] = playwright_stub.async_api


async def fetch_polymarket():
    """Fetch markets from Polymarket."""
    from src.collectors.polymarket import PolymarketCollector

    print("\n--- Fetching Polymarket ---")
    collector = PolymarketCollector()
    try:
        async with collector:
            markets = await collector.fetch_markets(max_markets=500)
            print(f"  Fetched {len(markets)} markets from Polymarket")
            # Show a few examples
            for m in markets[:3]:
                print(f"    Example: '{m.title}' YES=${m.yes_price} url={m.url}")
            return markets
    except Exception as e:
        print(f"  ERROR fetching Polymarket: {e}")
        traceback.print_exc()
        return []


async def fetch_kalshi():
    """Fetch markets from Kalshi."""
    from src.collectors.kalshi import KalshiCollector

    print("\n--- Fetching Kalshi ---")
    collector = KalshiCollector()
    try:
        async with collector:
            # Fetch more from Kalshi since most early results are sports (filtered)
            markets = await collector.fetch_markets(max_markets=2000)
            print(f"  Fetched {len(markets)} non-sports markets from Kalshi")
            for m in markets[:3]:
                print(f"    Example: '{m.title}' YES=${m.yes_price} url={m.url}")
            return markets
    except Exception as e:
        print(f"  ERROR fetching Kalshi: {e}")
        traceback.print_exc()
        return []


async def fetch_predictit():
    """Fetch markets from PredictIt."""
    from src.collectors.predictit import PredictItCollector

    print("\n--- Fetching PredictIt ---")
    collector = PredictItCollector()
    try:
        async with collector:
            markets = await collector.fetch_markets()
            print(f"  Fetched {len(markets)} markets from PredictIt")
            for m in markets[:3]:
                print(f"    Example: '{m.title}' YES=${m.yes_price} url={m.url}")
            return markets
    except Exception as e:
        print(f"  ERROR fetching PredictIt: {e}")
        traceback.print_exc()
        return []


async def fetch_draftkings():
    """Fetch markets from DraftKings Predictions."""
    from src.collectors.draftkings import DraftKingsCollector

    print("\n--- Fetching DraftKings ---")
    # Only fetch non-sports for cross-platform arbitrage matching
    collector = DraftKingsCollector(include_sports=False)
    try:
        async with collector:
            markets = await collector.fetch_markets()
            print(f"  Fetched {len(markets)} non-sports markets from DraftKings")
            for m in markets[:3]:
                bid_ask = f"bid={m.yes_bid} ask={m.yes_ask}" if m.yes_bid or m.yes_ask else ""
                print(f"    Example: '{m.title}' YES=${m.yes_price} {bid_ask}")
            return markets
    except Exception as e:
        print(f"  ERROR fetching DraftKings: {e}")
        traceback.print_exc()
        return []


async def run_matching(markets_by_platform):
    """Run cross-platform matching on all platform pairs."""
    from src.matching.fuzzy_matcher import MarketMatcher

    # Set up LLM validator if API key is available
    llm_validator = None
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        from src.matching.llm_validator import LLMMatchValidator
        llm_validator = LLMMatchValidator(api_key=api_key)
        print("\n  LLM validation ENABLED (using GPT-4o-mini)")
    else:
        print("\n  LLM validation DISABLED (set OPENAI_API_KEY to enable)")

    matcher = MarketMatcher(min_confidence=0.65, llm_validator=llm_validator)
    platforms = list(markets_by_platform.keys())

    all_matches = []
    total_fuzzy_candidates = 0

    for i, platform_a in enumerate(platforms):
        for platform_b in platforms[i + 1:]:
            markets_a = markets_by_platform[platform_a]
            markets_b = markets_by_platform[platform_b]

            if not markets_a or not markets_b:
                print(f"\n  Skipping {platform_a} vs {platform_b} (empty)")
                continue

            print(f"\n--- Matching {platform_a} ({len(markets_a)}) vs {platform_b} ({len(markets_b)}) ---")

            try:
                # Get unfiltered count for comparison
                fuzzy_matches = matcher.find_matches(markets_a, markets_b, max_date_diff_days=30)
                fuzzy_count = len(fuzzy_matches)
                total_fuzzy_candidates += fuzzy_count

                # Apply LLM validation
                matches = await matcher.find_matches_validated(markets_a, markets_b, max_date_diff_days=30)
                print(f"  Found {len(matches)} matches (fuzzy candidates: {fuzzy_count})")

                for market_a, market_b, confidence in matches:
                    all_matches.append((market_a, market_b, confidence))
                    print(f"\n  MATCH (confidence={confidence:.2%}):")
                    print(f"    {platform_a}: '{market_a.title}'")
                    bid_ask_a = f"  bid={market_a.yes_bid} ask={market_a.yes_ask}" if market_a.yes_bid else ""
                    print(f"      YES=${market_a.yes_price}{bid_ask_a}  url={market_a.url}")
                    print(f"    {platform_b}: '{market_b.title}'")
                    bid_ask_b = f"  bid={market_b.yes_bid} ask={market_b.yes_ask}" if market_b.yes_bid else ""
                    print(f"      YES=${market_b.yes_price}{bid_ask_b}  url={market_b.url}")

                    # Check for arbitrage using execution prices (bid/ask)
                    if market_a.yes_price and market_b.yes_price:
                        # Buy YES = pay the ask; Buy NO = pay (1 - yes_bid) or no_ask
                        buy_yes_a = market_a.yes_ask or market_a.yes_price
                        buy_yes_b = market_b.yes_ask or market_b.yes_price
                        buy_no_a = market_a.no_ask or (Decimal("1") - market_a.yes_bid if market_a.yes_bid else Decimal("1") - market_a.yes_price)
                        buy_no_b = market_b.no_ask or (Decimal("1") - market_b.yes_bid if market_b.yes_bid else Decimal("1") - market_b.yes_price)

                        cost_1 = buy_yes_a + buy_no_b  # Buy YES on A, NO on B
                        cost_2 = buy_no_a + buy_yes_b  # Buy NO on A, YES on B
                        best_cost = min(cost_1, cost_2)

                        using_bidask = any([market_a.yes_ask, market_a.yes_bid, market_b.yes_ask, market_b.yes_bid])
                        price_label = "bid/ask" if using_bidask else "midpoint"

                        if best_cost < Decimal("1"):
                            spread = Decimal("1") - best_cost
                            if cost_1 < cost_2:
                                direction = f"Buy YES on {platform_a} @ ${float(buy_yes_a):.2f} + Buy NO on {platform_b} @ ${float(buy_no_b):.2f}"
                            else:
                                direction = f"Buy NO on {platform_a} @ ${float(buy_no_a):.2f} + Buy YES on {platform_b} @ ${float(buy_yes_b):.2f}"
                            print(f"    >>> ARBITRAGE: {float(spread)*100:.2f}% gross spread ({price_label} prices)")
                            print(f"    >>> Strategy: {direction}")
                            print(f"    >>> Cost: ${float(best_cost):.4f} for $1.00 payout")
                        else:
                            diff = float(abs(market_a.yes_price - market_b.yes_price)) * 100
                            print(f"    Price diff: {diff:.2f}% (no arb at {price_label} prices, cost >= $1)")

            except Exception as e:
                print(f"  ERROR matching {platform_a} vs {platform_b}: {e}")
                traceback.print_exc()

    if llm_validator and total_fuzzy_candidates > 0:
        print(f"\n  LLM filter: {total_fuzzy_candidates} fuzzy candidates -> {len(all_matches)} validated matches")

    return all_matches


def test_normalizer():
    """Test the normalizer on known tricky cases."""
    from src.matching.fuzzy_matcher import MarketMatcher

    matcher = MarketMatcher(min_confidence=0.65)

    test_cases = [
        # (title_a, platform_a, title_b, platform_b, expected_match)
        # --- True matches ---
        ("Will BTC close above $100,000?", "kalshi", "Bitcoin above $100k?", "polymarket", True),
        ("Will Trump win the 2026 election?", "kalshi", "Who will win the 2026 presidential election? - Donald Trump", "predictit", True),
        ("Will the Fed cut rates in March?", "kalshi", "Federal Reserve rate cut by March 2026?", "polymarket", True),
        ("S&P 500 above 6000 by March?", "kalshi", "SPX above 6000 in Q1?", "polymarket", True),
        ("Government shutdown in 2026?", "kalshi", "Govt shutdown before July?", "polymarket", True),
        # --- False matches: different numbers ---
        ("Will Bitcoin hit $200k by end of 2026?", "kalshi", "Bitcoin above $100k?", "polymarket", False),
        # --- False matches: different people ---
        ("Will Trump win?", "kalshi", "Will Harris win?", "polymarket", False),
        # --- Bug fixes: these should NOT match ---
        # Bug 1: First-name-only overlap (Mark Cuban vs Mark Kelly)
        ("Who will run for office? - Mark Cuban", "predictit", "Who will run for office? - Mark Kelly", "predictit", False),
        # Bug 2: Person vs party (Tim Walz vs Democratic)
        ("Will Tim Walz win the primary?", "polymarket", "Which party will win? - Democratic", "predictit", False),
        # Bug 3: Person vs party (Rand Paul vs Republican)
        ("Will Rand Paul win the election?", "polymarket", "Which party will win? - Republican", "predictit", False),
        # Bug 4: Generational suffix (Trump vs Trump Jr)
        ("Will Trump win the election?", "kalshi", "Will Trump Jr run for office?", "polymarket", False),
        # Bug 5: Different market types (win primary vs endorse)
        ("Will Trump win the primary?", "kalshi", "Will Trump endorse a candidate?", "polymarket", False),
        # Bug 6: Binary vs bucket (control Senate vs 48 seats)
        ("Will Democrats control the Senate?", "polymarket", "How many Senate seats? - 48", "predictit", False),
        # Bug 7: Inverse polarity (uphold vs strike down)
        ("Will the Supreme Court uphold transgender sports bans?", "kalshi", "Will SCOTUS strike down transgender sports bans?", "predictit", False),
        # Bug 8: Different country scope (Japan House vs US House)
        ("Will any party win at least 233 seats in the 2026 Japan House of Representatives election?", "kalshi", "Which party will win the 2026 US House election in Maine's 2nd District? - Republican", "predictit", False),
        # Bug 9: Multi-state sweep vs single state
        ("Will Democrats win the 2026 senate elections in Georgia, Michigan, North Carolina, AND Maine?", "kalshi", "Which party will win the 2026 US Senate election in North Carolina? - Democratic", "predictit", False),
        # Bug 10: Count/range vs specific person (pardon count vs pardon specific person)
        ("Will Donald Trump pardon between 3 and 9 people before Mar 1, 2026?", "kalshi", "Will Trump pardon Elon Musk in 2026?", "predictit", False),
        # Bug 11: On ballot vs win election (different market types)
        ("Will Flávio Bolsonaro be on the ballot in the next Brazilian presidential election?", "kalshi", "Who will win the 2026 Brazilian presidential election? - Flávio Bolsonaro", "predictit", False),
        # Bug 12: Any independent win anywhere vs specific state independent
        ("Will any independent or third-party candidate win an election in the U.S. House or Senate in 2026?", "kalshi", "Which party will win the 2026 US Senate election in Alaska? - Independent", "predictit", False),
        # Bug 13: Nominee vs vote share (George Conway nominee vs >5% vote)
        ("Will George Conway be the Democratic nominee for NY-12?", "kalshi", "Will George Conway get over 5% of the vote in the NY-12 Dem primary?", "predictit", False),
    ]

    print("\n=== NORMALIZER UNIT TESTS ===\n")

    passed = 0
    failed = 0

    for title_a, plat_a, title_b, plat_b, expected in test_cases:
        norm_a = matcher.normalize_title(title_a, plat_a)
        norm_b = matcher.normalize_title(title_b, plat_b)
        score = matcher.calculate_similarity(title_a, title_b, plat_a, plat_b)
        is_match = score >= 0.65

        status = "PASS" if is_match == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1

        print(f"  [{status}] score={score:.2%} (expect {'match' if expected else 'no match'})")
        print(f"    A: '{title_a}' -> '{norm_a}'")
        print(f"    B: '{title_b}' -> '{norm_b}'")
        print()

    print(f"  Results: {passed} passed, {failed} failed out of {len(test_cases)}")
    return failed == 0


async def main():
    print("=" * 70)
    print("CROSS-PLATFORM MATCHING TEST")
    print("=" * 70)

    # First run normalizer unit tests
    normalizer_ok = test_normalizer()

    # Fetch from all 4 platforms in parallel
    print("\n" + "=" * 70)
    print("LIVE PLATFORM FETCH")
    print("=" * 70)

    results = await asyncio.gather(
        fetch_polymarket(),
        fetch_kalshi(),
        fetch_predictit(),
        fetch_draftkings(),
        return_exceptions=True,
    )

    markets_by_platform = {}
    platform_names = ["polymarket", "kalshi", "predictit", "draftkings"]

    for name, result in zip(platform_names, results):
        if isinstance(result, Exception):
            print(f"\n  {name} FAILED: {result}")
            markets_by_platform[name] = []
        else:
            markets_by_platform[name] = result

    total_markets = sum(len(m) for m in markets_by_platform.values())
    print(f"\n  Total markets fetched: {total_markets}")
    for name, markets in markets_by_platform.items():
        print(f"    {name}: {len(markets)}")

    # Run cross-platform matching
    print("\n" + "=" * 70)
    print("CROSS-PLATFORM MATCHING")
    print("=" * 70)

    all_matches = await run_matching(markets_by_platform)

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Total markets: {total_markets}")
    print(f"  Total cross-platform matches: {len(all_matches)}")

    arb_count = 0
    for market_a, market_b, confidence in all_matches:
        if market_a.yes_price and market_b.yes_price:
            cost_1 = market_a.yes_price + (Decimal("1") - market_b.yes_price)
            cost_2 = (Decimal("1") - market_a.yes_price) + market_b.yes_price
            if min(cost_1, cost_2) < Decimal("1"):
                arb_count += 1

    print(f"  Matches with gross arbitrage: {arb_count}")
    print(f"  Normalizer tests: {'ALL PASSED' if normalizer_ok else 'SOME FAILED'}")

    # Check for broken URLs
    broken_urls = 0
    for market_a, market_b, _ in all_matches:
        for m in [market_a, market_b]:
            if not m.url or m.url in ("https://polymarket.com", "https://www.predictit.org", "https://kalshi.com", "https://predictions.draftkings.com"):
                broken_urls += 1
                print(f"  BROKEN URL: {m.platform} '{m.title[:50]}' -> {m.url}")

    print(f"  Broken URLs in matches: {broken_urls}")


if __name__ == "__main__":
    asyncio.run(main())
