#!/usr/bin/env python3
"""Live data collector for Kalshi 15-minute BTC UP/DOWN markets.

Collects:
1. Real-time BTC price (every second)
2. Kalshi market prices (YES/NO bids/asks)
3. Order flow metrics
4. Funding rates
5. Volatility measures
6. Window outcomes

Saves everything to CSV for pattern analysis.
"""

import asyncio
import json
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import deque
from typing import Optional
import math
import sys

import httpx
import websockets

# Data directory
DATA_DIR = Path(__file__).parent.parent.parent.parent / "data" / "live_observations"
DATA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class TickData:
    """Single tick of observation data."""
    timestamp: str
    window_id: str
    seconds_remaining: int

    # BTC price data
    btc_price: float
    btc_target: float  # Previous 15m close
    btc_distance_pct: float

    # Kalshi market data
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    spread_cents: float

    # Momentum metrics
    btc_change_10s: float
    btc_change_30s: float
    btc_change_60s: float
    btc_change_5m: float

    # Volatility metrics
    vol_1m: float
    vol_5m: float
    price_range_1m: float

    # Order flow (if available)
    trade_flow_30s: float  # Buy - sell volume
    large_trades_1m: int

    # Calculated fair value
    model_prob_yes: float
    edge_vs_market: float


@dataclass
class WindowResult:
    """Result of a completed window."""
    window_id: str
    start_time: str
    end_time: str
    target_price: float
    open_price: float
    close_price: float
    high_price: float
    low_price: float
    outcome: str  # "UP" or "DOWN"
    final_yes_price: float
    final_no_price: float

    # Patterns observed
    early_direction: str  # Direction in first 5 min
    mid_direction: str    # Direction in min 5-10
    late_direction: str   # Direction in last 5 min
    reversals: int        # Number of times it crossed target
    max_distance_pct: float
    volatility: float


