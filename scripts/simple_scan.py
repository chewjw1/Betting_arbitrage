#!/usr/bin/env python3
"""Simple live scan without dependencies that might fail.

Tests the API connections and fuzzy matching without Kalshi RSA auth.
"""

import asyncio
import os
import sys
from decimal import Decimal
from datetime import datetime
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx


async def fetch_polymarket():
    """Fetch markets from Polymarket."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        markets = []
        response = await client.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": True, "closed": False, "limit": 500}
        )
        response.raise_for_status()
        data = response.json()

        for m in data:
            if isinstance(m, dict) and m.get("question"):
                # Get price from outcomePrices
                yes_price = None
                if m.get("outcomePrices"):
                    prices = m["outcomePrices"]
                    if isinstance(prices, str):
                        try:
                            prices = json.loads(prices)
                        except:
                            prices = []
                    if isinstance(prices, list) and len(prices) >= 1:
                        yes_price = float(prices[0])

                if yes_price and yes_price > 0:
                    markets.append({
                        "platform": "polymarket",
                        "title": m.get("question", ""),
                        "yes_price": yes_price,
                        "url": f"https://polymarket.com/event/{m.get('slug', '')}",
                    })
        return markets


async def fetch_predictit():
    """Fetch markets from PredictIt."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get("https://www.predictit.org/api/marketdata/all/")
        response.raise_for_status()
        data = response.json()

        markets = []
        for market in data.get("markets", []):
            for contract in market.get("contracts", []):
                if contract.get("lastTradePrice"):
                    markets.append({
                        "platform": "predictit",
                        "title": f"{market.get('name', '')} - {contract.get('name', '')}",
                        "yes_price": contract["lastTradePrice"],
                        "url": market.get("url", ""),
                    })
        return markets


async def fetch_kalshi_public():
    """Fetch markets from Kalshi public endpoint."""
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        markets = []
        # Use the public series endpoint for elections
        try:
            response = await client.get(
                "https://api.elections.kalshi.com/v1/markets",
                params={"limit": 200}
            )
            response.raise_for_status()
            data = response.json()

            for m in data.get("markets", []):
                yes_price = None
                if m.get("yes_bid"):
                    yes_price = m["yes_bid"] / 100
                elif m.get("last_price"):
                    yes_price = m["last_price"] / 100

                if yes_price and yes_price > 0:
                    markets.append({
                        "platform": "kalshi",
                        "title": m.get("title", ""),
                        "yes_price": yes_price,
                        "url": f"https://kalshi.com/markets/{m.get('ticker', '')}",
                    })
        except Exception as e:
            print(f"  Kalshi public API error: {e}")
        return markets


def fuzzy_match(title_a: str, title_b: str) -> float:
    """Simple fuzzy matching using token overlap."""
    # Normalize
    a_tokens = set(title_a.lower().split())
    b_tokens = set(title_b.lower().split())

    # Remove common stop words
    stop_words = {"will", "the", "a", "an", "in", "on", "at", "to", "for", "of", "be", "by", "?", "-"}
    a_tokens -= stop_words
    b_tokens -= stop_words

    if not a_tokens or not b_tokens:
        return 0.0

    # Jaccard similarity
    intersection = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)

    return intersection / union if union > 0 else 0.0


def find_matches(markets_a: list, markets_b: list, threshold: float = 0.4) -> list:
    """Find matching markets across platforms."""
    matches = []

    for ma in markets_a:
        for mb in markets_b:
            if ma["platform"] == mb["platform"]:
                continue

            score = fuzzy_match(ma["title"], mb["title"])
            if score >= threshold:
                # Calculate potential arbitrage
                price_a = ma["yes_price"]
                price_b = mb["yes_price"]

                # Check if buying YES on A and NO on B makes sense
                cost_yes_no = price_a + (1 - price_b)
                # Or buying NO on A and YES on B
                cost_no_yes = (1 - price_a) + price_b

                best_cost = min(cost_yes_no, cost_no_yes)
                gross_profit = (1 - best_cost) * 100  # as percentage

                matches.append({
                    "market_a": ma,
                    "market_b": mb,
                    "match_score": score,
                    "gross_profit_pct": gross_profit,
                    "best_strategy": "YES_A + NO_B" if cost_yes_no < cost_no_yes else "NO_A + YES_B",
                })

    # Sort by profit potential
    matches.sort(key=lambda x: x["gross_profit_pct"], reverse=True)
    return matches


