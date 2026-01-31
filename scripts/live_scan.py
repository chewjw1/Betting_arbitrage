#!/usr/bin/env python3
"""Live scan across all API markets to find current arbitrage opportunities.

This script:
1. Fetches markets from PredictIt, Kalshi, and Polymarket
2. Checks for cross-platform arbitrage (same event, different prices)
3. Checks for logical arbitrage (complement violations, etc.)
4. Reports any opportunities found

Usage:
    python scripts/live_scan.py
"""

import asyncio
import ssl
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
import urllib.request
import json


def fetch_json(url: str, headers: dict = None) -> dict:
    """Fetch JSON from URL with SSL workaround."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_predictit() -> list[dict]:
    """Fetch all PredictIt markets."""
    print("Fetching PredictIt markets...")
    data = fetch_json("https://www.predictit.org/api/marketdata/all/")

    markets = []
    for market in data.get("markets", []):
        for contract in market.get("contracts", []):
            if contract.get("status") == "Open":
                yes_price = contract.get("lastTradePrice") or contract.get("bestBuyYesCost")
                if yes_price:
                    markets.append({
                        "platform": "predictit",
                        "id": str(contract["id"]),
                        "title": f"{market['name']}: {contract['name']}",
                        "short_title": contract["name"],
                        "category": market.get("name", ""),
                        "yes_price": Decimal(str(yes_price)),
                        "no_price": Decimal(str(1 - yes_price)) if yes_price else None,
                        "url": market.get("url"),
                        "end_date": contract.get("dateEnd"),
                    })

    print(f"  Found {len(markets)} PredictIt contracts")
    return markets


def fetch_polymarket() -> list[dict]:
    """Fetch Polymarket markets via Gamma API."""
    print("Fetching Polymarket markets...")
    try:
        # Get active markets
        data = fetch_json(
            "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500",
            headers={"Accept": "application/json"}
        )

        markets = []
        for market in data:
            if market.get("active") and not market.get("closed"):
                # outcomePrices is a JSON string like "[\"0.95\", \"0.05\"]"
                outcome_prices = market.get("outcomePrices")
                if outcome_prices:
                    try:
                        prices = json.loads(outcome_prices)
                        if len(prices) >= 2:
                            yes_price = Decimal(prices[0])
                            no_price = Decimal(prices[1])

                            markets.append({
                                "platform": "polymarket",
                                "id": market.get("conditionId", market.get("id", "")),
                                "title": market.get("question", ""),
                                "yes_price": yes_price,
                                "no_price": no_price,
                                "url": f"https://polymarket.com/event/{market.get('slug', '')}",
                                "volume": market.get("volume"),
                                "end_date": market.get("endDate"),
                            })
                    except (json.JSONDecodeError, IndexError):
                        pass

        print(f"  Found {len(markets)} Polymarket markets")
        return markets
    except Exception as e:
        print(f"  Error fetching Polymarket: {e}")
        return []


def fetch_kalshi() -> list[dict]:
    """Fetch Kalshi markets (public endpoint)."""
    print("Fetching Kalshi markets...")
    try:
        data = fetch_json(
            "https://api.elections.kalshi.com/trade-api/v2/markets?limit=200&status=open",
            headers={"Accept": "application/json"}
        )

        markets = []
        for market in data.get("markets", []):
            if market.get("status") == "active":
                # yes_price in cents (0-100)
                yes_bid = market.get("yes_bid")
                yes_ask = market.get("yes_ask")

                if yes_bid or yes_ask:
                    # Use midpoint if both available
                    if yes_bid and yes_ask:
                        yes_price = Decimal(str((yes_bid + yes_ask) / 2 / 100))
                    else:
                        yes_price = Decimal(str((yes_bid or yes_ask) / 100))

                    markets.append({
                        "platform": "kalshi",
                        "id": market.get("ticker", ""),
                        "title": market.get("title", ""),
                        "yes_price": yes_price,
                        "no_price": Decimal("1") - yes_price,
                        "url": f"https://kalshi.com/markets/{market.get('ticker', '')}",
                        "volume": market.get("volume"),
                        "end_date": market.get("close_time"),
                    })

        print(f"  Found {len(markets)} Kalshi markets")
        return markets
    except Exception as e:
        print(f"  Error fetching Kalshi: {e}")
        return []


def normalize_title(title: str) -> str:
    """Normalize market title for matching."""
    import re
    title = title.lower()
    # Remove common words
    for word in ["will", "the", "be", "in", "by", "on", "a", "an", "to", "of", "for"]:
        title = re.sub(rf'\b{word}\b', '', title)
    # Keep alphanumeric and spaces
    title = re.sub(r'[^a-z0-9\s]', '', title)
    # Collapse whitespace
    title = ' '.join(title.split())
    return title


def similarity(a: str, b: str) -> float:
    """Calculate string similarity."""
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def extract_keywords(title: str) -> set:
    """Extract key terms from a title."""
    import re
    title = title.lower()
    # Remove common words
    stop_words = {"will", "the", "be", "in", "by", "on", "a", "an", "to", "of", "for",
                  "yes", "no", "before", "after", "during", "market", "contract"}
    words = re.findall(r'\b[a-z]+\b', title)
    return {w for w in words if w not in stop_words and len(w) > 2}


def find_cross_platform_opportunities(all_markets: dict[str, list]) -> list:
    """Find arbitrage opportunities across platforms."""
    opportunities = []

    platforms = list(all_markets.keys())

    for i, platform_a in enumerate(platforms):
        for platform_b in platforms[i+1:]:
            markets_a = all_markets[platform_a]
            markets_b = all_markets[platform_b]

            for market_a in markets_a:
                keywords_a = extract_keywords(market_a["title"])

                for market_b in markets_b:
                    # Quick keyword check first
                    keywords_b = extract_keywords(market_b["title"])
                    common = keywords_a & keywords_b

                    # Need significant overlap
                    if len(common) < 2:
                        continue

                    # Calculate similarity
                    sim = similarity(market_a["title"], market_b["title"])
                    if sim < 0.5:
                        continue

                    # Calculate arbitrage
                    yes_a = market_a["yes_price"]
                    yes_b = market_b["yes_price"]

                    # Direction 1: Buy YES on A, NO on B
                    cost_1 = yes_a + (Decimal("1") - yes_b)
                    # Direction 2: Buy NO on A, YES on B
                    cost_2 = (Decimal("1") - yes_a) + yes_b

                    best_cost = min(cost_1, cost_2)
                    if best_cost < Decimal("1"):
                        gross_spread = Decimal("1") - best_cost
                        gross_pct = float(gross_spread * 100)

                        # Estimate fees (rough: 5% combined)
                        estimated_fee_pct = 5.0
                        if "predictit" in [platform_a, platform_b]:
                            estimated_fee_pct = 12.0  # PredictIt has high fees
                        elif "polymarket" in [platform_a, platform_b]:
                            estimated_fee_pct = 3.0  # Polymarket has low fees

                        net_pct = gross_pct - estimated_fee_pct

                        if net_pct > 0:
                            if cost_1 < cost_2:
                                action = f"Buy YES on {platform_a} @ {float(yes_a):.2f}, NO on {platform_b} @ {float(1-yes_b):.2f}"
                            else:
                                action = f"Buy NO on {platform_a} @ {float(1-yes_a):.2f}, YES on {platform_b} @ {float(yes_b):.2f}"

                            opportunities.append({
                                "type": "cross_platform",
                                "platform_a": platform_a,
                                "platform_b": platform_b,
                                "market_a": market_a["title"][:80],
                                "market_b": market_b["title"][:80],
                                "similarity": sim,
                                "gross_spread_pct": gross_pct,
                                "estimated_fee_pct": estimated_fee_pct,
                                "net_profit_pct": net_pct,
                                "action": action,
                                "url_a": market_a.get("url"),
                                "url_b": market_b.get("url"),
                            })

    return sorted(opportunities, key=lambda x: -x["net_profit_pct"])


def find_complement_violations(markets: list) -> list:
    """Find markets where YES + NO prices don't sum to ~1."""
    violations = []

    for market in markets:
        yes_price = market["yes_price"]
        no_price = market.get("no_price")

        if no_price is None:
            no_price = Decimal("1") - yes_price

        total = yes_price + no_price
        deviation = abs(total - Decimal("1"))

        # Significant deviation (> 2%)
        if deviation > Decimal("0.02"):
            violations.append({
                "type": "complement_violation",
                "platform": market["platform"],
                "title": market["title"][:80],
                "yes_price": float(yes_price),
                "no_price": float(no_price),
                "total": float(total),
                "deviation_pct": float(deviation * 100),
                "action": "Buy BOTH" if total < 1 else "N/A (can't short)",
                "url": market.get("url"),
            })

    return sorted(violations, key=lambda x: -x["deviation_pct"])


