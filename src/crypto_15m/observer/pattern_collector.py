#!/usr/bin/env python3
"""Pattern collector for finding market mispricing.

Collects for each window:
- Opening YES price (market's implied probability)
- Time of day (UTC hour)
- Prior outcomes (momentum state)
- Actual outcome

Goal: Find where market consistently misprices patterns.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import time

import httpx

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pattern_data.db"


class PatternCollector:
    """Collect pattern data to find market mispricing."""

    BINANCE_REST = "https://api.binance.us/api/v3"
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self):
        self.db = sqlite3.connect(DB_PATH)
        self._init_db()
        self.current_window_id = None
        self.window_start_data = None
        self._running = False

    def _init_db(self):
        """Initialize database."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id TEXT UNIQUE,

                -- Timing
                utc_hour INTEGER,
                utc_minute INTEGER,
                day_of_week INTEGER,

                -- Opening state (first 30 seconds)
                open_yes_mid REAL,
                open_btc_price REAL,
                open_distance_pct REAL,
                target_price REAL,

                -- Prior outcomes (momentum)
                prior_1_outcome TEXT,
                prior_2_outcome TEXT,
                prior_3_outcome TEXT,
                momentum_state TEXT,

                -- Closing state
                close_yes_mid REAL,
                close_btc_price REAL,
                close_distance_pct REAL,

                -- Result
                outcome TEXT,

                -- Analysis
                market_was_right INTEGER,
                edge_pct REAL,

                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_windows_hour ON windows(utc_hour)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_windows_momentum ON windows(momentum_state)
        """)

        self.db.commit()

    def _get_prior_outcomes(self, n: int = 3) -> list[str]:
        """Get prior N window outcomes."""
        cursor = self.db.execute("""
            SELECT outcome FROM windows
            WHERE outcome IS NOT NULL
            ORDER BY id DESC LIMIT ?
        """, (n,))
        return [row[0] for row in cursor.fetchall()]

    def _calc_momentum_state(self, priors: list[str]) -> str:
        """Calculate momentum state from prior outcomes."""
        if len(priors) < 2:
            return "insufficient_data"

        if priors[0] == priors[1] == "UP":
            return "strong_up"  # 2 UPs in a row
        elif priors[0] == priors[1] == "DOWN":
            return "strong_down"  # 2 DOWNs in a row
        elif len(priors) >= 3 and priors[0] == priors[1] == priors[2]:
            return f"streak_{priors[0].lower()}_3"  # 3 in a row
        elif priors[0] != priors[1]:
            return "alternating"
        else:
            return "mixed"

    async def start(self):
        """Start collecting pattern data."""
        print("=" * 60)
        print("PATTERN COLLECTOR - Finding Market Mispricing")
        print("=" * 60)
        print(f"\nDatabase: {DB_PATH}")
        print("\nCollecting: opening_price | time | momentum | outcome")
        print("Press Ctrl+C to stop\n")

        self._running = True

        async with httpx.AsyncClient(timeout=5) as client:
            while self._running:
                try:
                    await self._collection_cycle(client)
                except Exception as e:
                    print(f"Error: {e}")

                await asyncio.sleep(1)

    async def _collection_cycle(self, client: httpx.AsyncClient):
        """One collection cycle."""
        ts = time.time()
        now = datetime.now(timezone.utc)

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

        if not market or btc_price == 0:
            return

        window_id = market.get("ticker", "")
        target = float(market.get("floor_strike", 0) or 0)
        yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0)
        yes_mid = (yes_bid + yes_ask) / 2

        close_time = datetime.fromisoformat(
            market.get("close_time", "").replace("Z", "+00:00")
        )
        secs_remaining = max(0, int((close_time - now).total_seconds()))

        distance_pct = (btc_price - target) / target * 100 if target > 0 else 0

        # New window detected
        if window_id != self.current_window_id:
            # Finalize previous window
            if self.current_window_id and self.window_start_data:
                await self._finalize_window(btc_price, yes_mid, distance_pct)

            # Start tracking new window
            self.current_window_id = window_id
            priors = self._get_prior_outcomes(3)
            momentum = self._calc_momentum_state(priors)

            self.window_start_data = {
                "window_id": window_id,
                "utc_hour": now.hour,
                "utc_minute": now.minute,
                "day_of_week": now.weekday(),
                "open_yes_mid": yes_mid,
                "open_btc_price": btc_price,
                "open_distance_pct": distance_pct,
                "target_price": target,
                "prior_1": priors[0] if len(priors) > 0 else None,
                "prior_2": priors[1] if len(priors) > 1 else None,
                "prior_3": priors[2] if len(priors) > 2 else None,
                "momentum_state": momentum,
            }

            print(f"\n{'='*60}")
            print(f"NEW WINDOW: {window_id}")
            print(f"  Time: {now.hour:02d}:{now.minute:02d} UTC ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][now.weekday()]})")
            print(f"  Opening YES: {yes_mid*100:.1f}c (market thinks {yes_mid*100:.1f}% UP)")
            print(f"  BTC: ${btc_price:,.0f} ({distance_pct:+.3f}% from target)")
            print(f"  Momentum: {momentum} (prior: {', '.join(priors[:3]) if priors else 'none'})")
            print(f"{'='*60}")

        # Display tick
        direction = "↑" if distance_pct > 0 else "↓"
        print(
            f"{now.strftime('%H:%M:%S')} | "
            f"BTC: ${btc_price:,.0f} {direction}{distance_pct:+.3f}% | "
            f"T-{secs_remaining:3}s | "
            f"YES: {yes_mid*100:.1f}c"
        )

    async def _finalize_window(self, close_btc: float, close_yes: float, close_dist: float):
        """Finalize window and record outcome."""
        if not self.window_start_data:
            return

        data = self.window_start_data
        target = data["target_price"]
        outcome = "UP" if close_btc >= target else "DOWN"

        # Did market predict correctly?
        market_predicted_up = data["open_yes_mid"] > 0.5
        actual_up = outcome == "UP"
        market_was_right = 1 if market_predicted_up == actual_up else 0

        # Edge calculation
        # If market said 40% UP but it went UP, edge = 60% (we could have bet YES)
        # If market said 40% UP and it went DOWN, edge = -40% (market was right-ish)
        if actual_up:
            edge_pct = (1 - data["open_yes_mid"]) * 100  # What we'd have won betting YES
        else:
            edge_pct = -data["open_yes_mid"] * 100  # What we'd have lost betting YES

        # Store in database
        self.db.execute("""
            INSERT OR REPLACE INTO windows (
                window_id, utc_hour, utc_minute, day_of_week,
                open_yes_mid, open_btc_price, open_distance_pct, target_price,
                prior_1_outcome, prior_2_outcome, prior_3_outcome, momentum_state,
                close_yes_mid, close_btc_price, close_distance_pct,
                outcome, market_was_right, edge_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["window_id"], data["utc_hour"], data["utc_minute"], data["day_of_week"],
            data["open_yes_mid"], data["open_btc_price"], data["open_distance_pct"], data["target_price"],
            data["prior_1"], data["prior_2"], data["prior_3"], data["momentum_state"],
            close_yes, close_btc, close_dist,
            outcome, market_was_right, edge_pct
        ))
        self.db.commit()

        print(f"\n{'='*60}")
        print(f"WINDOW SETTLED: {data['window_id']}")
        print(f"  Outcome: {outcome}")
        print(f"  Market predicted: {'UP' if market_predicted_up else 'DOWN'} ({data['open_yes_mid']*100:.1f}c)")
        print(f"  Market was: {'RIGHT' if market_was_right else 'WRONG'}")
        print(f"  Edge if bet YES: {edge_pct:+.1f}%")
        print(f"{'='*60}")

        self.window_start_data = None

    def analyze(self):
        """Analyze collected data for mispricing patterns."""
        print("\n" + "=" * 60)
        print("PATTERN ANALYSIS - Finding Market Mispricing")
        print("=" * 60)

        # Total windows
        cursor = self.db.execute("SELECT COUNT(*) FROM windows WHERE outcome IS NOT NULL")
        total = cursor.fetchone()[0]
        print(f"\nTotal windows analyzed: {total}")

        if total < 10:
            print("Need more data (at least 10 windows) for meaningful analysis.")
            return

        # Overall market accuracy
        cursor = self.db.execute("""
            SELECT AVG(market_was_right) * 100 FROM windows WHERE outcome IS NOT NULL
        """)
        accuracy = cursor.fetchone()[0]
        print(f"Market accuracy: {accuracy:.1f}%")

        # By hour
        print("\n--- BY HOUR (UTC) ---")
        cursor = self.db.execute("""
            SELECT utc_hour,
                   COUNT(*) as n,
                   AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) * 100 as actual_up_pct,
                   AVG(open_yes_mid) * 100 as market_implied_pct,
                   AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) * 100 - AVG(open_yes_mid) * 100 as edge
            FROM windows
            WHERE outcome IS NOT NULL
            GROUP BY utc_hour
            HAVING n >= 3
            ORDER BY edge DESC
        """)

        print(f"{'Hour':<6} {'N':<4} {'Actual UP%':<12} {'Market Said':<12} {'EDGE':<8}")
        print("-" * 50)
        for row in cursor.fetchall():
            hour, n, actual, market, edge = row
            edge_indicator = "***" if abs(edge) > 10 else ""
            print(f"{hour:02d}:00  {n:<4} {actual:>10.1f}%  {market:>10.1f}%  {edge:>+6.1f}% {edge_indicator}")

        # By momentum
        print("\n--- BY MOMENTUM STATE ---")
        cursor = self.db.execute("""
            SELECT momentum_state,
                   COUNT(*) as n,
                   AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) * 100 as actual_up_pct,
                   AVG(open_yes_mid) * 100 as market_implied_pct,
                   AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) * 100 - AVG(open_yes_mid) * 100 as edge
            FROM windows
            WHERE outcome IS NOT NULL AND momentum_state != 'insufficient_data'
            GROUP BY momentum_state
            HAVING n >= 3
            ORDER BY edge DESC
        """)

        print(f"{'Momentum':<20} {'N':<4} {'Actual UP%':<12} {'Market Said':<12} {'EDGE':<8}")
        print("-" * 60)
        for row in cursor.fetchall():
            momentum, n, actual, market, edge = row
            edge_indicator = "***" if abs(edge) > 10 else ""
            print(f"{momentum:<20} {n:<4} {actual:>10.1f}%  {market:>10.1f}%  {edge:>+6.1f}% {edge_indicator}")

        # By opening price bucket
        print("\n--- BY OPENING PRICE BUCKET ---")
        cursor = self.db.execute("""
            SELECT
                CASE
                    WHEN open_yes_mid < 0.3 THEN '0-30c (strong DOWN)'
                    WHEN open_yes_mid < 0.45 THEN '30-45c (lean DOWN)'
                    WHEN open_yes_mid < 0.55 THEN '45-55c (toss-up)'
                    WHEN open_yes_mid < 0.7 THEN '55-70c (lean UP)'
                    ELSE '70-100c (strong UP)'
                END as bucket,
                COUNT(*) as n,
                AVG(CASE WHEN outcome = 'UP' THEN 1.0 ELSE 0.0 END) * 100 as actual_up_pct,
                AVG(open_yes_mid) * 100 as market_implied_pct
            FROM windows
            WHERE outcome IS NOT NULL
            GROUP BY bucket
            ORDER BY market_implied_pct
        """)

        print(f"{'Price Bucket':<25} {'N':<4} {'Actual UP%':<12} {'Market Said':<12} {'Calibrated?':<10}")
        print("-" * 70)
        for row in cursor.fetchall():
            bucket, n, actual, market = row
            diff = abs(actual - market)
            calibrated = "YES" if diff < 10 else "NO - EDGE!"
            print(f"{bucket:<25} {n:<4} {actual:>10.1f}%  {market:>10.1f}%  {calibrated}")

        print("\n*** = Potential edge >10%")
        print("\nLook for patterns where Actual% differs significantly from Market%")
        print("That's where systematic betting can win >50%")

    def stop(self):
        """Stop collection."""
        self._running = False
        self.db.commit()
        self.db.close()


async def main():
    collector = PatternCollector()

    try:
        await collector.start()
    except KeyboardInterrupt:
        print("\n\nStopping...")
        collector.stop()
        collector.analyze()


if __name__ == "__main__":
    asyncio.run(main())
