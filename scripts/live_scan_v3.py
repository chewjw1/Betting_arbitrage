#!/usr/bin/env python3
"""Live scan v3 - with strict matching to avoid false positives."""

import ssl
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
import urllib.request
import json
import re


def fetch_json(url: str, headers: dict = None, timeout: int = 30) -> dict:
    """Fetch JSON from URL."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_predictit() -> list[dict]:
    """Fetch PredictIt markets."""
    print("Fetching PredictIt...")
    data = fetch_json("https://www.predictit.org/api/marketdata/all/")

    markets = []
    for market in data.get("markets", []):
        for contract in market.get("contracts", []):
            if contract.get("status") == "Open":
                yes_price = contract.get("lastTradePrice") or contract.get("bestBuyYesCost")
                if yes_price and yes_price > 0:
                    markets.append({
                        "platform": "predictit",
                        "id": str(contract["id"]),
                        "title": f"{market['name']}: {contract['name']}",
                        "market_name": market['name'],
                        "contract_name": contract['name'],
                        "yes_price": Decimal(str(yes_price)),
                        "no_price": Decimal(str(1 - yes_price)),
                        "url": market.get("url"),
                    })
    print(f"  {len(markets)} contracts")
    return markets


def fetch_polymarket() -> list[dict]:
    """Fetch Polymarket markets."""
    print("Fetching Polymarket...")
    try:
        data = fetch_json("https://clob.polymarket.com/markets", timeout=15)

        markets = []
        for market in data:
            tokens = market.get("tokens", [])
            question = market.get("question", "")
            if tokens and len(tokens) >= 2 and question:
                yes_price = Decimal(str(tokens[0].get("price", 0)))
                if yes_price > 0:
                    markets.append({
                        "platform": "polymarket",
                        "id": market.get("condition_id", ""),
                        "title": question,
                        "yes_price": yes_price,
                        "no_price": Decimal(str(tokens[1].get("price", 0))),
                        "url": f"https://polymarket.com/",
                    })
        print(f"  {len(markets)} markets")
        return markets
    except Exception as e:
        print(f"  Error: {e}")
        return []


def fetch_kalshi() -> list[dict]:
    """Fetch Kalshi markets."""
    print("Fetching Kalshi...")
    try:
        data = fetch_json("https://api.elections.kalshi.com/trade-api/v2/markets?limit=500&status=open")

        markets = []
        for market in data.get("markets", []):
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
                })
        print(f"  {len(markets)} markets")
        return markets
    except Exception as e:
        print(f"  Error: {e}")
        return []


def extract_year(text: str) -> str | None:
    """Extract year from text."""
    match = re.search(r'\b(202[3-9]|203[0-9])\b', text)
    return match.group(1) if match else None


def extract_subject(text: str) -> str | None:
    """Extract main subject/person from text."""
    # Look for common patterns
    patterns = [
        r'will\s+(\w+(?:\s+\w+)?)\s+(?:win|be|become)',  # "Will Trump win"
        r'(\w+(?:\s+\w+)?)\s+(?:wins?|wins\s+the)',  # "Trump wins"
        r'(?::|-)?\s*(\w+(?:\s+\w+)?)\s*$',  # ": Trump" at end
    ]

    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            subject = match.group(1).strip()
            # Filter out common words
            if subject not in ['the', 'a', 'an', 'will', 'be', 'who', 'what']:
                return subject
    return None


def extract_event_type(text: str) -> str | None:
    """Extract type of event."""
    text_lower = text.lower()
    if 'presiden' in text_lower:
        return 'presidential'
    if 'senate' in text_lower:
        return 'senate'
    if 'house' in text_lower or 'congress' in text_lower:
        return 'house'
    if 'governor' in text_lower:
        return 'governor'
    if 'fed' in text_lower or 'interest rate' in text_lower:
        return 'fed'
    if 'bitcoin' in text_lower or 'btc' in text_lower or 'crypto' in text_lower:
        return 'crypto'
    if 'super bowl' in text_lower:
        return 'superbowl'
    return None


def extract_country(text: str) -> str:
    """Extract country from text."""
    text_lower = text.lower()

    countries = {
        'us': ['u.s.', 'united states', 'america', 'american'],
        'uk': ['uk', 'united kingdom', 'britain', 'british'],
        'turkey': ['turkey', 'turkish', 'erdogan'],
        'portugal': ['portugal', 'portuguese'],
        'czech': ['czech', 'czechia'],
        'france': ['france', 'french'],
        'germany': ['germany', 'german'],
    }

    for country, keywords in countries.items():
        for kw in keywords:
            if kw in text_lower:
                return country

    # Default to US for common US-specific terms
    us_terms = ['republican', 'democrat', 'gop', 'dnc', 'rnc', 'trump', 'biden', 'harris']
    for term in us_terms:
        if term in text_lower:
            return 'us'

    return 'unknown'


def markets_match_strict(market_a: dict, market_b: dict) -> tuple[bool, float, str]:
    """Strictly check if two markets are the SAME event.

    Requirements for a match:
    1. Same year (or both no year specified)
    2. Same country
    3. Same event type
    4. Same subject/candidate (or very high title similarity)
    """
    title_a = market_a["title"]
    title_b = market_b["title"]

    # Extract components
    year_a = extract_year(title_a)
    year_b = extract_year(title_b)
    country_a = extract_country(title_a)
    country_b = extract_country(title_b)
    event_a = extract_event_type(title_a)
    event_b = extract_event_type(title_b)
    subject_a = extract_subject(title_a)
    subject_b = extract_subject(title_b)

    reasons = []

    # DISQUALIFIERS - these must match

    # Year check - if both have years, they must match
    if year_a and year_b and year_a != year_b:
        return False, 0.0, f"Year mismatch: {year_a} vs {year_b}"

    # Country check
    if country_a != 'unknown' and country_b != 'unknown' and country_a != country_b:
        return False, 0.0, f"Country mismatch: {country_a} vs {country_b}"

    # Event type check
    if event_a and event_b and event_a != event_b:
        return False, 0.0, f"Event type mismatch: {event_a} vs {event_b}"

    # SCORING
    score = 0.0

    # Same year
    if year_a and year_b and year_a == year_b:
        score += 0.25
        reasons.append(f"same year: {year_a}")

    # Same country
    if country_a == country_b and country_a != 'unknown':
        score += 0.2
        reasons.append(f"same country: {country_a}")

    # Same event type
    if event_a and event_b and event_a == event_b:
        score += 0.2
        reasons.append(f"same event: {event_a}")

    # Subject similarity
    if subject_a and subject_b:
        subject_sim = SequenceMatcher(None, subject_a, subject_b).ratio()
        if subject_sim > 0.8:
            score += 0.35
            reasons.append(f"same subject: {subject_a}")
        elif subject_sim > 0.5:
            score += 0.15
            reasons.append(f"similar subject: {subject_a}/{subject_b}")

    # Overall title similarity as backup
    title_sim = SequenceMatcher(
        None,
        re.sub(r'[^\w\s]', '', title_a.lower()),
        re.sub(r'[^\w\s]', '', title_b.lower())
    ).ratio()

    if title_sim > 0.85:
        score += 0.2
        reasons.append(f"high title similarity: {title_sim:.0%}")

    # Require high confidence for a match
    is_match = score >= 0.7

    return is_match, score, "; ".join(reasons) if reasons else "No match"


def calculate_arbitrage(market_a: dict, market_b: dict) -> dict | None:
    """Calculate arbitrage if it exists."""
    yes_a = market_a["yes_price"]
    yes_b = market_b["yes_price"]

    cost_1 = yes_a + (Decimal("1") - yes_b)
    cost_2 = (Decimal("1") - yes_a) + yes_b

    best_cost = min(cost_1, cost_2)

    if best_cost >= Decimal("1"):
        return None

    gross_spread = Decimal("1") - best_cost
    gross_pct = float(gross_spread * 100)

    # Fee estimates
    platforms = {market_a["platform"], market_b["platform"]}
    if "predictit" in platforms:
        fee_pct = 12.0
    elif "kalshi" in platforms and "polymarket" in platforms:
        fee_pct = 3.0
    else:
        fee_pct = 5.0

    net_pct = gross_pct - fee_pct

    if net_pct <= 0:
        return None

    if cost_1 < cost_2:
        return {
            "gross_pct": gross_pct,
            "fee_pct": fee_pct,
            "net_pct": net_pct,
            "action": f"Buy YES @ {float(yes_a):.2f} on {market_a['platform']}, NO @ {float(1-yes_b):.2f} on {market_b['platform']}",
        }
    else:
        return {
            "gross_pct": gross_pct,
            "fee_pct": fee_pct,
            "net_pct": net_pct,
            "action": f"Buy NO @ {float(1-yes_a):.2f} on {market_a['platform']}, YES @ {float(yes_b):.2f} on {market_b['platform']}",
        }


def find_opportunities(all_markets: dict[str, list]) -> tuple[list, list]:
    """Find arbitrage opportunities with strict matching."""
    opportunities = []
    matches = []

    platforms = list(all_markets.keys())

    for i, platform_a in enumerate(platforms):
        for platform_b in platforms[i+1:]:
            markets_a = all_markets[platform_a]
            markets_b = all_markets[platform_b]

            print(f"\nComparing {platform_a} vs {platform_b}...")

            match_count = 0
            for market_a in markets_a:
                for market_b in markets_b:
                    is_match, confidence, reason = markets_match_strict(market_a, market_b)

                    if is_match:
                        match_count += 1
                        match_info = {
                            "platform_a": platform_a,
                            "platform_b": platform_b,
                            "title_a": market_a["title"][:80],
                            "title_b": market_b["title"][:80],
                            "yes_a": float(market_a["yes_price"]),
                            "yes_b": float(market_b["yes_price"]),
                            "confidence": confidence,
                            "reason": reason,
                        }
                        matches.append(match_info)

                        # Check for arbitrage
                        arb = calculate_arbitrage(market_a, market_b)
                        if arb:
                            opportunities.append({
                                **match_info,
                                **arb,
                                "url_a": market_a.get("url"),
                                "url_b": market_b.get("url"),
                            })

            print(f"  Verified matches: {match_count}")

    return opportunities, matches


def main():
    print("=" * 70)
    print("LIVE ARBITRAGE SCAN v3 (Strict Matching)")
    print(f"Time: {datetime.utcnow().isoformat()}")
    print("=" * 70)
    print()

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

    print(f"\nTotal: {sum(len(m) for m in all_markets.values())} markets")

    opportunities, matches = find_opportunities(all_markets)

    # Show verified matches
    print()
    print("=" * 70)
    print("VERIFIED MARKET MATCHES (same event on different platforms)")
    print("=" * 70)

    if matches:
        for i, m in enumerate(sorted(matches, key=lambda x: -x["confidence"])[:15], 1):
            print(f"\n{i}. [{m['platform_a']} vs {m['platform_b']}] Confidence: {m['confidence']:.0%}")
            print(f"   A: {m['title_a']}")
            print(f"   B: {m['title_b']}")
            print(f"   Prices: A={m['yes_a']:.2f}, B={m['yes_b']:.2f}")
            print(f"   Match: {m['reason']}")
    else:
        print("\nNo verified matches found between platforms.")

    # Show opportunities
    print()
    print("=" * 70)
    print("ARBITRAGE OPPORTUNITIES")
    print("=" * 70)

    if opportunities:
        for i, opp in enumerate(sorted(opportunities, key=lambda x: -x["net_pct"])[:10], 1):
            print(f"\n{i}. NET PROFIT: {opp['net_pct']:.2f}%")
            print(f"   {opp['platform_a'].upper()}: {opp['title_a']}")
            print(f"   {opp['platform_b'].upper()}: {opp['title_b']}")
            print(f"   Gross: {opp['gross_pct']:.2f}% - Fees: {opp['fee_pct']:.1f}% = Net: {opp['net_pct']:.2f}%")
            print(f"   Action: {opp['action']}")
            if opp.get('url_a'):
                print(f"   URL: {opp['url_a']}")
    else:
        print("\nNo arbitrage opportunities found.")
        print("Markets are efficiently priced where they overlap.")

    # Summary
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Platforms: {list(all_markets.keys())}")
    print(f"Total markets: {sum(len(m) for m in all_markets.values())}")
    print(f"Verified cross-platform matches: {len(matches)}")
    print(f"Arbitrage opportunities: {len(opportunities)}")
    if opportunities:
        above_3 = len([o for o in opportunities if o['net_pct'] >= 3])
        print(f"  Above 3% net: {above_3}")


if __name__ == "__main__":
    main()