async def main():
    print("=" * 80)
    print("PREDICTION MARKET - SIMPLE VALIDATION SCAN")
    print(f"Time: {datetime.utcnow().isoformat()}Z")
    print("=" * 80)

    # Fetch from all platforms
    print("\n--- Fetching Markets ---")

    polymarket_markets = await fetch_polymarket()
    print(f"Polymarket: {len(polymarket_markets)} markets")

    predictit_markets = await fetch_predictit()
    print(f"PredictIt:  {len(predictit_markets)} markets")

    kalshi_markets = await fetch_kalshi_public()
    print(f"Kalshi:     {len(kalshi_markets)} markets")

    all_markets = polymarket_markets + predictit_markets + kalshi_markets
    print(f"\nTotal: {len(all_markets)} markets")

    # Find cross-platform matches
    print("\n--- Finding Cross-Platform Matches ---")

    # Polymarket vs PredictIt
    pm_pi_matches = find_matches(polymarket_markets, predictit_markets)
    print(f"Polymarket ↔ PredictIt: {len(pm_pi_matches)} matches")

    # Polymarket vs Kalshi
    pm_k_matches = find_matches(polymarket_markets, kalshi_markets)
    print(f"Polymarket ↔ Kalshi:    {len(pm_k_matches)} matches")

    # PredictIt vs Kalshi
    pi_k_matches = find_matches(predictit_markets, kalshi_markets)
    print(f"PredictIt ↔ Kalshi:     {len(pi_k_matches)} matches")

    all_matches = pm_pi_matches + pm_k_matches + pi_k_matches
    all_matches.sort(key=lambda x: x["gross_profit_pct"], reverse=True)

    print(f"\nTotal matches: {len(all_matches)}")

    # Categorize
    actionable = [m for m in all_matches if m["gross_profit_pct"] >= 5]  # 5% gross ~ 3% net after fees
    marginal = [m for m in all_matches if 2 <= m["gross_profit_pct"] < 5]
    no_arb = [m for m in all_matches if m["gross_profit_pct"] < 2]

    print(f"\n--- Breakdown ---")
    print(f"≥5% gross (potentially actionable): {len(actionable)}")
    print(f"2-5% gross (marginal after fees):   {len(marginal)}")
    print(f"<2% gross (no arbitrage):           {len(no_arb)}")

    # Show top matches
    if all_matches:
        print("\n" + "=" * 80)
        print("TOP 20 MATCHES (by gross profit potential)")
        print("=" * 80)

        for i, match in enumerate(all_matches[:20]):
            ma = match["market_a"]
            mb = match["market_b"]
            status = "🟢" if match["gross_profit_pct"] >= 5 else "🟡" if match["gross_profit_pct"] >= 2 else "⚪"

            print(f"\n{status} #{i+1}: Gross {match['gross_profit_pct']:.1f}% | Match {match['match_score']:.0%}")
            print(f"   {ma['platform']}: YES @ {ma['yes_price']:.2f} - {ma['title'][:65]}")
            print(f"   {mb['platform']}: YES @ {mb['yes_price']:.2f} - {mb['title'][:65]}")
            print(f"   Strategy: {match['best_strategy']}")
            if ma.get("url"):
                print(f"   URL: {ma['url']}")

    # Summary
    print("\n" + "=" * 80)
    print("ASSESSMENT")
    print("=" * 80)

    if len(actionable) == 0:
        print("✅ No obvious false positives - markets appear efficiently priced")
        print("   This is expected. Real arbitrage opportunities are rare.")
    elif len(actionable) > 10:
        print(f"⚠️  {len(actionable)} potential opportunities found")
        print("   High count may indicate false positive matches - review manually")
        print("   LLM validation would help filter these")
    else:
        print(f"🔍 {len(actionable)} matches worth investigating")
        print("   Review the matches above to verify they're the same events")

    return all_matches


if __name__ == "__main__":
    asyncio.run(main())
