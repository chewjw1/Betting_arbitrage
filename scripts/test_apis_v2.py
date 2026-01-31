#!/usr/bin/env python3
"""Test script v2 - Better matching and more platforms."""

import asyncio
import json
import re
from decimal import Decimal
from typing import Optional
import httpx


async def fetch_predictit() -> list[dict]:
    """Fetch markets from PredictIt API."""
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        response = await client.get("https://www.predictit.org/api/marketdata/all/")
        data = response.json()

    markets = []
    for market in data.get("markets", []):
        for contract in market.get("contracts", []):
            yes_price = contract.get("lastTradePrice")
            buy_yes = contract.get("bestBuyYesCost")
            buy_no = contract.get("bestBuyNoCost")
            if yes_price:
                markets.append({
                    "platform": "predictit",
                    "id": f"{market['id']}_{contract['id']}",
                    "title": f"{market['name']} - {contract['name']}",
                    "short_title": contract['name'],
                    "market_name": market['name'],
                    "yes_price": float(yes_price),
                    "no_price": 1 - float(yes_price),
                    "buy_yes": float(buy_yes) if buy_yes else None,
                    "buy_no": float(buy_no) if buy_no else None,
                })
    return markets


async def fetch_polymarket() -> list[dict]:
    """Fetch markets from Polymarket Gamma API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            "https://gamma-api.polymarket.com/markets",
            params={"closed": "false", "limit": 500}
        )
        data = response.json()

    markets = []
    for market in data:
        outcome_prices = market.get("outcomePrices", "")
        if outcome_prices:
            try:
                prices = json.loads(outcome_prices)
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    no_price = float(prices[1])
                    markets.append({
                        "platform": "polymarket",
                        "id": market.get("id") or market.get("conditionId"),
                        "title": market.get("question", "Unknown"),
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "volume": float(market.get("volume", 0)) if market.get("volume") else 0,
                    })
            except:
                pass
    return markets


def extract_keywords(title: str) -> set:
    """Extract meaningful keywords from a title."""
    # Lowercase and remove punctuation
    t = title.lower()
    t = re.sub(r'[^\w\s]', ' ', t)
    words = t.split()

    # Remove common stop words
    stop_words = {
        'will', 'the', 'a', 'an', 'be', 'to', 'in', 'on', 'at', 'by', 'for',
        'of', 'or', 'and', 'is', 'are', 'was', 'were', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'shall', 'should', 'would',
        'could', 'may', 'might', 'must', 'can', 'this', 'that', 'these', 'those',
        'before', 'after', 'during', 'than', 'more', 'less', 'not', 'yes', 'no',
        'what', 'when', 'where', 'who', 'how', 'which', 'any', 'all', 'each',
    }

    keywords = {w for w in words if w not in stop_words and len(w) > 2}
    return keywords


def calculate_keyword_overlap(title_a: str, title_b: str) -> float:
    """Calculate overlap between keywords."""
    kw_a = extract_keywords(title_a)
    kw_b = extract_keywords(title_b)

    if not kw_a or not kw_b:
        return 0.0

    intersection = kw_a & kw_b
    union = kw_a | kw_b

    # Jaccard similarity
    return len(intersection) / len(union) if union else 0


def find_matching_markets(predictit: list[dict], polymarket: list[dict]) -> list[tuple]:
    """Find markets that are likely the same event."""
    matches = []

    for pi in predictit:
        for pm in polymarket:
            # Calculate keyword overlap
            overlap = calculate_keyword_overlap(pi["title"], pm["title"])

            # Require higher threshold (60% keyword overlap)
            if overlap >= 0.4:
                matches.append((pi, pm, overlap))

    # Sort by overlap
    matches.sort(key=lambda x: x[2], reverse=True)
    return matches


def check_arbitrage(market_a: dict, market_b: dict, fees: dict = None) -> Optional[dict]:
    """Check for arbitrage opportunity with fee calculations."""
    if fees is None:
        fees = {
            "predictit": {"trading": 0, "profit": 0.10, "withdrawal": 0.05},  # 10% profit + 5% withdrawal
            "polymarket": {"trading": 0.0001, "profit": 0, "withdrawal": 0},  # 0.01%
        }

    yes_a = market_a.get("yes_price")
    yes_b = market_b.get("yes_price")

    if not yes_a or not yes_b:
        return None

    # Direction 1: Buy YES on A, Buy NO on B
    cost_1 = yes_a + (1 - yes_b)
    # Direction 2: Buy NO on A, Buy YES on B
    cost_2 = (1 - yes_a) + yes_b

    if cost_1 < cost_2:
        direction = "YES_A + NO_B"
        cost = cost_1
        win_on_a = True  # If event happens, we win on A
    else:
        direction = "NO_A + YES_B"
        cost = cost_2
        win_on_a = False

    gross_spread = 1.0 - cost

    if gross_spread <= 0:
        return None

    # Calculate fees (simplified)
    # PredictIt: 10% on profit + 5% on withdrawal
    # Polymarket: 0.01% on trade
    fee_a = fees.get(market_a["platform"], {})
    fee_b = fees.get(market_b["platform"], {})

    # Estimate total fees as % of profit
    profit_fee_rate = max(
        fee_a.get("profit", 0) + fee_a.get("withdrawal", 0),
        fee_b.get("profit", 0) + fee_b.get("withdrawal", 0)
    )

    # Net spread after fees
    net_spread = gross_spread * (1 - profit_fee_rate)

    if net_spread > 0:
        return {
            "market_a": market_a,
            "market_b": market_b,
            "direction": direction,
            "cost": cost,
            "gross_spread_pct": gross_spread * 100,
            "fees_pct": (gross_spread - net_spread) * 100,
            "net_spread_pct": net_spread * 100,
        }
    return None


def check_complement_arbitrage(markets: list[dict]) -> list[dict]:
    """Check for complement violations (Yes + No < 1) within a platform."""
    violations = []

    for m in markets:
        buy_yes = m.get("buy_yes")
        buy_no = m.get("buy_no")

        if buy_yes is not None and buy_no is not None:
            total = buy_yes + buy_no
            if total < 0.97:  # Allow some spread
                gross_profit = 1 - total
                # PredictIt fees: 10% profit + 5% withdrawal
                net_profit = gross_profit * (1 - 0.15) if m["platform"] == "predictit" else gross_profit

                if net_profit > 0.01:  # > 1% after fees
                    violations.append({
                        "market": m,
                        "buy_yes": buy_yes,
                        "buy_no": buy_no,
                        "sum": total,
                        "gross_pct": gross_profit * 100,
                        "net_pct": net_profit * 100,
                    })

    violations.sort(key=lambda x: x["net_pct"], reverse=True)
    return violations


async def main():
    print("=" * 70)
    print("PREDICTION MARKET ARBITRAGE SCANNER - LIVE TEST")
    print("=" * 70)
    print()

    # Fetch data
    print("Fetching market data...")
    predictit_markets = await fetch_predictit()
    print(f"  PredictIt: {len(predictit_markets)} contracts")

    polymarket_markets = await fetch_polymarket()
    print(f"  Polymarket: {len(polymarket_markets)} markets")

    print()
    print("=" * 70)
    print("1. COMPLEMENT ARBITRAGE (Buy Yes + Buy No < $1.00)")
    print("=" * 70)

    complement_violations = check_complement_arbitrage(predictit_markets)

    if complement_violations:
        print(f"\nFound {len(complement_violations)} potential opportunities:\n")
        for v in complement_violations[:10]:
            m = v["market"]
            print(f"  {m['market_name'][:50]}")
            print(f"  Contract: {m['short_title'][:40]}")
            print(f"    Buy Yes: ${v['buy_yes']:.2f} + Buy No: ${v['buy_no']:.2f} = ${v['sum']:.2f}")
            print(f"    Gross Profit: {v['gross_pct']:.1f}% | Net (after 15% fee): {v['net_pct']:.1f}%")
            print()
    else:
        print("\n  No complement arbitrage found (market is efficient)")

    print()
    print("=" * 70)
    print("2. CROSS-PLATFORM MATCHES (PredictIt vs Polymarket)")
    print("=" * 70)

    matches = find_matching_markets(predictit_markets, polymarket_markets)

    print(f"\nFound {len(matches)} potential cross-platform matches:\n")

    arbitrage_opps = []

    for pi, pm, overlap in matches[:20]:
        print(f"  Match (overlap: {overlap:.0%}):")
        print(f"    PI: {pi['title'][:55]}")
        print(f"    PM: {pm['title'][:55]}")
        print(f"    Prices: PI Yes=${pi['yes_price']:.2f} vs PM Yes=${pm['yes_price']:.2f}")

        arb = check_arbitrage(pi, pm)
        if arb:
            print(f"    >>> ARBITRAGE: {arb['direction']}")
            print(f"        Gross: {arb['gross_spread_pct']:.1f}% | Net: {arb['net_spread_pct']:.1f}%")
            arbitrage_opps.append(arb)
        print()

    print()
    print("=" * 70)
    print("3. VIABLE ARBITRAGE OPPORTUNITIES (Net > 1%)")
    print("=" * 70)

    viable = [a for a in arbitrage_opps if a["net_spread_pct"] > 1.0]

    if viable:
        print(f"\nFound {len(viable)} opportunities with >1% net profit:\n")
        for arb in viable:
            print(f"  {arb['market_a']['title'][:50]}")
            print(f"  vs {arb['market_b']['title'][:50]}")
            print(f"    Strategy: {arb['direction']}")
            print(f"    Cost: ${arb['cost']:.2f} for $1.00 payout")
            print(f"    Gross: {arb['gross_spread_pct']:.1f}% | Fees: ~{arb['fees_pct']:.1f}% | Net: {arb['net_spread_pct']:.1f}%")
            print()
    else:
        print("\n  No viable arbitrage found with >1% net profit")
        print("  (Markets are relatively efficient across platforms)")

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Markets scanned: {len(predictit_markets) + len(polymarket_markets)}")
    print(f"  Cross-platform matches: {len(matches)}")
    print(f"  Complement violations: {len(complement_violations)}")
    print(f"  Cross-platform arbitrage: {len(arbitrage_opps)}")
    print(f"  Viable (>1% net): {len(viable)}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    asyncio.run(main())