def main():
    """Run live scan."""
    print("=" * 60)
    print("LIVE ARBITRAGE SCAN")
    print(f"Time: {datetime.utcnow().isoformat()}")
    print("=" * 60)
    print()

    # Fetch from all platforms
    all_markets = {}

    # PredictIt (most reliable)
    pi_markets = fetch_predictit()
    if pi_markets:
        all_markets["predictit"] = pi_markets

    # Polymarket
    poly_markets = fetch_polymarket()
    if poly_markets:
        all_markets["polymarket"] = poly_markets

    # Kalshi
    kalshi_markets = fetch_kalshi()
    if kalshi_markets:
        all_markets["kalshi"] = kalshi_markets

    print()
    print(f"Total markets fetched: {sum(len(m) for m in all_markets.values())}")
    print()

    # Find cross-platform opportunities
    print("-" * 60)
    print("CROSS-PLATFORM ARBITRAGE OPPORTUNITIES")
    print("-" * 60)

    cross_opps = find_cross_platform_opportunities(all_markets)

    if cross_opps:
        for i, opp in enumerate(cross_opps[:10], 1):  # Top 10
            print(f"\n{i}. {opp['platform_a'].upper()} vs {opp['platform_b'].upper()}")
            print(f"   Market A: {opp['market_a']}")
            print(f"   Market B: {opp['market_b']}")
            print(f"   Similarity: {opp['similarity']:.0%}")
            print(f"   Gross Spread: {opp['gross_spread_pct']:.2f}%")
            print(f"   Est. Fees: {opp['estimated_fee_pct']:.1f}%")
            print(f"   NET PROFIT: {opp['net_profit_pct']:.2f}%")
            print(f"   Action: {opp['action']}")
            if opp.get("url_a"):
                print(f"   URL A: {opp['url_a']}")
            if opp.get("url_b"):
                print(f"   URL B: {opp['url_b']}")
    else:
        print("\nNo cross-platform arbitrage opportunities found.")
        print("(Markets are efficiently priced across platforms)")

    # Find complement violations (logical arbitrage within platform)
    print()
    print("-" * 60)
    print("COMPLEMENT VIOLATIONS (YES + NO != 100%)")
    print("-" * 60)

    all_flat = []
    for markets in all_markets.values():
        all_flat.extend(markets)

    violations = find_complement_violations(all_flat)

    if violations:
        for i, v in enumerate(violations[:10], 1):
            print(f"\n{i}. [{v['platform'].upper()}] {v['title']}")
            print(f"   YES: {v['yes_price']:.2f} + NO: {v['no_price']:.2f} = {v['total']:.2f}")
            print(f"   Deviation: {v['deviation_pct']:.2f}%")
            print(f"   Action: {v['action']}")
    else:
        print("\nNo complement violations found.")
        print("(All YES + NO prices sum to ~100%)")

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Platforms scanned: {list(all_markets.keys())}")
    print(f"Total markets: {sum(len(m) for m in all_markets.values())}")
    print(f"Cross-platform opportunities: {len(cross_opps)}")
    print(f"  - Above 3% net: {len([o for o in cross_opps if o['net_profit_pct'] >= 3])}")
    print(f"Complement violations: {len(violations)}")
    print()

    if cross_opps and cross_opps[0]["net_profit_pct"] >= 3:
        print("🎯 ACTIONABLE OPPORTUNITY FOUND!")
        print(f"   Best opportunity: {cross_opps[0]['net_profit_pct']:.2f}% net profit")
    else:
        print("No actionable opportunities at this time (threshold: 3% net)")


if __name__ == "__main__":
    main()
