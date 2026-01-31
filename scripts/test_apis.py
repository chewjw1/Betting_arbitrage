#!/usr/bin/env python3
"""Test script to fetch data from prediction market APIs and find discrepancies."""

import asyncio
import json
import ssl
from decimal import Decimal
from typing import Optional
import httpx


async def fetch_predictit() -> list[dict]:
    """Fetch markets from PredictIt API."""
    # Create SSL context that doesn't verify (for testing)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        response = await client.get("https://www.predictit.org/api/marketdata/all/")
        data = response.json()

    markets = []
    for market in data.get("markets", []):
        for contract in market.get("contracts", []):
            yes_price = contract.get("lastTradePrice")
            if yes_price:
                markets.append({
                    "platform": "predictit",
                    "id": f"{market['id']}_{contract['id']}",
                    "title": f"{market['name']} - {contract['name']}",
                    "yes_price": float(yes_price),
                    "no_price": 1 - float(yes_price) if yes_price else None,
                    "buy_yes": contract.get("bestBuyYesCost"),
                    "buy_no": contract.get("bestBuyNoCost"),
                })
    return markets


async def fetch_polymarket() -> list[dict]:
    """Fetch markets from Polymarket Gamma API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get active markets
        response = await client.get(
            "https://gamma-api.polymarket.com/markets",
            params={"closed": "false", "limit": 100}
        )
        data = response.json()

    markets = []
    for market in data:
        # Polymarket prices are in the outcomes
        outcomes = market.get("outcomes", [])
        if len(outcomes) >= 2:
            # Find yes/no prices from outcome prices
            yes_price = None
            no_price = None

            outcome_prices = market.get("outcomePrices", "")
            if outcome_prices:
                try:
                    prices = json.loads(outcome_prices)
                    if len(prices) >= 2:
                        yes_price = float(prices[0])
                        no_price = float(prices[1])
                except:
                    pass

            if yes_price:
                markets.append({
                    "platform": "polymarket",
                    "id": market.get("id") or market.get("conditionId"),
                    "title": market.get("question", "Unknown"),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume": market.get("volume"),
                })
    return markets


def normalize_title(title: str) -> str:
    """Normalize a title for comparison."""
    import re
    # Lowercase
    t = title.lower()
    # Remove punctuation except spaces
    t = re.sub(r'[^\w\s]', '', t)
    # Normalize whitespace
    t = ' '.join(t.split())
    return t


def find_similar_markets(markets_a: list[dict], markets_b: list[dict], threshold: float = 0.7) -> list[tuple]:
    """Find markets that might be the same event across platforms."""
    from difflib import SequenceMatcher

    matches = []

    for ma in markets_a:
        title_a = normalize_title(ma["title"])
        best_match = None
        best_score = 0

        for mb in markets_b:
            title_b = normalize_title(mb["title"])

            # Calculate similarity
            score = SequenceMatcher(None, title_a, title_b).ratio()

            if score > best_score and score >= threshold:
                best_score = score
                best_match = mb

        if best_match:
            matches.append((ma, best_match, best_score))

    return matches


def check_arbitrage(market_a: dict, market_b: dict) -> Optional[dict]:
    """Check if there's an arbitrage opportunity between two markets."""
    yes_a = market_a.get("yes_price")
    yes_b = market_b.get("yes_price")

    if not yes_a or not yes_b:
        return None

    # Direction 1: Buy YES on A, Buy NO on B
    cost_1 = yes_a + (1 - yes_b)

    # Direction 2: Buy NO on A, Buy YES on B
    cost_2 = (1 - yes_a) + yes_b

    best_cost = min(cost_1, cost_2)

    if best_cost < 1.0:
        gross_spread = 1.0 - best_cost
        # Estimate fees (conservative)
        est_fees = 0.03  # ~3% total for both platforms
        net_spread = gross_spread - est_fees

        if net_spread > 0:
            return {
                "market_a": market_a,
                "market_b": market_b,
                "direction": "YES_A + NO_B" if cost_1 < cost_2 else "NO_A + YES_B",
                "cost": best_cost,
                "gross_spread_pct": gross_spread * 100,
                "net_spread_pct": net_spread * 100,
            }

    return None


def check_complement_violation(market: dict) -> Optional[dict]:
    """Check if Yes + No prices don't sum to ~1 (complement violation)."""
    buy_yes = market.get("buy_yes")
    buy_no = market.get("buy_no")

    if buy_yes and buy_no:
        total = float(buy_yes) + float(buy_no)
        if total < 0.98:  # Should be ~1.0
            return {
                "market": market,
                "yes_price": buy_yes,
                "no_price": buy_no,
                "sum": total,
                "spread_pct": (1 - total) * 100,
            }
    return None


