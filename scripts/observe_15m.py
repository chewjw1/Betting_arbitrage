#!/usr/bin/env python3
"""Observe a single 15-minute window to validate edge hypothesis.

This script:
1. Tracks BTC price every second
2. Tracks Kalshi market price
3. Calculates fair value vs market
4. Identifies divergences (potential edge)
5. Shows where overreactions occur

Run this for one 15-minute window to see if the edge theory holds.

Usage:
    python scripts/observe_15m.py
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass
from collections import deque
import math

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from scipy import stats


@dataclass
class Observation:
    """Single point in time observation."""
    timestamp: datetime
    btc_price: float
    target_price: float
    minutes_remaining: float
    market_yes: float  # Kalshi market price for YES
    fair_value_yes: float  # Our calculated fair value
    divergence: float  # fair_value - market (positive = edge to buy YES)
    vol_15m: float
    returns_1m: float
    returns_5m: float


class FifteenMinuteObserver:
    """Observe one 15-minute crypto market window."""

    def __init__(self):
        self.observations: list[Observation] = []
        self.btc_prices: deque = deque(maxlen=1000)
        self.target_price: float = 0
        self.window_start: datetime = None

    async def observe_window(self, duration_minutes: float = 15):
        """Observe for the specified duration."""
        print("=" * 70)
        print("15-MINUTE OBSERVATION WINDOW")
        print("=" * 70)
        print("\nCollecting data to validate edge hypothesis...")
        print("We'll track: BTC price, Kalshi market, fair value, divergences\n")

        start_time = datetime.now(timezone.utc)
        end_time_seconds = duration_minutes * 60

        async with httpx.AsyncClient(timeout=10) as client:
            elapsed = 0

            while elapsed < end_time_seconds:
                try:
                    obs = await self._get_observation(client, duration_minutes - elapsed/60)
                    if obs:
                        self.observations.append(obs)
                        self._print_observation(obs)
                except Exception as e:
                    print(f"Error: {e}")

                await asyncio.sleep(2)  # Every 2 seconds
                elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

        self._print_summary()

    async def _get_observation(self, client: httpx.AsyncClient, mins_left: float) -> Observation:
        """Get current observation."""
        # Get BTC price
        btc_resp = await client.get(
            "https://api.binance.us/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
        )
        if btc_resp.status_code != 200:
            return None
        btc_price = float(btc_resp.json()["price"])

        # Store for volatility
        self.btc_prices.append({
            "price": btc_price,
            "time": datetime.now(timezone.utc),
        })

        # Get Kalshi market
        kalshi_resp = await client.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            params={"series_ticker": "KXBTC15M", "status": "active", "limit": 1},
        )
        if kalshi_resp.status_code != 200:
            return None

        markets = kalshi_resp.json().get("markets", [])
        if not markets:
            return None

        market = markets[0]
        target = float(market.get("floor_strike", 0))
        yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0)
        market_yes = (yes_bid + yes_ask) / 2

        # Set target on first observation
        if self.target_price == 0:
            self.target_price = target
            print(f"Target price set: ${target:,.2f}")
            print("-" * 70)

        # Calculate volatility and returns
        vol_15m, returns_1m, returns_5m = self._calc_features()

        # Calculate fair value
        fair_value = self._calc_fair_value(btc_price, target, mins_left, vol_15m)

        return Observation(
            timestamp=datetime.now(timezone.utc),
            btc_price=btc_price,
            target_price=target,
            minutes_remaining=mins_left,
            market_yes=market_yes,
            fair_value_yes=fair_value,
            divergence=fair_value - market_yes,
            vol_15m=vol_15m,
            returns_1m=returns_1m,
            returns_5m=returns_5m,
        )

    def _calc_features(self) -> tuple[float, float, float]:
        """Calculate volatility and returns."""
        if len(self.btc_prices) < 10:
            return 0.003, 0, 0

        prices = list(self.btc_prices)
        now = prices[-1]["time"].timestamp()
        current = prices[-1]["price"]

        # Returns
        returns_1m = 0
        returns_5m = 0
        for p in reversed(prices):
            age = now - p["time"].timestamp()
            if age >= 60 and returns_1m == 0:
                returns_1m = (current - p["price"]) / p["price"]
            if age >= 300 and returns_5m == 0:
                returns_5m = (current - p["price"]) / p["price"]
                break

        # Volatility (last 60 seconds)
        recent = [p["price"] for p in prices if now - p["time"].timestamp() < 60]
        if len(recent) > 5:
            rets = [(recent[i] - recent[i-1]) / recent[i-1] for i in range(1, len(recent))]
            vol = math.sqrt(sum(r**2 for r in rets) / len(rets)) if rets else 0.003
        else:
            vol = 0.003

        return vol, returns_1m, returns_5m

    def _calc_fair_value(self, current: float, target: float, mins_left: float, vol: float) -> float:
        """Calculate fair value for YES."""
        if mins_left <= 0:
            return 1.0 if current >= target else 0.0

        distance_pct = (current - target) / target
        time_ratio = mins_left / 15.0
        expected_vol = vol * math.sqrt(time_ratio)

        if expected_vol < 0.0001:
            expected_vol = 0.001

        z_score = distance_pct / expected_vol
        fair_value = stats.norm.cdf(z_score)

        return max(0.01, min(0.99, fair_value))

    def _print_observation(self, obs: Observation):
        """Print single observation."""
        # Color-code divergence
        if abs(obs.divergence) > 0.05:
            div_marker = "***"
        elif abs(obs.divergence) > 0.02:
            div_marker = " * "
        else:
            div_marker = "   "

        direction = "↑" if obs.btc_price >= obs.target_price else "↓"

        print(
            f"{obs.timestamp.strftime('%H:%M:%S')} | "
            f"BTC: ${obs.btc_price:,.0f} {direction} | "
            f"Target: ${obs.target_price:,.0f} | "
            f"Time: {obs.minutes_remaining:5.1f}m | "
            f"Market: {obs.market_yes*100:5.1f}c | "
            f"Fair: {obs.fair_value_yes*100:5.1f}c | "
            f"Div: {obs.divergence*100:+5.1f}c {div_marker}"
        )

    def _print_summary(self):
        """Print analysis summary."""
        print("\n" + "=" * 70)
        print("OBSERVATION SUMMARY")
        print("=" * 70)

        if not self.observations:
            print("No observations collected.")
            return

        # Divergence analysis
        divergences = [o.divergence for o in self.observations]
        abs_div = [abs(d) for d in divergences]

        print(f"\nDivergence Analysis (Fair Value - Market):")
        print(f"  Mean divergence: {sum(divergences)/len(divergences)*100:+.2f}c")
        print(f"  Max positive: {max(divergences)*100:+.2f}c (edge to buy YES)")
        print(f"  Max negative: {min(divergences)*100:+.2f}c (edge to buy NO)")
        print(f"  Mean absolute: {sum(abs_div)/len(abs_div)*100:.2f}c")

        # Count edge opportunities
        edge_5pct = sum(1 for d in abs_div if d > 0.05)
        edge_10pct = sum(1 for d in abs_div if d > 0.10)

        print(f"\nEdge Opportunities:")
        print(f"  >5% edge: {edge_5pct} times ({edge_5pct/len(abs_div)*100:.1f}%)")
        print(f"  >10% edge: {edge_10pct} times ({edge_10pct/len(abs_div)*100:.1f}%)")

        # Time analysis
        early_obs = [o for o in self.observations if o.minutes_remaining > 10]
        mid_obs = [o for o in self.observations if 5 < o.minutes_remaining <= 10]
        late_obs = [o for o in self.observations if o.minutes_remaining <= 5]

        print(f"\nDivergence by Time Period:")
        if early_obs:
            early_div = sum(abs(o.divergence) for o in early_obs) / len(early_obs)
            print(f"  Early (>10m left): {early_div*100:.2f}c avg")
        if mid_obs:
            mid_div = sum(abs(o.divergence) for o in mid_obs) / len(mid_obs)
            print(f"  Mid (5-10m left): {mid_div*100:.2f}c avg")
        if late_obs:
            late_div = sum(abs(o.divergence) for o in late_obs) / len(late_obs)
            print(f"  Late (<5m left): {late_div*100:.2f}c avg")

        # BTC movement analysis
        btc_start = self.observations[0].btc_price
        btc_end = self.observations[-1].btc_price
        btc_move = (btc_end - btc_start) / btc_start * 100

        print(f"\nBTC Movement:")
        print(f"  Start: ${btc_start:,.2f}")
        print(f"  End: ${btc_end:,.2f}")
        print(f"  Change: {btc_move:+.2f}%")

        # Overreaction detection
        print(f"\nOverreaction Detection:")
        overreactions = []
        for i in range(1, len(self.observations)):
            prev = self.observations[i-1]
            curr = self.observations[i]

            btc_change = (curr.btc_price - prev.btc_price) / prev.btc_price
            market_change = curr.market_yes - prev.market_yes
            fair_change = curr.fair_value_yes - prev.fair_value_yes

            # Overreaction = market moved more than fair value suggests
            overreaction = abs(market_change) - abs(fair_change)
            if overreaction > 0.02:
                overreactions.append({
                    "time": curr.timestamp,
                    "btc_change": btc_change,
                    "market_change": market_change,
                    "fair_change": fair_change,
                    "overreaction": overreaction,
                })

        if overreactions:
            print(f"  Found {len(overreactions)} potential overreactions:")
            for o in overreactions[:5]:
                print(
                    f"    {o['time'].strftime('%H:%M:%S')}: "
                    f"BTC {o['btc_change']*100:+.2f}%, "
                    f"Market {o['market_change']*100:+.1f}c, "
                    f"Fair {o['fair_change']*100:+.1f}c"
                )
        else:
            print("  No significant overreactions detected")

        print("\n" + "=" * 70)
        print("CONCLUSION")
        print("=" * 70)

        avg_edge = sum(abs_div) / len(abs_div)
        if avg_edge > 0.05:
            print("\n✓ EDGE EXISTS: Average divergence > 5%")
            print("  The model finds pricing inefficiencies we could exploit.")
        elif avg_edge > 0.02:
            print("\n~ MARGINAL EDGE: Average divergence 2-5%")
            print("  Some opportunities exist but spread may eat profits.")
        else:
            print("\n✗ NO CLEAR EDGE: Average divergence < 2%")
            print("  Market is efficiently priced.")


async def main():
    print("\nStarting 15-minute observation window...")
    print("This will collect data to validate whether edge exists.\n")

    observer = FifteenMinuteObserver()

    # Run for 5 minutes as a quick test (or full 15)
    duration = float(input("Observation duration in minutes [5]: ") or 5)

    await observer.observe_window(duration_minutes=duration)


if __name__ == "__main__":
    asyncio.run(main())
