#!/usr/bin/env python3
"""High-frequency data collector for BTC/Kalshi correlation analysis.

Captures:
1. Every BTC trade from Binance WebSocket (multiple per second)
2. Kalshi bid/ask every second
3. Timestamps aligned for correlation analysis

Goal: See how fast Kalshi responds to BTC moves.
"""

import asyncio
import json
import csv
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from typing import Optional
import time

import httpx
import websockets

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data" / "hf_observations"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class HighFrequencyCollector:
    """High-frequency data collector with proper WebSocket handling."""

    BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"  # Global endpoint
    BINANCE_REST = "https://api.binance.us/api/v3"
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self):
        # BTC data - every tick
        self.btc_ticks: list[dict] = []
        self.current_btc: float = 0
        self.btc_updated_at: float = 0

        # Kalshi data - every second
        self.kalshi_ticks: list[dict] = []
        self.current_market: Optional[dict] = None

        # Combined for analysis
        self.combined_data: list[dict] = []

        self._running = False
        self._use_rest_fallback = False

    async def start(self, duration_seconds: int = 900):
        """Start high-frequency collection."""
        print("=" * 70)
        print("HIGH-FREQUENCY COLLECTOR")
        print("=" * 70)
        print(f"\nCollecting for {duration_seconds} seconds ({duration_seconds//60} min)")
        print(f"Data directory: {DATA_DIR}\n")

        self._running = True
        start_time = time.time()

        # Start both collection tasks
        btc_task = asyncio.create_task(self._collect_btc())
        kalshi_task = asyncio.create_task(self._collect_kalshi())
        display_task = asyncio.create_task(self._display_loop())

        # Run for duration
        try:
            await asyncio.sleep(duration_seconds)
        except asyncio.CancelledError:
            pass

        self._running = False

        # Cancel tasks
        btc_task.cancel()
        kalshi_task.cancel()
        display_task.cancel()

        try:
            await btc_task
        except asyncio.CancelledError:
            pass
        try:
            await kalshi_task
        except asyncio.CancelledError:
            pass
        try:
            await display_task
        except asyncio.CancelledError:
            pass

        # Save data
        self._save_data()
        self._analyze_correlation()

    async def _collect_btc(self):
        """Collect BTC trades - try WebSocket, fallback to REST polling."""
        reconnect_count = 0

        # Try WebSocket first
        while self._running and not self._use_rest_fallback:
            try:
                async with websockets.connect(
                    self.BINANCE_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    print("[BTC] WebSocket connected")
                    reconnect_count = 0

                    async for message in ws:
                        if not self._running:
                            break

                        data = json.loads(message)
                        price = float(data["p"])
                        ts = data["T"] / 1000  # Unix timestamp

                        self.current_btc = price
                        self.btc_updated_at = ts

                        self.btc_ticks.append({
                            "ts": ts,
                            "price": price,
                            "qty": float(data["q"]),
                            "is_buy": not data["m"],  # m=True means seller is maker
                        })

            except Exception as e:
                if self._running:
                    reconnect_count += 1
                    print(f"[BTC] WebSocket error: {e}")
                    if reconnect_count >= 3:
                        print("[BTC] Switching to REST polling fallback")
                        self._use_rest_fallback = True
                        break
                    await asyncio.sleep(1)

        # REST fallback - poll every 200ms
        if self._use_rest_fallback:
            async with httpx.AsyncClient(timeout=2) as client:
                while self._running:
                    try:
                        ts = time.time()
                        resp = await client.get(
                            f"{self.BINANCE_REST}/ticker/price",
                            params={"symbol": "BTCUSDT"}
                        )
                        if resp.status_code == 200:
                            price = float(resp.json()["price"])
                            self.current_btc = price
                            self.btc_updated_at = ts

                            self.btc_ticks.append({
                                "ts": ts,
                                "price": price,
                                "qty": 0,
                                "is_buy": None,
                            })
                    except Exception as e:
                        print(f"[BTC REST] Error: {e}")

                    await asyncio.sleep(0.2)  # 5 updates per second

    async def _collect_kalshi(self):
        """Collect Kalshi data every second."""
        async with httpx.AsyncClient(timeout=5) as client:
            while self._running:
                try:
                    ts = time.time()

                    # Get active market
                    resp = await client.get(
                        f"{self.KALSHI_API}/markets",
                        params={"series_ticker": "KXBTC15M", "status": "open", "limit": 5}
                    )

                    if resp.status_code == 200:
                        markets = resp.json().get("markets", [])
                        active = [m for m in markets if m.get("status") == "active"]

                        if active:
                            m = active[0]
                            self.current_market = m

                            close_time = datetime.fromisoformat(
                                m.get("close_time", "").replace("Z", "+00:00")
                            )
                            secs_remaining = max(0, (close_time - datetime.now(timezone.utc)).total_seconds())

                            tick = {
                                "ts": ts,
                                "ticker": m.get("ticker", ""),
                                "target": float(m.get("floor_strike", 0) or 0),
                                "yes_bid": float(m.get("yes_bid_dollars", 0) or 0),
                                "yes_ask": float(m.get("yes_ask_dollars", 0) or 0),
                                "secs_remaining": secs_remaining,
                                "btc_at_sample": self.current_btc,
                                "btc_age_ms": (ts - self.btc_updated_at) * 1000,
                            }
                            self.kalshi_ticks.append(tick)

                            # Combined data point
                            if self.current_btc > 0:
                                self.combined_data.append({
                                    "ts": ts,
                                    "btc": self.current_btc,
                                    "target": tick["target"],
                                    "distance_pct": (self.current_btc - tick["target"]) / tick["target"] * 100 if tick["target"] > 0 else 0,
                                    "yes_bid": tick["yes_bid"],
                                    "yes_ask": tick["yes_ask"],
                                    "yes_mid": (tick["yes_bid"] + tick["yes_ask"]) / 2,
                                    "secs_left": secs_remaining,
                                    "ticker": tick["ticker"],
                                })

                except Exception as e:
                    print(f"[Kalshi] Error: {e}")

                # Sleep remainder of second
                elapsed = time.time() - ts
                if elapsed < 1:
                    await asyncio.sleep(1 - elapsed)

    async def _display_loop(self):
        """Display current state every second."""
        last_btc = 0
        last_yes = 0

        while self._running:
            await asyncio.sleep(1)

            if not self.current_market or self.current_btc == 0:
                continue

            m = self.current_market
            target = float(m.get("floor_strike", 0) or 0)
            yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
            yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
            yes_mid = (yes_bid + yes_ask) / 2

            close_time = datetime.fromisoformat(m.get("close_time", "").replace("Z", "+00:00"))
            secs_left = max(0, (close_time - datetime.now(timezone.utc)).total_seconds())

            # Calculate changes
            btc_change = self.current_btc - last_btc if last_btc > 0 else 0
            yes_change = (yes_mid - last_yes) * 100 if last_yes > 0 else 0

            distance_pct = (self.current_btc - target) / target * 100 if target > 0 else 0

            # Direction arrows
            btc_arrow = "↑" if btc_change > 0 else ("↓" if btc_change < 0 else "→")
            yes_arrow = "↑" if yes_change > 0.1 else ("↓" if yes_change < -0.1 else "→")

            print(
                f"{datetime.now().strftime('%H:%M:%S')} | "
                f"BTC: ${self.current_btc:,.0f} {btc_arrow}{btc_change:+.0f} | "
                f"Dist: {distance_pct:+.3f}% | "
                f"T-{int(secs_left)}s | "
                f"Yes: {yes_bid*100:.0f}/{yes_ask*100:.0f}c {yes_arrow}{yes_change:+.1f}c | "
                f"Ticks: {len(self.btc_ticks)}"
            )

            last_btc = self.current_btc
            last_yes = yes_mid

    def _save_data(self):
        """Save collected data."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save BTC ticks
        if self.btc_ticks:
            btc_file = DATA_DIR / f"btc_ticks_{ts}.csv"
            with open(btc_file, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.btc_ticks[0].keys())
                writer.writeheader()
                writer.writerows(self.btc_ticks)
            print(f"\nSaved {len(self.btc_ticks)} BTC ticks to {btc_file}")

        # Save Kalshi ticks
        if self.kalshi_ticks:
            kalshi_file = DATA_DIR / f"kalshi_ticks_{ts}.csv"
            with open(kalshi_file, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.kalshi_ticks[0].keys())
                writer.writeheader()
                writer.writerows(self.kalshi_ticks)
            print(f"Saved {len(self.kalshi_ticks)} Kalshi ticks to {kalshi_file}")

        # Save combined
        if self.combined_data:
            combined_file = DATA_DIR / f"combined_{ts}.csv"
            with open(combined_file, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.combined_data[0].keys())
                writer.writeheader()
                writer.writerows(self.combined_data)
            print(f"Saved {len(self.combined_data)} combined records to {combined_file}")

    def _analyze_correlation(self):
        """Quick correlation analysis."""
        print("\n" + "=" * 70)
        print("CORRELATION ANALYSIS")
        print("=" * 70)

        if len(self.combined_data) < 10:
            print("Not enough data for analysis")
            return

        # Calculate BTC moves and corresponding Kalshi moves
        btc_moves = []
        kalshi_moves = []

        for i in range(1, len(self.combined_data)):
            prev = self.combined_data[i-1]
            curr = self.combined_data[i]

            if prev["ticker"] != curr["ticker"]:
                continue  # Skip window transitions

            btc_move = curr["btc"] - prev["btc"]
            kalshi_move = curr["yes_mid"] - prev["yes_mid"]

            btc_moves.append(btc_move)
            kalshi_moves.append(kalshi_move)

        if not btc_moves:
            print("No valid move pairs")
            return

        # Basic stats
        print(f"\nData points: {len(btc_moves)}")

        # Correlation
        n = len(btc_moves)
        mean_btc = sum(btc_moves) / n
        mean_kal = sum(kalshi_moves) / n

        cov = sum((btc_moves[i] - mean_btc) * (kalshi_moves[i] - mean_kal) for i in range(n)) / n
        std_btc = (sum((x - mean_btc)**2 for x in btc_moves) / n) ** 0.5
        std_kal = (sum((x - mean_kal)**2 for x in kalshi_moves) / n) ** 0.5

        if std_btc > 0 and std_kal > 0:
            corr = cov / (std_btc * std_kal)
            print(f"Correlation (BTC move → Kalshi move): {corr:.3f}")
        else:
            print("Cannot calculate correlation (no variance)")

        # Response analysis: when BTC moves $X, how much does Kalshi move?
        print("\nBTC move → Kalshi response:")

        buckets = [
            (-1000, -50, "BTC drops $50+"),
            (-50, -20, "BTC drops $20-50"),
            (-20, 0, "BTC drops $0-20"),
            (0, 20, "BTC rises $0-20"),
            (20, 50, "BTC rises $20-50"),
            (50, 1000, "BTC rises $50+"),
        ]

        for low, high, label in buckets:
            moves = [(btc_moves[i], kalshi_moves[i]) for i in range(n)
                     if low <= btc_moves[i] < high]
            if moves:
                avg_kalshi = sum(m[1] for m in moves) / len(moves)
                print(f"  {label}: {len(moves)} instances, avg Kalshi move: {avg_kalshi*100:+.2f}c")

        # Lag analysis: does Kalshi lead or lag BTC?
        print("\nLag analysis (does Kalshi anticipate BTC moves?):")

        # Compare BTC move at t+1 with Kalshi move at t
        lead_pairs = [(btc_moves[i], kalshi_moves[i-1]) for i in range(1, n)]
        if lead_pairs:
            lead_corr_sum = sum(b * k for b, k in lead_pairs)
            print(f"  Kalshi at t vs BTC at t+1: {lead_corr_sum:.2f} (positive = Kalshi leads)")


async def main():
    print("\n" + "=" * 70)
    print("HIGH-FREQUENCY BTC/KALSHI COLLECTOR")
    print("=" * 70)
    print("\nThis collects:")
    print("  - Every BTC trade (Binance WebSocket)")
    print("  - Kalshi prices every second")
    print("  - Correlation analysis\n")

    duration = input("Duration in seconds [900 = 15 min]: ").strip()
    duration = int(duration) if duration else 900

    collector = HighFrequencyCollector()
    await collector.start(duration_seconds=duration)


if __name__ == "__main__":
    asyncio.run(main())
