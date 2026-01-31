#!/usr/bin/env python3
"""Final live scan - shows what markets exist and any real opportunities."""

import ssl
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
import urllib.request
import json
import re


def fetch_json(url: str, timeout: int = 30) -> dict:
    """Fetch JSON from URL."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")

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
        for item in data:
            # Handle different formats
            if isinstance(item, str):
                continue  # Skip string items
            if not isinstance(item, dict):
                continue

            tokens = item.get("tokens", [])
            question = item.get("question", "")
            if not tokens or not question:
                continue

            if len(tokens) >= 2:
                try:
                    yes_price = Decimal(str(tokens[0].get("price", 0)))
                    if yes_price > 0:
                        markets.append({
                            "platform": "polymarket",
                            "id": item.get("condition_id", ""),
                            "title": question,
                            "yes_price": yes_price,
                            "url": "https://polymarket.com/",
                        })
                except (TypeError, ValueError):
                    continue

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
                    "url": f"https://kalshi.com/markets/{market.get('ticker', '')}",
                })
        print(f"  {len(markets)} markets")
        return markets
    except Exception as e:
        print(f"  Error: {e}")
        return []


def normalize(text: str) -> str:
    """Normalize text for comparison."""
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = ' '.join(text.split())
    return text


def get_keywords(text: str) -> set:
    """Get important keywords from text."""
    text = text.lower()
    # Remove common stop words
    stop = {'will', 'the', 'be', 'in', 'by', 'on', 'a', 'an', 'to', 'of', 'for', 'who', 'what', 'win', 'yes', 'no'}
    words = re.findall(r'\b\w+\b', text)
    return {w for w in words if w not in stop and len(w) > 2}


def find_matches(all_markets: dict) -> list:
    """Find potential matches between platforms."""
    matches = []
    platforms = list(all_markets.keys())

    for i, p1 in enumerate(platforms):
        for p2 in platforms[i+1:]:
            for m1 in all_markets[p1]:
                kw1 = get_keywords(m1["title"])
                for m2 in all_markets[p2]:
                    kw2 = get_keywords(m2["title"])

                    # Need substantial keyword overlap
                    common = kw1 & kw2
                    if len(common) < 3:
                        continue

                    # Check title similarity
                    sim = SequenceMatcher(None, normalize(m1["title"]), normalize(m2["title"])).ratio()
                    if sim < 0.6:
                        continue

                    # Calculate potential arbitrage
                    yes1 = m1["yes_price"]
                    yes2 = m2["yes_price"]
                    cost = min(yes1 + (1 - yes2), (1 - yes1) + yes2)
                    spread = float((1 - cost) * 100) if cost < 1 else 0

                    matches.append({
                        "p1": p1, "p2": p2,
                        "m1": m1, "m2": m2,
                        "common_keywords": common,
                        "similarity": sim,
                        "spread": spread,
                    })

    return sorted(matches, key=lambda x: (-x["spread"], -x["similarity"]))


def main():
    print("=" * 70)
    print("LIVE ARBITRAGE SCAN")
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

    total = sum(len(m) for m in all_markets.values())
    print(f"\nTotal: {total} markets across {len(all_markets)} platforms")

    # Show sample markets from each platform
    print()
    print("=" * 70)
    print("SAMPLE MARKETS FROM EACH PLATFORM")
    print("=" * 70)

    for platform, markets in all_markets.items():
        print(f"\n{platform.upper()} (showing top 5 by liquidity/activity):")
        for m in markets[:5]:
            print(f"  - {m['title'][:70]}")
            print(f"    YES: {float(m['yes_price']):.2f}")

    # Find cross-platform matches
    print()
    print("=" * 70)
    print("POTENTIAL CROSS-PLATFORM MATCHES")
    print("=" * 70)

    matches = find_matches(all_markets)

    if matches:
        print(f"\nFound {len(matches)} potential matches:")
        for i, m in enumerate(matches[:10], 1):
            print(f"\n{i}. {m['p1'].upper()} vs {m['p2'].upper()}")
            print(f"   A: {m['m1']['title'][:65]}")
            print(f"   B: {m['m2']['title'][:65]}")
            print(f"   Keywords: {', '.join(list(m['common_keywords'])[:5])}")
            print(f"   Similarity: {m['similarity']:.0%}")
            print(f"   Prices: {float(m['m1']['yes_price']):.2f} vs {float(m['m2']['yes_price']):.2f}")

            if m['spread'] > 0:
                fee_est = 12 if 'predictit' in [m['p1'], m['p2']] else 5
                net = m['spread'] - fee_est
                print(f"   Gross spread: {m['spread']:.1f}%, Est fees: {fee_est}%, Net: {net:.1f}%")
                if net > 3:
                    print(f"   ⚠️  POTENTIAL OPPORTUNITY - verify markets are identical!")
            else:
                print(f"   No arbitrage (prices aligned)")
    else:
        print("\nNo cross-platform matches found.")
        print("This could mean:")
        print("  1. Markets don't overlap between platforms")
        print("  2. Different wording for same events")
        print("  3. Different timeframes/resolutions")

    # Summary
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Platforms scanned: {list(all_markets.keys())}")
    print(f"Total markets: {total}")
    print(f"Potential matches: {len(matches)}")

    actionable = [m for m in matches if m['spread'] > 15]  # 15% gross = ~3% net after PredictIt fees
    print(f"Potentially actionable (>3% net): {len(actionable)}")

    if not actionable:
        print("\nConclusion: Markets are efficiently priced across platforms.")
        print("Real arbitrage opportunities are rare and short-lived.")


if __name__ == "__main__":
    main()
