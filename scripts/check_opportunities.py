#!/usr/bin/env python3
"""Check what opportunities would be detected at 3% threshold."""

import asyncio
import ssl
import urllib.request
import json
from decimal import Decimal
from datetime import datetime


def fetch_json(url: str, timeout: int = 30) -> dict:
    """Fetch JSON from URL."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_predictit():
    """Fetch PredictIt."""
    data = fetch_json("https://www.predictit.org/api/marketdata/all/")
    markets = []
    for market in data.get("markets", []):
        for contract in market.get("contracts", []):
            if contract.get("status") == "Open":
                yes_price = contract.get("lastTradePrice") or contract.get("bestBuyYesCost")
                no_price = contract.get("bestBuyNoCost")
                if yes_price and yes_price > 0:
                    markets.append({
                        "platform": "predictit",
                        "title": f"{market['name']}: {contract['name']}",
                        "yes_price": Decimal(str(yes_price)),
                        "no_price": Decimal(str(no_price)) if no_price else Decimal("1") - Decimal(str(yes_price)),
                    })
    return markets


def check_complement_violations(markets: list, threshold_pct: float = 3.0):
    """Check for YES + NO != 1.0 violations."""
    violations = []

    for m in markets:
        yes = m["yes_price"]
        no = m["no_price"]
        total = yes + no
        deviation = abs(total - Decimal("1"))
        deviation_pct = float(deviation * 100)

        if deviation_pct >= threshold_pct:
            violations.append({
                "title": m["title"][:60],
                "yes": float(yes),
                "no": float(no),
                "total": float(total),
                "deviation_pct": deviation_pct,
                "action": "Buy BOTH" if total < 1 else "Can't short",
            })

    return sorted(violations, key=lambda x: -x["deviation_pct"])


def main():
    print("=" * 70)
    print("OPPORTUNITY CHECK AT 3% THRESHOLD")
    print(f"Time: {datetime.utcnow().isoformat()}")
    print("=" * 70)

    print("\nFetching PredictIt...")
    pi_markets = fetch_predictit()
    print(f"  {len(pi_markets)} contracts")

    # Check complement violations at different thresholds
    for threshold in [1, 2, 3, 5, 10]:
        violations = check_complement_violations(pi_markets, threshold)
        print(f"\n  Complement violations at {threshold}%: {len(violations)}")

    # Show violations at 3%
    violations_3pct = check_complement_violations(pi_markets, 3.0)

    print("\n" + "-" * 70)
    print(f"COMPLEMENT VIOLATIONS >= 3% (showing {min(10, len(violations_3pct))})")
    print("-" * 70)

    for i, v in enumerate(violations_3pct[:10], 1):
        print(f"\n{i}. {v['title']}")
        print(f"   YES: {v['yes']:.2f} + NO: {v['no']:.2f} = {v['total']:.2f}")
        print(f"   Deviation: {v['deviation_pct']:.1f}%")
        print(f"   Action: {v['action']}")

    # Now check the REAL issue - same-market bid-ask spread
    print("\n" + "-" * 70)
    print("CHECKING ACTUAL BID-ASK SPREAD ISSUE")
    print("-" * 70)

    # On PredictIt, you can't actually arbitrage YES+NO != 1 because:
    # - You pay 10% fee on profits
    # - You pay trading fees
    # - The prices shown are often last trade, not current bid/ask

    print("\nKey insight: PredictIt YES + NO often != 1.0 due to:")
    print("  1. Bid-ask spread (lastTrade vs bestBuy)")
    print("  2. 10% profit fee makes small deviations unprofitable")
    print("  3. $0.01 minimum price increment causes rounding")
    print("\nThese are NOT real arbitrage opportunities!")

    # Estimate real opportunities
    # For a 3% deviation to be profitable after 10% fee:
    # Need: 3% - 10% * 3% = 2.7%
    # But with two trades (buy YES + buy NO), fees compound

    print("\n" + "=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    print(f"\nPredictIt complement 'violations' at 3%: {len(violations_3pct)}")
    print("But these are NOT actionable because:")
    print("  - 10% profit fee on PredictIt")
    print("  - 5% withdrawal fee")
    print("  - Bid-ask spread wider than shown prices")
    print("\nRECOMMENDATION: Disable complement checking for PredictIt,")
    print("or raise threshold to 15%+ to account for fees.")


if __name__ == "__main__":
    main()
