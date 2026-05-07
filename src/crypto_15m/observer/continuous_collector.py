#!/usr/bin/env python3
"""Continuous data collector that runs for hours and captures correlations.

Captures every second:
- BTC price (Binance REST - reliable)
- Kalshi YES/NO bid/ask
- Computes and logs correlations in real-time

Run this in background: nohup python -m src.crypto_15m.observer.continuous_collector &
"""

import asyncio
import json
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import time

import httpx

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data" / "continuous"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "market_data.db"


class ContinuousCollector:
    """Continuous market data collector with SQLite storage."""

    BINANCE_REST = "https://api.binance.us/api/v3"
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self):
        self.db = sqlite3.connect(DB_PATH)
        self._init_db()
        self._running = False

        # Rolling window for real-time correlation
        self.recent_data: list[dict] = []  # Last 60 seconds

    def _init_db(self):
        """Initialize SQLite database."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                window_id TEXT,
                secs_remaining INTEGER,
                btc_price REAL,
                target_price REAL,
                distance_pct REAL,
                yes_bid REAL,
                yes_ask REAL,
                yes_mid REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id TEXT UNIQUE,
                target_price REAL,
                start_ts REAL,
                end_ts REAL,
                outcome TEXT,
                open_btc REAL,
                close_btc REAL,
                high_btc REAL,
                low_btc REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticks_window ON ticks(window_id)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts)
        """)

        self.db.commit()

    async def start(self):
        """Start continuous collection."""
        print("=" * 70)
        print("CONTINUOUS MARKET DATA COLLECTOR")
        print("=" * 70)
        print(f"\nDatabase: {DB_PATH}")
        print("Press Ctrl+C to stop\n")

        self._running = True
        current_window = None

        async with httpx.AsyncClient(timeout=5) as client:
            while self._running:
                try:
                    ts = time.time()

                    # Get BTC price
                    btc_resp = await client.get(
                        f"{self.BINANCE_REST}/ticker/price",
                        params={"symbol": "BTCUSDT"}
                    )
                    btc_price = float(btc_resp.json()["price"]) if btc_resp.status_code == 200 else 0

                    # Get Kalshi market
                    kalshi_resp = await client.get(
                        f"{self.KALSHI_API}/markets",
                        params={"series_ticker": "KXBTC15M", "status": "open", "limit": 5}
                    )

                    market = None
                    if kalshi_resp.status_code == 200:
                        markets = kalshi_resp.json().get("markets", [])
                        active = [m for m in markets if m.get("status") == "active"]
                        if active:
                            market = active[0]

                    if market and btc_price > 0:
                        window_id = market.get("ticker", "")
                        target = float(market.get("floor_strike", 0) or 0)
                        yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
                        yes_ask = float(market.get("yes_ask_dollars", 0) or 0)

                        close_time = datetime.fromisoformat(
                            market.get("close_time", "").replace("Z", "+00:00")
                        )
                        secs_remaining = max(0, int((close_time - datetime.now(timezone.utc)).total_seconds()))

                        distance_pct = (btc_price - target) / target * 100 if target > 0 else 0
                        yes_mid = (yes_bid + yes_ask) / 2

                        # New window?
                        if current_window != window_id:
                            if current_window:
                                await self._finalize_window(current_window)
                            current_window = window_id
                            print(f"\n=== NEW WINDOW: {window_id} ===")
                            print(f"    Target: ${target:,.2f}")

                        # Store tick
                        self.db.execute("""
                            INSERT INTO ticks (ts, window_id, secs_remaining, btc_price,
                                             target_price, distance_pct, yes_bid, yes_ask, yes_mid)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (ts, window_id, secs_remaining, btc_price, target,
                              distance_pct, yes_bid, yes_ask, yes_mid))

                        # Add to rolling window
                        self.recent_data.append({
                            "ts": ts,
                            "btc": btc_price,
                            "yes_mid": yes_mid,
                            "distance": distance_pct,
                        })
                        # Keep last 60 seconds
                        self.recent_data = [d for d in self.recent_data if ts - d["ts"] < 60]

                        # Calculate real-time correlation
                        corr = self._calc_correlation()

                        # Display
                        direction = "↑" if distance_pct > 0 else "↓"
                        print(
                            f"{datetime.now().strftime('%H:%M:%S')} | "
                            f"BTC: ${btc_price:,.0f} {direction}{distance_pct:+.3f}% | "
                            f"T-{secs_remaining:3}s | "
                            f"Yes: {yes_bid*100:.0f}/{yes_ask*100:.0f}c | "
                            f"Corr: {corr:+.2f}"
                        )

                        # Commit every 10 seconds
                        if int(ts) % 10 == 0:
                            self.db.commit()

                except Exception as e:
                    print(f"Error: {e}")

                # Sleep to maintain 1-second cadence
                elapsed = time.time() - ts
                if elapsed < 1:
                    await asyncio.sleep(1 - elapsed)

    async def _finalize_window(self, window_id: str):
        """Finalize a completed window."""
        # Get all ticks for this window
        cursor = self.db.execute("""
            SELECT btc_price, secs_remaining, yes_mid
            FROM ticks
            WHERE window_id = ?
            ORDER BY ts
        """, (window_id,))
        rows = cursor.fetchall()

        if not rows:
            return

        btc_prices = [r[0] for r in rows]
        first_tick = rows[0]
        last_tick = rows[-1]

        # Get target from first tick
        cursor = self.db.execute("""
            SELECT target_price FROM ticks WHERE window_id = ? LIMIT 1
        """, (window_id,))
        target_row = cursor.fetchone()
        target = target_row[0] if target_row else 0

        outcome = "yes" if btc_prices[-1] >= target else "no"

        # Store window result
        self.db.execute("""
            INSERT OR REPLACE INTO windows
            (window_id, target_price, outcome, open_btc, close_btc, high_btc, low_btc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (window_id, target, outcome, btc_prices[0], btc_prices[-1],
              max(btc_prices), min(btc_prices)))

        self.db.commit()

        print(f"\n=== WINDOW SETTLED: {window_id} ===")
        print(f"    Outcome: {outcome.upper()}")
        print(f"    Target: ${target:,.2f}")
        print(f"    Close: ${btc_prices[-1]:,.2f}")

    def _calc_correlation(self) -> float:
        """Calculate rolling correlation between BTC moves and Kalshi moves."""
        if len(self.recent_data) < 10:
            return 0.0

        # Calculate moves
        btc_moves = []
        kalshi_moves = []

        for i in range(1, len(self.recent_data)):
            btc_move = self.recent_data[i]["btc"] - self.recent_data[i-1]["btc"]
            kalshi_move = self.recent_data[i]["yes_mid"] - self.recent_data[i-1]["yes_mid"]
            btc_moves.append(btc_move)
            kalshi_moves.append(kalshi_move)

        if not btc_moves:
            return 0.0

        # Pearson correlation
        n = len(btc_moves)
        mean_btc = sum(btc_moves) / n
        mean_kal = sum(kalshi_moves) / n

        cov = sum((btc_moves[i] - mean_btc) * (kalshi_moves[i] - mean_kal) for i in range(n)) / n
        std_btc = (sum((x - mean_btc)**2 for x in btc_moves) / n) ** 0.5
        std_kal = (sum((x - mean_kal)**2 for x in kalshi_moves) / n) ** 0.5

        if std_btc > 0 and std_kal > 0:
            return cov / (std_btc * std_kal)
        return 0.0

    def stop(self):
        """Stop collection."""
        self._running = False
        self.db.commit()
        self.db.close()

    def analyze_data(self):
        """Analyze collected data."""
        print("\n" + "=" * 70)
        print("DATA ANALYSIS")
        print("=" * 70)

        # Count records
        cursor = self.db.execute("SELECT COUNT(*) FROM ticks")
        tick_count = cursor.fetchone()[0]
        print(f"\nTotal ticks: {tick_count}")

        cursor = self.db.execute("SELECT COUNT(*) FROM windows")
        window_count = cursor.fetchone()[0]
        print(f"Windows completed: {window_count}")

        # Win rate
        cursor = self.db.execute("""
            SELECT outcome, COUNT(*) FROM windows GROUP BY outcome
        """)
        outcomes = dict(cursor.fetchall())
        if outcomes:
            yes_count = outcomes.get("yes", 0)
            no_count = outcomes.get("no", 0)
            total = yes_count + no_count
            print(f"Outcomes: {yes_count} UP, {no_count} DOWN ({yes_count/total*100:.1f}% UP)")


async def main():
    collector = ContinuousCollector()

    try:
        await collector.start()
    except KeyboardInterrupt:
        print("\n\nStopping...")
        collector.stop()
        collector.analyze_data()


if __name__ == "__main__":
    asyncio.run(main())
