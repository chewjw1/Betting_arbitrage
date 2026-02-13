#!/usr/bin/env python3
"""Live scan with LLM validation using GPT-4o-mini or Claude Haiku.

This script fetches markets from public APIs and validates fuzzy matches
using an LLM to filter false positives.

Usage:
    OPENAI_API_KEY=sk-... python scripts/llm_validate_scan.py

    Or with Anthropic:
    ANTHROPIC_API_KEY=sk-ant-... LLM_PROVIDER=anthropic python scripts/llm_validate_scan.py
"""

import asyncio
import os
import sys
from datetime import datetime
import json
import hashlib

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx


# LLM validation prompt
VALIDATION_PROMPT = """Are these two prediction market questions about the SAME specific real-world event/outcome?

Market A ({platform_a}): "{title_a}"
Market B ({platform_b}): "{title_b}"

Answer SAME or DIFFERENT (one word only).

DIFFERENT if: different people, countries, time periods, positions (president vs VP), actions (win vs announce/visit/buy), scope (one state vs four), inverse polarity (uphold vs strike down), or fundamentally different questions."""


class LLMValidator:
    """Simple LLM validator for the standalone script."""

    def __init__(self):
        self.openai_key = os.environ.get("OPENAI_API_KEY", "")
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.provider = os.environ.get("LLM_PROVIDER", "openai")
        self.cache: dict[str, bool] = {}
        self.calls = 0
        self.rejections = 0

        # Select provider based on available keys (OpenAI preferred - cheaper)
        if self.provider == "openai" and self.openai_key:
            self.model = "gpt-4o-mini"
        elif self.anthropic_key:
            self.provider = "anthropic"
            self.model = "claude-3-haiku-20240307"
        elif self.openai_key:
            self.provider = "openai"
            self.model = "gpt-4o-mini"
        else:
            self.provider = None
            self.model = None

    def _cache_key(self, title_a: str, title_b: str) -> str:
        pair = tuple(sorted([title_a.strip().lower(), title_b.strip().lower()]))
        return hashlib.sha256(f"{pair[0]}||{pair[1]}".encode()).hexdigest()

    async def validate(self, title_a: str, platform_a: str, title_b: str, platform_b: str) -> bool:
        if not self.provider:
            return True  # No LLM available, keep all matches

        key = self._cache_key(title_a, title_b)
        if key in self.cache:
            return self.cache[key]

        prompt = VALIDATION_PROMPT.format(
            platform_a=platform_a,
            title_a=title_a,
            platform_b=platform_b,
            title_b=title_b,
        )

        self.calls += 1

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if self.provider == "anthropic":
                    response = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": self.anthropic_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "max_tokens": 10,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                    answer = data["content"][0]["text"].strip().upper()
                else:
                    response = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.openai_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0,
                            "max_tokens": 10,
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                    answer = data["choices"][0]["message"]["content"].strip().upper()

                result = answer.startswith("SAME")
                self.cache[key] = result

                if not result:
                    self.rejections += 1

                return result

        except Exception as e:
            print(f"    LLM error: {e}")
            return True  # Keep match on error


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
    a_tokens = set(title_a.lower().split())
    b_tokens = set(title_b.lower().split())

    stop_words = {"will", "the", "a", "an", "in", "on", "at", "to", "for", "of", "be", "by", "?", "-"}
    a_tokens -= stop_words
    b_tokens -= stop_words

    if not a_tokens or not b_tokens:
        return 0.0

    intersection = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)

    return intersection / union if union > 0 else 0.0


async def find_and_validate_matches(
    markets_a: list,
    markets_b: list,
    validator: LLMValidator,
    threshold: float = 0.4,
    max_candidates: int = 100,
) -> list:
    """Find matching markets and validate with LLM."""
    # First pass: fuzzy matching
    candidates = []
    for ma in markets_a:
        for mb in markets_b:
            if ma["platform"] == mb["platform"]:
                continue
            score = fuzzy_match(ma["title"], mb["title"])
            if score >= threshold:
                candidates.append((ma, mb, score))

    # Sort by match score and limit
    candidates.sort(key=lambda x: x[2], reverse=True)
    candidates = candidates[:max_candidates]

    print(f"  Fuzzy candidates: {len(candidates)} (limited to {max_candidates})")

    if not validator.provider:
        return candidates

    # Second pass: LLM validation
    validated = []
    for i, (ma, mb, score) in enumerate(candidates):
        if i % 10 == 0:
            print(f"    Validating {i+1}/{len(candidates)}...")

        is_same = await validator.validate(
            title_a=ma["title"],
            platform_a=ma["platform"],
            title_b=mb["title"],
            platform_b=mb["platform"],
        )

        if is_same:
            validated.append((ma, mb, score))
        else:
            # Show first few rejections for visibility
            if validator.rejections <= 5:
                print(f"    ✗ Rejected: '{ma['title'][:40]}...' vs '{mb['title'][:40]}...'")

    return validated


