#!/usr/bin/env python3
"""Test all API collectors to verify they return live data.

Usage:
    python scripts/test_collectors.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx


async def test_polymarket():
    """Test Polymarket Gamma API."""
    print("\n--- POLYMARKET ---")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": True, "closed": False, "limit": 5},
            )
            response.raise_for_status()
            data = response.json()
            count = len(data)
            print(f"  Status: {response.status_code}")
            print(f"  Markets returned: {count}")
            if count > 0 and isinstance(data[0], dict):
                m = data[0]
                print(f"  Sample: {m.get('question', 'N/A')[:80]}")
                prices = m.get("outcomePrices", "N/A")
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except:
                        pass
                print(f"  Prices: {prices}")
            return count > 0
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


async def test_predictit():
    """Test PredictIt API."""
    print("\n--- PREDICTIT ---")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://www.predictit.org/api/marketdata/all/"
            )
            response.raise_for_status()
            data = response.json()
            markets = data.get("markets", [])
            total_contracts = sum(len(m.get("contracts", [])) for m in markets)
            print(f"  Status: {response.status_code}")
            print(f"  Markets: {len(markets)}")
            print(f"  Total contracts: {total_contracts}")
            if markets:
                m = markets[0]
                print(f"  Sample: {m.get('name', 'N/A')[:80]}")
                contracts = m.get("contracts", [])
                if contracts:
                    c = contracts[0]
                    print(f"  Contract: {c.get('name', 'N/A')[:60]}")
                    print(f"  LastTradePrice: {c.get('lastTradePrice')}")
                    print(f"  BestBuyYes: {c.get('bestBuyYesCost')}")
                    print(f"  BestSellYes: {c.get('bestSellYesCost')}")
            return len(markets) > 0
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


async def test_kalshi():
    """Test Kalshi trade API v2."""
    print("\n--- KALSHI ---")
    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            # Test series endpoint
            response = await client.get(
                "https://api.elections.kalshi.com/trade-api/v2/series",
                params={"category": "Politics"},
            )
            print(f"  /series Status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                series = data.get("series", [])
                print(f"  Politics series: {len(series)}")

                if series:
                    ticker = series[0].get("ticker", "")
                    print(f"  First series ticker: {ticker}")

                    # Fetch markets for this series
                    response2 = await client.get(
                        "https://api.elections.kalshi.com/trade-api/v2/markets",
                        params={"series_ticker": ticker, "status": "open", "limit": 5},
                    )
                    print(f"  /markets Status: {response2.status_code}")

                    if response2.status_code == 200:
                        markets = response2.json().get("markets", [])
                        print(f"  Markets in '{ticker}': {len(markets)}")
                        if markets:
                            m = markets[0]
                            print(f"  Sample: {m.get('title', 'N/A')[:80]}")
                            print(f"  yes_bid: {m.get('yes_bid')}  yes_ask: {m.get('yes_ask')}")
                            print(f"  no_bid: {m.get('no_bid')}  no_ask: {m.get('no_ask')}")
                            print(f"  volume: {m.get('volume')}  open_interest: {m.get('open_interest')}")
                        return len(markets) > 0
            else:
                print(f"  Response: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


async def test_draftkings():
    """Test DraftKings predictions API."""
    print("\n--- DRAFTKINGS ---")
    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            # Try the category listing endpoint
            response = await client.get(
                "https://sportsbook-nash-usmi.draftkings.com/api/sportscontent/dkuspredictionmarkets/v1/leagues"
            )
            print(f"  /leagues Status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                leagues = data.get("leagues", data) if isinstance(data, dict) else data
                if isinstance(leagues, list):
                    print(f"  Leagues/categories: {len(leagues)}")
                    for league in leagues[:3]:
                        if isinstance(league, dict):
                            print(f"    - {league.get('name', league.get('leagueAbbreviation', 'N/A'))}")
                else:
                    print(f"  Response keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")
            else:
                # Try alternate endpoint
                response2 = await client.get(
                    "https://sportsbook-nash-usmi.draftkings.com/api/sportscontent/dkuspredictionmarkets/v1/categories"
                )
                print(f"  /categories Status: {response2.status_code}")
                if response2.status_code == 200:
                    data = response2.json()
                    print(f"  Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

            # Try fetching actual prediction markets
            response3 = await client.get(
                "https://sportsbook-nash-usmi.draftkings.com/api/sportscontent/dkuspredictionmarkets/v1/leagues/econ/events"
            )
            print(f"  /econ/events Status: {response3.status_code}")

            if response3.status_code == 200:
                data = response3.json()
                events = data.get("events", []) if isinstance(data, dict) else []
                print(f"  Econ events: {len(events)}")
                if events:
                    e = events[0]
                    print(f"  Sample: {e.get('name', 'N/A')[:80]}")
                return len(events) > 0
            else:
                print(f"  Response: {response3.text[:300]}")

            return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


async def main():
    print("=" * 70)
    print("COLLECTOR VALIDATION TEST")
    print(f"Time: {datetime.utcnow().isoformat()}Z")
    print("=" * 70)

    results = {}
    results["polymarket"] = await test_polymarket()
    results["predictit"] = await test_predictit()
    results["kalshi"] = await test_kalshi()
    results["draftkings"] = await test_draftkings()

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    for platform, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  {platform:15s} {status}")

    passing = sum(1 for v in results.values() if v)
    print(f"\n  {passing}/{len(results)} collectors returning data")


if __name__ == "__main__":
    asyncio.run(main())
