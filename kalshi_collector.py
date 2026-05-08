#!/usr/bin/env python3
"""
Kalshi BTC Market Data Collector
================================
Collects second-by-second bid/ask data for BTC prediction markets.

Usage (PowerShell):
    python kalshi_collector.py

Output: kalshi_data_YYYYMMDD_HHMMSS.jsonl (one JSON object per line)

Share the output file for LLM analysis of patterns.
"""

import json
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

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


def collect_tick(markets):
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

        tick = {
            "ts": ts,
            "time_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": ticker,
            "series": ticker.split("-")[0] if "-" in ticker else ticker,
            "target_price": target,
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
    print("KALSHI BTC MARKET DATA COLLECTOR")
    print("=" * 60)

    # Create output file
    start_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = Path(f"kalshi_data_{start_time}.jsonl")

    print(f"\nOutput file: {output_file}")
    print("Press Ctrl+C to stop\n")

    # Track window transitions for summary
    current_windows = {}
    window_history = []
    tick_count = 0

    try:
        with open(output_file, "w") as f:
            while True:
                loop_start = time.time()

                # Get active markets
                markets = get_active_markets()

                if not markets:
                    print(f"{datetime.now().strftime('%H:%M:%S')} | No active markets found")
                    time.sleep(1)
                    continue

                # Collect ticks
                ticks = collect_tick(markets)

                # Write to file
                for tick in ticks:
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
                        direction = "↑" if tick["yes_mid"] > 0.5 else "↓"
                        print(
                            f"{tick['time_utc'].split()[1]} | "
                            f"{tick['ticker'][-15:]} | "
                            f"T-{tick['secs_remaining']:4}s | "
                            f"YES: {tick['yes_bid']*100:5.1f}/{tick['yes_ask']*100:5.1f}c | "
                            f"Spread: {tick['spread']*100:.1f}c"
                        )

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

    print("\nShare this file for LLM analysis!")
    print("=" * 60)


if __name__ == "__main__":
    main()
