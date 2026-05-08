#!/usr/bin/env python3
"""
Kalshi BTC Market Data Collector with Lag Tracking
===================================================
Collects second-by-second bid/ask data for BTC prediction markets.
Tracks the lag between Binance price moves and Kalshi orderbook adjustments.

Usage (PowerShell):
    python kalshi_collector.py

Output: kalshi_data_YYYYMMDD_HHMMSS.jsonl (one JSON object per line)

Share the output file for LLM analysis of patterns and lag analysis.
"""

import json
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

try:
    import requests
except ImportError:
    print("Installing requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Market series to track
SERIES = [
    "KXBTC15M",   # 15-minute BTC markets
    "KXBTC",       # Hourly BTC markets (if available)
    "KXBTCD",      # Daily BTC markets (if available)
]

# Lag tracking settings
MOVE_THRESHOLD_PCT = 0.03  # Minimum BTC move to track (0.03% = ~$30 on $100k BTC)
LAG_TIMEOUT_SECS = 15      # Stop tracking lag after this many seconds
PRICE_HISTORY_SIZE = 60    # Keep 60 seconds of price history


class LagTracker:
    """Tracks lag between Binance price moves and Kalshi adjustments."""

    def __init__(self):
        self.price_history = deque(maxlen=PRICE_HISTORY_SIZE)
        self.active_events = []  # Price events waiting for Kalshi to catch up
        self.completed_lags = []  # Measured lag events

    def add_price(self, ts, btc_price):
        """Record a new BTC price point."""
        if btc_price is None:
            return
        self.price_history.append({"ts": ts, "price": btc_price})

    def detect_move(self, ts, btc_price):
        """Detect if BTC just made a significant move."""
        if btc_price is None or len(self.price_history) < 2:
            return None

        # Compare to price 1-3 seconds ago
        for i in range(-2, -min(4, len(self.price_history)) - 1, -1):
            old = self.price_history[i]
            if ts - old["ts"] > 5:  # Don't compare to stale data
                continue

            pct_change = (btc_price - old["price"]) / old["price"] * 100
            if abs(pct_change) >= MOVE_THRESHOLD_PCT:
                return {
                    "ts_detected": ts,
                    "old_price": old["price"],
                    "new_price": btc_price,
                    "pct_change": pct_change,
                    "direction": "UP" if pct_change > 0 else "DOWN"
                }
        return None

    def start_tracking(self, event, kalshi_yes_mid):
        """Start tracking a price event for lag measurement."""
        event["kalshi_at_detection"] = kalshi_yes_mid
        event["expected_direction"] = "higher" if event["direction"] == "UP" else "lower"
        self.active_events.append(event)

    def check_kalshi_caught_up(self, ts, kalshi_yes_mid, btc_price, target_price):
        """Check if Kalshi has adjusted to reflect recent BTC moves."""
        if not self.active_events or kalshi_yes_mid is None:
            return []

        completed = []
        still_active = []

        for event in self.active_events:
            elapsed = ts - event["ts_detected"]

            # Timeout - Kalshi never caught up
            if elapsed > LAG_TIMEOUT_SECS:
                event["lag_secs"] = None  # Timeout
                event["caught_up"] = False
                event["timeout"] = True
                completed.append(event)
                continue

            # Check if Kalshi price moved in expected direction
            initial = event["kalshi_at_detection"]
            change = kalshi_yes_mid - initial

            # Consider "caught up" if Kalshi moved at least 1c in expected direction
            threshold = 0.01
            if event["expected_direction"] == "higher" and change >= threshold:
                event["lag_secs"] = elapsed
                event["caught_up"] = True
                event["kalshi_final"] = kalshi_yes_mid
                event["kalshi_change"] = change
                completed.append(event)
            elif event["expected_direction"] == "lower" and change <= -threshold:
                event["lag_secs"] = elapsed
                event["caught_up"] = True
                event["kalshi_final"] = kalshi_yes_mid
                event["kalshi_change"] = change
                completed.append(event)
            else:
                still_active.append(event)

        self.active_events = still_active
        self.completed_lags.extend(completed)
        return completed

    def get_stats(self):
        """Get summary statistics of lag measurements."""
        if not self.completed_lags:
            return None

        caught_up = [e for e in self.completed_lags if e.get("caught_up")]
        timeouts = [e for e in self.completed_lags if e.get("timeout")]

        if caught_up:
            lags = [e["lag_secs"] for e in caught_up]
            return {
                "total_events": len(self.completed_lags),
                "caught_up": len(caught_up),
                "timeouts": len(timeouts),
                "avg_lag_secs": sum(lags) / len(lags),
                "min_lag_secs": min(lags),
                "max_lag_secs": max(lags),
            }
        return {
            "total_events": len(self.completed_lags),
            "caught_up": 0,
            "timeouts": len(timeouts),
        }


def get_btc_price():
    """Fetch current BTC price from Binance."""
    try:
        resp = requests.get(
            "https://api.binance.us/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5
        )
        if resp.status_code == 200:
            return float(resp.json()["price"])
    except:
        pass
    return None


def get_active_markets():
    """Fetch all active BTC markets."""
    markets = []

    for series in SERIES:
        try:
            resp = requests.get(
                f"{KALSHI_API}/markets",
                params={"series_ticker": series, "status": "open", "limit": 10},
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get("markets", []):
                    if m.get("status") == "active":
                        markets.append(m)
        except Exception as e:
            pass  # Silently skip failed series

    return markets


def collect_tick(markets, btc_price):
    """Collect one tick of data for all markets."""
    ts = time.time()
    now = datetime.now(timezone.utc)

    ticks = []

    for m in markets:
        ticker = m.get("ticker", "")

        # Parse close time
        close_time_str = m.get("close_time", "")
        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            secs_remaining = max(0, int((close_time - now).total_seconds()))
        except:
            secs_remaining = -1

        # Extract prices
        yes_bid = float(m.get("yes_bid_dollars", 0) or 0)
        yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
        no_bid = float(m.get("no_bid_dollars", 0) or 0)
        no_ask = float(m.get("no_ask_dollars", 0) or 0)

        target = float(m.get("floor_strike", 0) or 0)

        # Calculate distance from target
        if target > 0 and btc_price:
            distance_pct = (btc_price - target) / target * 100
        else:
            distance_pct = None

        tick = {
            "ts": ts,
            "time_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": ticker,
            "series": ticker.split("-")[0] if "-" in ticker else ticker,
            "btc_price": btc_price,
            "target_price": target,
            "distance_pct": distance_pct,
            "secs_remaining": secs_remaining,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "yes_mid": (yes_bid + yes_ask) / 2,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "spread": yes_ask - yes_bid,
        }
        ticks.append(tick)

    return ticks


def main():
    print("=" * 60)
    print("KALSHI BTC MARKET DATA COLLECTOR (with Lag Tracking)")
    print("=" * 60)

    # Create output file
    start_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = Path(f"kalshi_data_{start_time}.jsonl")
    lag_file = Path(f"kalshi_lag_{start_time}.jsonl")

    print(f"\nOutput file: {output_file}")
    print(f"Lag events: {lag_file}")
    print(f"Move threshold: {MOVE_THRESHOLD_PCT}%")
    print("Press Ctrl+C to stop\n")

    # Track window transitions for summary
    current_windows = {}
    window_history = []
    tick_count = 0

    # Initialize lag tracker
    lag_tracker = LagTracker()

    try:
        with open(output_file, "w") as f, open(lag_file, "w") as lag_f:
            while True:
                loop_start = time.time()
                ts = time.time()

                # Get BTC price and active markets
                btc_price = get_btc_price()
                markets = get_active_markets()

                # --- LAG TRACKING ---
                # Record price for history
                lag_tracker.add_price(ts, btc_price)

                # Detect significant BTC moves
                move_event = lag_tracker.detect_move(ts, btc_price)

                if not markets:
                    print(f"{datetime.now().strftime('%H:%M:%S')} | No active markets found")
                    time.sleep(1)
                    continue

                # Collect ticks
                ticks = collect_tick(markets, btc_price)

                # Get primary market YES mid for lag tracking
                primary_yes_mid = None
                primary_target = None
                for tick in ticks:
                    if "15M" in tick["ticker"] and tick["secs_remaining"] > 30:
                        primary_yes_mid = tick["yes_mid"]
                        primary_target = tick["target_price"]
                        break

                # Start tracking new move events
                if move_event and primary_yes_mid is not None:
                    lag_tracker.start_tracking(move_event, primary_yes_mid)
                    print(f"\n    !!! BTC MOVE: {move_event['pct_change']:+.3f}% "
                          f"(${move_event['old_price']:,.0f} → ${move_event['new_price']:,.0f})")

                # Check if Kalshi caught up to any pending events
                completed = lag_tracker.check_kalshi_caught_up(ts, primary_yes_mid, btc_price, primary_target)
                for event in completed:
                    lag_f.write(json.dumps(event) + "\n")
                    if event.get("caught_up"):
                        print(f"    >>> LAG: {event['lag_secs']:.1f}s for {event['pct_change']:+.3f}% move "
                              f"(Kalshi: {event['kalshi_at_detection']*100:.1f}c → {event['kalshi_final']*100:.1f}c)")
                    else:
                        print(f"    >>> TIMEOUT: Kalshi didn't adjust after {LAG_TIMEOUT_SECS}s")
                    lag_f.flush()

                # Write to file
                for tick in ticks:
                    # Add lag tracking info to tick
                    tick["active_lag_events"] = len(lag_tracker.active_events)
                    f.write(json.dumps(tick) + "\n")
                    tick_count += 1

                # Check for window transitions
                for tick in ticks:
                    ticker = tick["ticker"]
                    secs = tick["secs_remaining"]

                    if ticker not in current_windows:
                        current_windows[ticker] = {"start_ts": tick["ts"], "start_yes": tick["yes_mid"]}
                        print(f"\n>>> NEW WINDOW: {ticker}")
                        print(f"    Target: ${tick['target_price']:,.2f}")
                        print(f"    Opening YES: {tick['yes_mid']*100:.1f}c")

                    # Window settled?
                    if secs == 0 and current_windows.get(ticker, {}).get("start_ts", 0) < tick["ts"] - 60:
                        start_data = current_windows.pop(ticker, {})
                        window_history.append({
                            "ticker": ticker,
                            "start_yes": start_data.get("start_yes", 0),
                            "end_yes": tick["yes_mid"],
                            "end_time": tick["time_utc"],
                        })
                        print(f"\n>>> WINDOW SETTLED: {ticker}")
                        print(f"    Final YES: {tick['yes_mid']*100:.1f}c")

                # Display status
                for tick in ticks:
                    if "15M" in tick["ticker"]:  # Only show 15M for brevity
                        dist = tick.get("distance_pct")
                        dist_str = f"{dist:+.3f}%" if dist is not None else "N/A"
                        direction = "↑" if dist and dist > 0 else "↓"
                        lag_indicator = f"[{len(lag_tracker.active_events)} pending]" if lag_tracker.active_events else ""
                        print(
                            f"{tick['time_utc'].split()[1]} | "
                            f"BTC: ${tick['btc_price']:,.0f} {direction}{dist_str} | "
                            f"T-{tick['secs_remaining']:4}s | "
                            f"YES: {tick['yes_bid']*100:5.1f}/{tick['yes_ask']*100:5.1f}c "
                            f"{lag_indicator}"
                        )
                        break  # Only show one 15M market per tick

                # Flush periodically
                if tick_count % 60 == 0:
                    f.flush()

                # Maintain 1-second cadence
                elapsed = time.time() - loop_start
                if elapsed < 1:
                    time.sleep(1 - elapsed)

    except KeyboardInterrupt:
        print("\n\nStopping...")

    # Summary
    print("\n" + "=" * 60)
    print("COLLECTION SUMMARY")
    print("=" * 60)
    print(f"Total ticks: {tick_count}")
    print(f"Windows observed: {len(window_history)}")
    print(f"Output file: {output_file}")
    print(f"File size: {output_file.stat().st_size / 1024:.1f} KB")

    if window_history:
        print("\nWindows captured:")
        for w in window_history[-10:]:
            print(f"  {w['ticker']}: {w['start_yes']*100:.1f}c → {w['end_yes']*100:.1f}c")

    # Lag statistics
    print("\n" + "-" * 60)
    print("LAG TRACKING SUMMARY")
    print("-" * 60)
    lag_stats = lag_tracker.get_stats()
    if lag_stats:
        print(f"Total price events detected: {lag_stats['total_events']}")
        print(f"Kalshi caught up: {lag_stats['caught_up']}")
        print(f"Timeouts (no adjustment): {lag_stats['timeouts']}")
        if lag_stats.get('avg_lag_secs'):
            print(f"Average lag: {lag_stats['avg_lag_secs']:.1f}s")
            print(f"Min lag: {lag_stats['min_lag_secs']:.1f}s")
            print(f"Max lag: {lag_stats['max_lag_secs']:.1f}s")
        print(f"\nLag events file: {lag_file}")
    else:
        print("No significant BTC moves detected during collection.")
        print(f"(Threshold: {MOVE_THRESHOLD_PCT}% move within 3 seconds)")

    print("\nShare both files for LLM analysis!")
    print("=" * 60)


if __name__ == "__main__":
    main()