async def main():
    print("=" * 80)
    print("PREDICTION MARKET - LLM VALIDATED SCAN")
    print(f"Time: {datetime.utcnow().isoformat()}Z")
    print("=" * 80)

    # Initialize validator
    validator = LLMValidator()
    if validator.provider:
        print(f"\nLLM Provider: {validator.provider} ({validator.model})")
    else:
        print("\nNo LLM API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY")
        print("Proceeding with fuzzy matching only (will have false positives)...")

    # Fetch markets
    print("\n--- Fetching Markets ---")

    polymarket_markets = await fetch_polymarket()
    print(f"Polymarket: {len(polymarket_markets)} markets")

    predictit_markets = await fetch_predictit()
    print(f"PredictIt:  {len(predictit_markets)} markets")

    kalshi_markets = await fetch_kalshi_public()
    print(f"Kalshi:     {len(kalshi_markets)} markets")

    total = len(polymarket_markets) + len(predictit_markets) + len(kalshi_markets)
    print(f"\nTotal: {total} markets")

    # Find and validate matches
    print("\n--- Finding & Validating Cross-Platform Matches ---")

    all_matches = []

    # Polymarket vs PredictIt
    print("\nPolymarket ↔ PredictIt:")
    pm_pi = await find_and_validate_matches(polymarket_markets, predictit_markets, validator)
    all_matches.extend(pm_pi)
    print(f"  Validated: {len(pm_pi)}")

    # Polymarket vs Kalshi
    if kalshi_markets:
        print("\nPolymarket ↔ Kalshi:")
        pm_k = await find_and_validate_matches(polymarket_markets, kalshi_markets, validator)
        all_matches.extend(pm_k)
        print(f"  Validated: {len(pm_k)}")

    # PredictIt vs Kalshi
    if kalshi_markets:
        print("\nPredictIt ↔ Kalshi:")
        pi_k = await find_and_validate_matches(predictit_markets, kalshi_markets, validator)
        all_matches.extend(pi_k)
        print(f"  Validated: {len(pi_k)}")

    # Calculate arbitrage for validated matches
    print("\n--- Arbitrage Analysis ---")

    opportunities = []
    for ma, mb, score in all_matches:
        price_a = ma["yes_price"]
        price_b = mb["yes_price"]

        # Check both directions
        cost_yes_no = price_a + (1 - price_b)
        cost_no_yes = (1 - price_a) + price_b

        best_cost = min(cost_yes_no, cost_no_yes)
        gross_profit = (1 - best_cost) * 100

        # Estimate fees (~3% total for most platform pairs)
        net_profit = gross_profit - 3.0

        opportunities.append({
            "market_a": ma,
            "market_b": mb,
            "match_score": score,
            "gross_profit_pct": gross_profit,
            "net_profit_pct": net_profit,
            "strategy": "YES_A + NO_B" if cost_yes_no < cost_no_yes else "NO_A + YES_B",
        })

    opportunities.sort(key=lambda x: x["net_profit_pct"], reverse=True)

    # Categorize
    actionable = [o for o in opportunities if o["net_profit_pct"] >= 3]
    marginal = [o for o in opportunities if 0 <= o["net_profit_pct"] < 3]
    negative = [o for o in opportunities if o["net_profit_pct"] < 0]

    print(f"\nTotal validated matches: {len(opportunities)}")
    print(f"  ≥3% net profit (actionable): {len(actionable)}")
    print(f"  0-3% net profit (marginal):  {len(marginal)}")
    print(f"  <0% net profit (no arb):     {len(negative)}")

    # Show top opportunities
    if opportunities:
        print("\n" + "=" * 80)
        print("TOP 15 VALIDATED OPPORTUNITIES")
        print("=" * 80)

        for i, opp in enumerate(opportunities[:15]):
            ma = opp["market_a"]
            mb = opp["market_b"]
            status = "🟢" if opp["net_profit_pct"] >= 3 else "🟡" if opp["net_profit_pct"] >= 0 else "⚪"

            print(f"\n{status} #{i+1}: Net ~{opp['net_profit_pct']:.1f}% | Gross {opp['gross_profit_pct']:.1f}% | Match {opp['match_score']:.0%}")
            print(f"   {ma['platform']}: YES @ {ma['yes_price']:.2f} - {ma['title'][:60]}")
            print(f"   {mb['platform']}: YES @ {mb['yes_price']:.2f} - {mb['title'][:60]}")
            print(f"   Strategy: {opp['strategy']}")
            if ma.get("url"):
                print(f"   URL: {ma['url']}")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Markets scanned:      {total}")
    print(f"Fuzzy candidates:     (limited to 100 per pair)")

    if validator.provider:
        print(f"LLM calls made:       {validator.calls}")
        print(f"LLM rejections:       {validator.rejections}")
        if validator.calls > 0:
            print(f"Rejection rate:       {validator.rejections/validator.calls*100:.1f}%")

    print(f"Validated matches:    {len(opportunities)}")
    print(f"Actionable (≥3% net): {len(actionable)}")

    if len(actionable) == 0:
        print("\nNo actionable opportunities found. Markets appear efficiently priced.")
    elif len(actionable) > 10:
        print(f"\n{len(actionable)} opportunities found - review the list above.")
    else:
        print(f"\n{len(actionable)} potential opportunities to investigate.")

    return opportunities


if __name__ == "__main__":
    asyncio.run(main())
