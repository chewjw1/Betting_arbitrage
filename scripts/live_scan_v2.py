#!/usr/bin/env python3
"""Live scan v2 - with better Polymarket support and detailed logging."""

import asyncio
import ssl
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
import urllib.request
import json
import re


def fetch_json(url: str, headers: dict = None, timeout: int = 30) -> dict:
    """Fetch JSON from URL with SSL workaround."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
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
    """Fetch Polymarket markets via multiple API endpoints."""
    print("Fetching Polymarket markets...")

    # Try the CLOB API (central limit order book)
    endpoints = [
        "https://clob.polymarket.com/markets",
        "https://strapi-matic.poly.market/markets?_limit=500&active=true",
    ]

    for endpoint in endpoints:
        try:
            data = fetch_json(endpoint, timeout=15)

            markets = []
            items = data if isinstance(data, list) else data.get("data", data.get("markets", []))

            for market in items:
                # Different field names depending on API
                question = market.get("question") or market.get("title") or market.get("description", "")

                # Get prices - different formats
                if "tokens" in market:
                    # CLOB format
                    tokens = market.get("tokens", [])
                    if len(tokens) >= 2:
                        yes_price = Decimal(str(tokens[0].get("price", 0.5)))
                        no_price = Decimal(str(tokens[1].get("price", 0.5)))
                elif "outcomePrices" in market:
                    # Gamma API format
                    try:
                        prices = json.loads(market["outcomePrices"])
                        yes_price = Decimal(prices[0])
                        no_price = Decimal(prices[1])
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue
                else:
                    continue

                if question and yes_price > 0:
                    markets.append({
                        "platform": "polymarket",
                        "id": market.get("condition_id") or market.get("conditionId") or market.get("id", ""),
                        "title": question,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "url": f"https://polymarket.com/event/{market.get('slug', '')}",
                        "volume": market.get("volume") or market.get("volumeNum"),
                        "end_date": market.get("endDate") or market.get("end_date_iso"),
                    })

            if markets:
                print(f"  Found {len(markets)} Polymarket markets (via {endpoint.split('/')[2]})")
                return markets

        except Exception as e:
            print(f"  Endpoint {endpoint} failed: {e}")
            continue

    print("  Could not fetch Polymarket markets (all endpoints failed)")
    return []


def fetch_kalshi() -> list[dict]:
    """Fetch Kalshi markets."""
    print("Fetching Kalshi markets...")

    # Try elections API first (public), then main API
    endpoints = [
        "https://api.elections.kalshi.com/trade-api/v2/markets?limit=500&status=open",
        "https://trading-api.kalshi.com/trade-api/v2/markets?limit=500&status=open",
    ]

    for endpoint in endpoints:
        try:
            data = fetch_json(endpoint)
            markets_data = data.get("markets", [])

            markets = []
            for market in markets_data:
                if market.get("status") not in ["active", "open"]:
                    continue

                yes_bid = market.get("yes_bid", 0)
                yes_ask = market.get("yes_ask", 0)

                if yes_bid or yes_ask:
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

            if markets:
                print(f"  Found {len(markets)} Kalshi markets")
                return markets

        except Exception as e:
            print(f"  Endpoint failed: {e}")
            continue

    return []


def normalize_title(title: str) -> str:
    """Normalize market title for matching."""
    title = title.lower()
    # Remove punctuation and extra spaces
    title = re.sub(r'[^\w\s]', ' ', title)
    title = ' '.join(title.split())
    return title


def extract_entities(title: str) -> dict:
    """Extract key entities from title."""
    title_lower = title.lower()

    # Common entity patterns
    entities = {
        "people": [],
        "orgs": [],
        "dates": [],
        "numbers": [],
        "topics": [],
    }

    # People (common in prediction markets)
    people_patterns = [
        r'\b(trump|biden|harris|desantis|newsom|vance|pence|pelosi|mcconnell)\b',
        r'\b(musk|bezos|zuckerberg)\b',
        r'\b(powell|yellen|gensler)\b',
    ]
    for pattern in people_patterns:
        matches = re.findall(pattern, title_lower)
        entities["people"].extend(matches)

    # Dates/years
    date_matches = re.findall(r'\b(202[4-9]|203[0-9])\b', title)
    entities["dates"].extend(date_matches)

    # Topics
    topic_patterns = [
        r'\b(president|election|vote|nomination)\b',
        r'\b(fed|interest rate|inflation|gdp|unemployment)\b',
        r'\b(bitcoin|btc|ethereum|eth|crypto)\b',
        r'\b(super bowl|world series|nba|nfl)\b',
    ]
    for pattern in topic_patterns:
        matches = re.findall(pattern, title_lower)
        entities["topics"].extend(matches)

    return entities


def markets_match(market_a: dict, market_b: dict) -> tuple[bool, float, str]:
    """Check if two markets are about the same event.

    Returns: (is_match, confidence, reason)
    """
    title_a = market_a["title"]
    title_b = market_b["title"]

    # Extract entities
    entities_a = extract_entities(title_a)
    entities_b = extract_entities(title_b)

    # Check for shared entities
    shared_people = set(entities_a["people"]) & set(entities_b["people"])
    shared_dates = set(entities_a["dates"]) & set(entities_b["dates"])
    shared_topics = set(entities_a["topics"]) & set(entities_b["topics"])

    # Scoring
    score = 0.0
    reasons = []

    if shared_people:
        score += 0.3
        reasons.append(f"same people: {shared_people}")

    if shared_dates:
        score += 0.2
        reasons.append(f"same dates: {shared_dates}")

    if shared_topics:
        score += 0.2
        reasons.append(f"same topics: {shared_topics}")

    # String similarity
    sim = SequenceMatcher(None, normalize_title(title_a), normalize_title(title_b)).ratio()
    if sim > 0.6:
        score += 0.3
        reasons.append(f"title similarity: {sim:.0%}")

    return (score >= 0.5, score, "; ".join(reasons))


def calculate_arbitrage(market_a: dict, market_b: dict) -> dict | None:
    """Calculate arbitrage opportunity between two markets."""
    yes_a = market_a["yes_price"]
    yes_b = market_b["yes_price"]

    # Direction 1: Buy YES on A, NO on B
    cost_1 = yes_a + (Decimal("1") - yes_b)
    # Direction 2: Buy NO on A, YES on B
    cost_2 = (Decimal("1") - yes_a) + yes_b

    best_cost = min(cost_1, cost_2)

    if best_cost >= Decimal("1"):
        return None  # No arbitrage

    gross_spread = Decimal("1") - best_cost
    gross_pct = float(gross_spread * 100)

    # Fee estimation
    platforms = {market_a["platform"], market_b["platform"]}
    if "predictit" in platforms:
        fee_pct = 12.0  # 10% profit fee + ~5% transaction
    elif "kalshi" in platforms and "polymarket" in platforms:
        fee_pct = 3.0  # Both low fee
    else:
        fee_pct = 5.0  # Default estimate

    net_pct = gross_pct - fee_pct

    if net_pct <= 0:
        return None

    if cost_1 < cost_2:
        direction = "Buy YES on A, NO on B"
        price_a = float(yes_a)
        side_a = "YES"
        price_b = float(1 - yes_b)
        side_b = "NO"
    else:
        direction = "Buy NO on A, YES on B"
        price_a = float(1 - yes_a)
        side_a = "NO"
        price_b = float(yes_b)
        side_b = "YES"

    return {
        "gross_pct": gross_pct,
        "fee_pct": fee_pct,
        "net_pct": net_pct,
        "direction": direction,
        "side_a": side_a,
        "price_a": price_a,
        "side_b": side_b,
        "price_b": price_b,
    }


def find_opportunities(all_markets: dict[str, list]) -> list:
    """Find all arbitrage opportunities."""
    opportunities = []

    platforms = list(all_markets.keys())

    # Track potential matches for logging
    potential_matches = []

    for i, platform_a in enumerate(platforms):
        for platform_b in platforms[i+1:]:
            markets_a = all_markets[platform_a]
            markets_b = all_markets[platform_b]

            print(f"\nComparing {platform_a} ({len(markets_a)} markets) vs {platform_b} ({len(markets_b)} markets)...")

            matches_found = 0

            for market_a in markets_a:
                for market_b in markets_b:
                    is_match, confidence, reason = markets_match(market_a, market_b)

                    if is_match:
                        matches_found += 1
                        potential_matches.append({
                            "platform_a": platform_a,
                            "platform_b": platform_b,
                            "title_a": market_a["title"][:60],
                            "title_b": market_b["title"][:60],
                            "confidence": confidence,
                            "reason": reason,
                        })

                        # Check for arbitrage
                        arb = calculate_arbitrage(market_a, market_b)
                        if arb:
                            opportunities.append({
                                "platform_a": platform_a,
                                "platform_b": platform_b,
                                "market_a": market_a,
                                "market_b": market_b,
                                "confidence": confidence,
                                "match_reason": reason,
                                **arb,
                            })

            print(f"  Potential matches: {matches_found}")

    # Log some potential matches (even without arbitrage)
    print("\n" + "-" * 60)
    print("POTENTIAL MARKET MATCHES (top 10)")
    print("-" * 60)

    for match in sorted(potential_matches, key=lambda x: -x["confidence"])[:10]:
        print(f"\n[{match['platform_a']} vs {match['platform_b']}] Confidence: {match['confidence']:.0%}")
        print(f"  A: {match['title_a']}")
        print(f"  B: {match['title_b']}")
        print(f"  Reason: {match['reason']}")

    return sorted(opportunities, key=lambda x: -x["net_pct"])


def main():
    """Run live scan."""
    print("=" * 70)
    print("LIVE ARBITRAGE SCAN v2")
    print(f"Time: {datetime.utcnow().isoformat()}")
    print("=" * 70)
    print()

    # Fetch markets
    all_markets = {}

    pi = fetch_predictit()
    if pi:
        all_markets["predictit"] = pi

    poly = fetch_polymarket()
    if poly:
        all_markets["polymarket"] = poly

    kalshi = fetch_kalshi()
    if kalshi:
        all_markets["kalshi"] = kalshi

    print()
    print(f"Total markets: {sum(len(m) for m in all_markets.values())}")

    # Find opportunities
    opportunities = find_opportunities(all_markets)

    print()
    print("=" * 70)
    print("ARBITRAGE OPPORTUNITIES")
    print("=" * 70)

    if opportunities:
        for i, opp in enumerate(opportunities[:15], 1):
            print(f"\n{i}. NET PROFIT: {opp['net_pct']:.2f}%")
            print(f"   {opp['platform_a'].upper()}: {opp['market_a']['title'][:70]}")
            print(f"   {opp['platform_b'].upper()}: {opp['market_b']['title'][:70]}")
            print(f"   Match confidence: {opp['confidence']:.0%} ({opp['match_reason']})")
            print(f"   Gross: {opp['gross_pct']:.2f}% - Fees: {opp['fee_pct']:.1f}% = Net: {opp['net_pct']:.2f}%")
            print(f"   Action: {opp['side_a']} @ ${opp['price_a']:.2f} on {opp['platform_a']}, "
                  f"{opp['side_b']} @ ${opp['price_b']:.2f} on {opp['platform_b']}")
            if opp['market_a'].get('url'):
                print(f"   URL: {opp['market_a']['url']}")
    else:
        print("\nNo arbitrage opportunities found.")

    # Summary
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Platforms: {list(all_markets.keys())}")
    print(f"Markets: {sum(len(m) for m in all_markets.values())}")
    print(f"Opportunities found: {len(opportunities)}")
    print(f"  Above 3% net: {len([o for o in opportunities if o['net_pct'] >= 3])}")
    print(f"  Above 5% net: {len([o for o in opportunities if o['net_pct'] >= 5])}")


if __name__ == "__main__":
    main()