async def main():
    print("=" * 70)
    print("PREDICTION MARKET API TEST - LIVE DATA")
    print("=" * 70)
    print()

    # Fetch from PredictIt
    print("Fetching PredictIt markets...")
    try:
        predictit_markets = await fetch_predictit()
        print(f"  Found {len(predictit_markets)} contracts")
    except Exception as e:
        print(f"  Error: {e}")
        predictit_markets = []

    # Fetch from Polymarket
    print("Fetching Polymarket markets...")
    try:
        polymarket_markets = await fetch_polymarket()
        print(f"  Found {len(polymarket_markets)} markets")
    except Exception as e:
        print(f"  Error: {e}")
        polymarket_markets = []

    print()
    print("=" * 70)
    print("SAMPLE MARKETS")
    print("=" * 70)

    print("\nPredictIt (first 5):")
    for m in predictit_markets[:5]:
        print(f"  {m['title'][:60]}")
        print(f"    YES: ${m['yes_price']:.2f}  |  BuyYes: {m.get('buy_yes')}  BuyNo: {m.get('buy_no')}")

    print("\nPolymarket (first 5):")
    for m in polymarket_markets[:5]:
        print(f"  {m['title'][:60]}")
        print(f"    YES: ${m['yes_price']:.2f}  NO: ${m.get('no_price', 0):.2f}")

    # Check for complement violations on PredictIt
    print()
    print("=" * 70)
    print("COMPLEMENT VIOLATIONS (Yes + No < $0.98)")
    print("=" * 70)

    violations = []
    for m in predictit_markets:
        v = check_complement_violation(m)
        if v:
            violations.append(v)

    if violations:
        violations.sort(key=lambda x: x["spread_pct"], reverse=True)
        print(f"\nFound {len(violations)} potential complement arbitrage opportunities:\n")
        for v in violations[:10]:
            print(f"  {v['market']['title'][:55]}")
            print(f"    BuyYes=${v['yes_price']} + BuyNo=${v['no_price']} = ${v['sum']:.2f}")
            print(f"    Gross Spread: {v['spread_pct']:.1f}%")
            print()
    else:
        print("\n  No complement violations found")

    # Find similar markets across platforms
    print()
    print("=" * 70)
    print("CROSS-PLATFORM MATCHES")
    print("=" * 70)

    matches = find_similar_markets(predictit_markets, polymarket_markets, threshold=0.5)
    print(f"\nFound {len(matches)} potential cross-platform matches:\n")

    for ma, mb, score in matches[:10]:
        print(f"  PredictIt: {ma['title'][:50]}")
        print(f"  Polymarket: {mb['title'][:50]}")
        print(f"  Similarity: {score:.0%}")
        print(f"  Prices: PI YES=${ma['yes_price']:.2f} vs PM YES=${mb['yes_price']:.2f}")

        arb = check_arbitrage(ma, mb)
        if arb:
            print(f"  >>> ARBITRAGE: {arb['direction']}, Net Spread: {arb['net_spread_pct']:.1f}%")
        print()

    # Look for any arbitrage opportunities
    print()
    print("=" * 70)
    print("ARBITRAGE OPPORTUNITIES (Net > 0%)")
    print("=" * 70)

    opportunities = []
    for ma, mb, score in matches:
        arb = check_arbitrage(ma, mb)
        if arb and arb["net_spread_pct"] > 0:
            arb["match_score"] = score
            opportunities.append(arb)

    if opportunities:
        opportunities.sort(key=lambda x: x["net_spread_pct"], reverse=True)
        print(f"\nFound {len(opportunities)} arbitrage opportunities:\n")
        for opp in opportunities[:10]:
            print(f"  {opp['market_a']['title'][:50]}")
            print(f"    PredictIt YES: ${opp['market_a']['yes_price']:.2f}")
            print(f"    Polymarket YES: ${opp['market_b']['yes_price']:.2f}")
            print(f"    Strategy: {opp['direction']}")
            print(f"    Gross: {opp['gross_spread_pct']:.1f}% | Net (est): {opp['net_spread_pct']:.1f}%")
            print()
    else:
        print("\n  No arbitrage opportunities found with current data")
        print("  (This is expected - efficient markets usually have small/no arbitrage)")

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  PredictIt markets: {len(predictit_markets)}")
    print(f"  Polymarket markets: {len(polymarket_markets)}")
    print(f"  Cross-platform matches: {len(matches)}")
    print(f"  Complement violations: {len(violations)}")
    print(f"  Arbitrage opportunities: {len(opportunities)}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    asyncio.run(main())