class LiveDataCollector:
    """Collect live data from Kalshi and Binance."""

    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
    BINANCE_WS = "wss://stream.binance.us:9443/ws/btcusdt@trade"
    BINANCE_REST = "https://api.binance.us/api/v3"

    def __init__(self):
        self.btc_prices: deque = deque(maxlen=1000)  # Last ~15 min of prices
        self.current_btc: float = 0
        self.current_window: Optional[dict] = None
        self.tick_data: list[TickData] = []
        self.window_results: list[WindowResult] = []

        self._running = False
        self._ws_task = None

    async def start_collection(self, duration_minutes: int = 60):
        """Start collecting data for specified duration."""
        print("=" * 70)
        print("LIVE DATA COLLECTOR - KALSHI 15M BTC UP/DOWN")
        print("=" * 70)
        print(f"\nCollecting data for {duration_minutes} minutes...")
        print(f"Data will be saved to: {DATA_DIR}\n")

        self._running = True
        start_time = datetime.now(timezone.utc)
        end_time = start_time + timedelta(minutes=duration_minutes)

        # Start BTC price WebSocket in background
        self._ws_task = asyncio.create_task(self._connect_btc_ws())

        # Wait for initial price data
        print("Waiting for price feed...")
        await asyncio.sleep(5)

        if self.current_btc == 0:
            print("Fetching initial BTC price via REST...")
            await self._fetch_btc_rest()

        print(f"BTC price: ${self.current_btc:,.2f}\n")
        print("-" * 70)

        # Main collection loop
        async with httpx.AsyncClient(timeout=15) as client:
            while self._running and datetime.now(timezone.utc) < end_time:
                try:
                    await self._collection_cycle(client)
                except Exception as e:
                    print(f"Error in collection cycle: {e}")

                await asyncio.sleep(2)  # Collect every 2 seconds

        # Stop WebSocket
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()

        # Save all data
        self._save_data()
        self._print_summary()

    async def _connect_btc_ws(self):
        """Connect to Binance WebSocket for real-time BTC prices."""
        while self._running:
            try:
                async with websockets.connect(self.BINANCE_WS) as ws:
                    async for message in ws:
                        if not self._running:
                            break
                        data = json.loads(message)
                        price = float(data["p"])
                        timestamp = datetime.fromtimestamp(data["T"] / 1000, tz=timezone.utc)

                        self.current_btc = price
                        self.btc_prices.append({
                            "price": price,
                            "time": timestamp,
                        })
            except Exception as e:
                if self._running:
                    await asyncio.sleep(1)

    async def _fetch_btc_rest(self):
        """Fetch BTC price via REST API."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.BINANCE_REST}/ticker/price",
                params={"symbol": "BTCUSDT"}
            )
            if resp.status_code == 200:
                self.current_btc = float(resp.json()["price"])

    async def _collection_cycle(self, client: httpx.AsyncClient):
        """One cycle of data collection."""

        # Get current Kalshi market
        market = await self._get_kalshi_market(client)
        if not market:
            return

        # Check if new window
        window_id = market.get("ticker", "")
        if self.current_window is None or self.current_window.get("ticker") != window_id:
            # New window started
            if self.current_window:
                await self._finalize_window()
            self.current_window = market
            self._start_new_window(market)

        # Collect tick data
        tick = self._create_tick(market)
        if tick:
            self.tick_data.append(tick)
            self._print_tick(tick)

    async def _get_kalshi_market(self, client: httpx.AsyncClient) -> Optional[dict]:
        """Get current active Kalshi 15M BTC market."""
        try:
            # Try to find active KXBTC15M market
            resp = await client.get(
                f"{self.KALSHI_API}/markets",
                params={
                    "series_ticker": "KXBTC15M",
                    "status": "open",  # "open" returns active markets
                    "limit": 5,
                }
            )

            if resp.status_code == 200:
                markets = resp.json().get("markets", [])
                # Filter for actually active (has pricing)
                active = [m for m in markets if m.get("status") == "active"]
                if active:
                    return active[0]

            return None

        except Exception as e:
            print(f"Kalshi API error: {e}")
            return None

    def _start_new_window(self, market: dict):
        """Initialize tracking for new window."""
        ticker = market.get("ticker", "")
        target = float(market.get("floor_strike", 0) or market.get("strike_price", 0) or 0)

        print(f"\n{'='*70}")
        print(f"NEW WINDOW: {ticker}")
        print(f"Target: ${target:,.2f} (previous 15m close)")
        print(f"Current BTC: ${self.current_btc:,.2f}")
        print(f"Distance: {(self.current_btc - target) / target * 100:+.3f}%")
        print("=" * 70 + "\n")

    async def _finalize_window(self):
        """Finalize and record completed window."""
        if not self.current_window:
            return

        # Get window ticks
        window_id = self.current_window.get("ticker", "")
        window_ticks = [t for t in self.tick_data if t.window_id == window_id]

        if not window_ticks:
            return

        # Calculate result
        first_tick = window_ticks[0]
        last_tick = window_ticks[-1]

        prices = [t.btc_price for t in window_ticks]

        outcome = "UP" if last_tick.btc_price >= first_tick.btc_target else "DOWN"

        # Track direction changes
        early_ticks = [t for t in window_ticks if t.seconds_remaining > 600]
        mid_ticks = [t for t in window_ticks if 300 < t.seconds_remaining <= 600]
        late_ticks = [t for t in window_ticks if t.seconds_remaining <= 300]

        def get_direction(ticks):
            if len(ticks) < 2:
                return "FLAT"
            return "UP" if ticks[-1].btc_price > ticks[0].btc_price else "DOWN"

        # Count reversals (crosses target)
        reversals = 0
        last_side = None
        for tick in window_ticks:
            side = "above" if tick.btc_price >= tick.btc_target else "below"
            if last_side and side != last_side:
                reversals += 1
            last_side = side

        result = WindowResult(
            window_id=window_id,
            start_time=first_tick.timestamp,
            end_time=last_tick.timestamp,
            target_price=first_tick.btc_target,
            open_price=prices[0],
            close_price=prices[-1],
            high_price=max(prices),
            low_price=min(prices),
            outcome=outcome,
            final_yes_price=last_tick.yes_bid,
            final_no_price=last_tick.no_bid,
            early_direction=get_direction(early_ticks),
            mid_direction=get_direction(mid_ticks),
            late_direction=get_direction(late_ticks),
            reversals=reversals,
            max_distance_pct=max(abs(t.btc_distance_pct) for t in window_ticks),
            volatility=self._calc_volatility(prices),
        )

        self.window_results.append(result)

        print(f"\n{'='*70}")
        print(f"WINDOW COMPLETE: {window_id}")
        print(f"Outcome: {outcome}")
        print(f"Target: ${result.target_price:,.2f}")
        print(f"Close: ${result.close_price:,.2f} ({(result.close_price-result.target_price)/result.target_price*100:+.3f}%)")
        print(f"Reversals: {reversals}")
        print(f"Early→Mid→Late: {result.early_direction}→{result.mid_direction}→{result.late_direction}")
        print("=" * 70 + "\n")

    def _create_tick(self, market: dict) -> Optional[TickData]:
        """Create tick data from current state."""
        if self.current_btc == 0:
            return None

        now = datetime.now(timezone.utc)

        # Parse market data
        ticker = market.get("ticker", "")
        target = float(market.get("floor_strike", 0) or 0)

        # Handle close time
        close_time_str = market.get("close_time", "")
        if close_time_str:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            seconds_remaining = max(0, int((close_time - now).total_seconds()))
        else:
            seconds_remaining = 900  # Default 15 min

        # Market prices (API returns as strings like "0.3600")
        yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0)
        no_bid = float(market.get("no_bid_dollars", 0) or 0)
        no_ask = float(market.get("no_ask_dollars", 0) or 0)

        # Calculate momentum
        btc_change_10s = self._calc_return(10)
        btc_change_30s = self._calc_return(30)
        btc_change_60s = self._calc_return(60)
        btc_change_5m = self._calc_return(300)

        # Volatility
        vol_1m, vol_5m, range_1m = self._calc_volatility_metrics()

        # Order flow (simplified - count direction of recent moves)
        trade_flow = self._calc_trade_flow()

        # Model probability
        distance_pct = (self.current_btc - target) / target if target > 0 else 0
        model_prob = self._calc_model_prob(distance_pct, seconds_remaining / 60, vol_1m)

        # Edge vs market
        market_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else 0.5
        edge = model_prob - market_mid

        return TickData(
            timestamp=now.isoformat(),
            window_id=ticker,
            seconds_remaining=seconds_remaining,
            btc_price=self.current_btc,
            btc_target=target,
            btc_distance_pct=distance_pct * 100,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            spread_cents=(yes_ask - yes_bid) * 100 if yes_ask and yes_bid else 0,
            btc_change_10s=btc_change_10s * 100,
            btc_change_30s=btc_change_30s * 100,
            btc_change_60s=btc_change_60s * 100,
            btc_change_5m=btc_change_5m * 100,
            vol_1m=vol_1m * 100,
            vol_5m=vol_5m * 100,
            price_range_1m=range_1m,
            trade_flow_30s=trade_flow,
            large_trades_1m=0,  # Would need trade-by-trade data
            model_prob_yes=model_prob * 100,
            edge_vs_market=edge * 100,
        )

    def _calc_return(self, seconds: int) -> float:
        """Calculate return over last N seconds."""
        if len(self.btc_prices) < 2:
            return 0

        prices = list(self.btc_prices)
        now = prices[-1]["time"].timestamp()
        current = prices[-1]["price"]

        for p in reversed(prices):
            if now - p["time"].timestamp() >= seconds:
                return (current - p["price"]) / p["price"]

        return 0

    def _calc_volatility_metrics(self) -> tuple[float, float, float]:
        """Calculate volatility metrics."""
        if len(self.btc_prices) < 10:
            return 0.003, 0.003, 0

        prices = list(self.btc_prices)
        now = prices[-1]["time"].timestamp()

        # 1-minute volatility
        recent_1m = [p["price"] for p in prices if now - p["time"].timestamp() < 60]
        vol_1m = self._calc_volatility(recent_1m) if len(recent_1m) > 5 else 0.003

        # 5-minute volatility
        recent_5m = [p["price"] for p in prices if now - p["time"].timestamp() < 300]
        vol_5m = self._calc_volatility(recent_5m) if len(recent_5m) > 10 else 0.003

        # Price range
        range_1m = max(recent_1m) - min(recent_1m) if recent_1m else 0

        return vol_1m, vol_5m, range_1m

    def _calc_volatility(self, prices: list) -> float:
        """Calculate realized volatility."""
        if len(prices) < 2:
            return 0
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        if not returns:
            return 0
        return math.sqrt(sum(r**2 for r in returns) / len(returns))

    def _calc_trade_flow(self) -> float:
        """Estimate trade flow direction from price changes."""
        if len(self.btc_prices) < 10:
            return 0

        prices = list(self.btc_prices)
        now = prices[-1]["time"].timestamp()
        recent = [p for p in prices if now - p["time"].timestamp() < 30]

        if len(recent) < 2:
            return 0

        # Count up vs down moves
        ups = sum(1 for i in range(1, len(recent)) if recent[i]["price"] > recent[i-1]["price"])
        downs = len(recent) - 1 - ups

        return (ups - downs) / (len(recent) - 1) if len(recent) > 1 else 0

    def _calc_model_prob(self, distance_pct: float, mins_left: float, vol: float) -> float:
        """Calculate model probability."""
        from scipy import stats

        if mins_left <= 0:
            return 1.0 if distance_pct >= 0 else 0.0

        time_ratio = mins_left / 15.0
        expected_vol = max(vol, 0.001) * math.sqrt(time_ratio)

        z_score = distance_pct / expected_vol
        return stats.norm.cdf(z_score)

    def _print_tick(self, tick: TickData):
        """Print tick summary."""
        direction = "↑" if tick.btc_distance_pct > 0 else "↓"
        mins = tick.seconds_remaining // 60
        secs = tick.seconds_remaining % 60

        print(
            f"{tick.timestamp[11:19]} | "
            f"BTC: ${tick.btc_price:,.0f} {direction} | "
            f"Dist: {tick.btc_distance_pct:+.3f}% | "
            f"Time: {mins:02d}:{secs:02d} | "
            f"Mkt: {tick.yes_bid*100:.0f}/{tick.yes_ask*100:.0f}c | "
            f"Model: {tick.model_prob_yes:.0f}% | "
            f"Edge: {tick.edge_vs_market:+.1f}%"
        )

    def _save_data(self):
        """Save collected data to files."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save tick data
        tick_file = DATA_DIR / f"ticks_{timestamp}.csv"
        if self.tick_data:
            with open(tick_file, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=asdict(self.tick_data[0]).keys())
                writer.writeheader()
                for tick in self.tick_data:
                    writer.writerow(asdict(tick))
            print(f"\nSaved {len(self.tick_data)} ticks to {tick_file}")

        # Save window results
        results_file = DATA_DIR / f"windows_{timestamp}.csv"
        if self.window_results:
            with open(results_file, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=asdict(self.window_results[0]).keys())
                writer.writeheader()
                for result in self.window_results:
                    writer.writerow(asdict(result))
            print(f"Saved {len(self.window_results)} window results to {results_file}")

        # Save raw JSON for debugging
        json_file = DATA_DIR / f"raw_{timestamp}.json"
        with open(json_file, "w") as f:
            json.dump({
                "ticks": [asdict(t) for t in self.tick_data],
                "windows": [asdict(w) for w in self.window_results],
            }, f, indent=2)
        print(f"Saved raw data to {json_file}")

    def _print_summary(self):
        """Print collection summary."""
        print("\n" + "=" * 70)
        print("COLLECTION SUMMARY")
        print("=" * 70)

        print(f"\nTotal ticks collected: {len(self.tick_data)}")
        print(f"Windows observed: {len(self.window_results)}")

        if self.window_results:
            ups = sum(1 for w in self.window_results if w.outcome == "UP")
            downs = len(self.window_results) - ups
            print(f"\nOutcomes: {ups} UP, {downs} DOWN ({ups/len(self.window_results)*100:.1f}% UP)")

            # Pattern analysis
            print("\nPatterns observed:")

            # Early direction → outcome
            early_up_wins = sum(1 for w in self.window_results
                               if w.early_direction == "UP" and w.outcome == "UP")
            early_up_total = sum(1 for w in self.window_results if w.early_direction == "UP")
            if early_up_total > 0:
                print(f"  Early UP → Final UP: {early_up_wins}/{early_up_total} ({early_up_wins/early_up_total*100:.0f}%)")

            early_down_wins = sum(1 for w in self.window_results
                                 if w.early_direction == "DOWN" and w.outcome == "DOWN")
            early_down_total = sum(1 for w in self.window_results if w.early_direction == "DOWN")
            if early_down_total > 0:
                print(f"  Early DOWN → Final DOWN: {early_down_wins}/{early_down_total} ({early_down_wins/early_down_total*100:.0f}%)")

            # Reversals
            avg_reversals = sum(w.reversals for w in self.window_results) / len(self.window_results)
            print(f"  Avg reversals per window: {avg_reversals:.1f}")


async def main():
    print("\n" + "=" * 70)
    print("KALSHI 15M BTC LIVE DATA COLLECTOR")
    print("=" * 70)

    duration = input("\nCollection duration in minutes [60]: ").strip()
    duration = int(duration) if duration else 60

    collector = LiveDataCollector()
    await collector.start_collection(duration_minutes=duration)


if __name__ == "__main__":
    asyncio.run(main())
