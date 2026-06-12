#!/usr/bin/env python3
"""
Analyze collected Kalshi data for lag patterns and trading edges.
Run this on your collected .jsonl files.

Usage:
    python analyze_collected_data.py kalshi_data_*.jsonl
    python analyze_collected_data.py kalshi_lag_*.jsonl
"""

import json
import sys
from collections import defaultdict
from pathlib import Path


def _num(value, default=0):
    """Coerce a possibly-None numeric field (collector writes null when data is missing)."""
    return default if value is None else value


def _quoted_mid(tick):
    """Return yes_mid only when the tick carries a real quote.

    The collector logs yes_mid as null (or bid/ask as 0/0) when the order
    book is empty - those ticks have no price information.
    """
    mid = tick.get("yes_mid")
    if mid is None:
        return None
    if tick.get("yes_bid") == 0 and tick.get("yes_ask") == 0:
        return None
    return mid


def _last_quoted_mid(ticks):
    """Last usable yes_mid from a list of ticks, or None if no tick has a quote."""
    for t in reversed(ticks):
        mid = _quoted_mid(t)
        if mid is not None:
            return mid
    return None


def analyze_main_data(filepath):
    """Analyze the main kalshi_data file."""
    print(f"\n{'='*70}")
    print(f"ANALYZING: {filepath}")
    print(f"{'='*70}")

    ticks = []
    with open(filepath) as f:
        for line in f:
            if line.strip():
                try:
                    ticks.append(json.loads(line))
                except:
                    pass

    if not ticks:
        print("No data found.")
        return

    print(f"\nTotal ticks: {len(ticks):,}")
    print(f"Time range: {ticks[0].get('time_utc', 'N/A')} to {ticks[-1].get('time_utc', 'N/A')}")

    # Group by ticker
    by_ticker = defaultdict(list)
    for t in ticks:
        by_ticker[t.get('ticker', 'unknown')].append(t)

    print(f"Unique markets: {len(by_ticker)}")

    # Analyze 15M markets specifically
    markets_15m = {k: v for k, v in by_ticker.items() if '15M' in k}
    print(f"15M markets: {len(markets_15m)}")

    # Group by asset
    by_asset = defaultdict(list)
    for t in ticks:
        asset = t.get('asset', 'BTC')  # Default to BTC for old data
        if asset:
            by_asset[asset].append(t)

    if len(by_asset) > 1:
        print(f"\nAssets tracked: {', '.join(sorted(by_asset.keys()))}")

    # Price movement analysis
    btc_prices = [t['btc_price'] for t in ticks if t.get('btc_price')]
    if btc_prices:
        print(f"\nBTC Price Range:")
        print(f"  Min: ${min(btc_prices):,.2f}")
        print(f"  Max: ${max(btc_prices):,.2f}")
        print(f"  Range: ${max(btc_prices) - min(btc_prices):,.2f} ({(max(btc_prices)/min(btc_prices)-1)*100:.2f}%)")

    # YES price distribution at different time buckets
    print(f"\n{'='*70}")
    print("YES PRICE BY TIME REMAINING (15M markets only)")
    print(f"{'='*70}")

    time_buckets = {
        "0-30s": (0, 30),
        "30-60s": (30, 60),
        "1-2min": (60, 120),
        "2-5min": (120, 300),
        "5-10min": (300, 600),
        "10-15min": (600, 900),
    }

    for bucket_name, (low, high) in time_buckets.items():
        bucket_ticks = []
        for ticker, ticker_ticks in markets_15m.items():
            for t in ticker_ticks:
                secs = t.get('secs_remaining')
                if secs is not None and low <= secs < high and t.get('yes_mid'):
                    bucket_ticks.append(t)

        if bucket_ticks:
            yes_mids = [t['yes_mid'] for t in bucket_ticks]
            spreads = [_num(t.get('spread')) for t in bucket_ticks]
            distances = [t.get('distance_pct') for t in bucket_ticks if t.get('distance_pct') is not None]

            print(f"\n{bucket_name} ({len(bucket_ticks):,} ticks):")
            print(f"  YES mid: {min(yes_mids)*100:.1f}c - {max(yes_mids)*100:.1f}c (avg: {sum(yes_mids)/len(yes_mids)*100:.1f}c)")
            print(f"  Spread:  {sum(spreads)/len(spreads)*100:.2f}c avg")
            if distances:
                print(f"  Distance from target: {sum(distances)/len(distances):.3f}% avg")

    # Window outcomes (infer from YES prices going to extremes)
    print(f"\n{'='*70}")
    print("WINDOW OUTCOMES (inferred from final prices)")
    print(f"{'='*70}")

    outcomes = {"UP": 0, "DOWN": 0, "UNCERTAIN": 0}
    for ticker, ticker_ticks in markets_15m.items():
        final_ticks = [t for t in ticker_ticks if _num(t.get('secs_remaining'), 999) <= 5]
        final_yes = _last_quoted_mid(final_ticks)
        if final_yes is not None:
            if final_yes > 0.9:
                outcomes["UP"] += 1
            elif final_yes < 0.1:
                outcomes["DOWN"] += 1
            else:
                outcomes["UNCERTAIN"] += 1

    total_windows = sum(outcomes.values())
    if total_windows > 0:
        print(f"\nTotal windows with final data: {total_windows}")
        print(f"  UP (YES > 90c):   {outcomes['UP']} ({outcomes['UP']/total_windows*100:.1f}%)")
        print(f"  DOWN (YES < 10c): {outcomes['DOWN']} ({outcomes['DOWN']/total_windows*100:.1f}%)")
        print(f"  UNCERTAIN:        {outcomes['UNCERTAIN']} ({outcomes['UNCERTAIN']/total_windows*100:.1f}%)")

    # Outcomes by asset
    print(f"\n{'='*70}")
    print("OUTCOMES BY ASSET")
    print(f"{'='*70}")

    asset_outcomes = defaultdict(lambda: {"UP": 0, "DOWN": 0, "UNCERTAIN": 0, "spreads": []})
    for ticker, ticker_ticks in markets_15m.items():
        # Get asset from ticker
        asset = None
        for a in ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB', 'BCH', 'ADA', 'HYPE']:
            if a in ticker.upper():
                asset = a
                break
        if not asset:
            asset = ticker_ticks[0].get('asset', 'UNKNOWN') if ticker_ticks else 'UNKNOWN'

        final_ticks = [t for t in ticker_ticks if _num(t.get('secs_remaining'), 999) <= 5]
        final_yes = _last_quoted_mid(final_ticks)
        if final_yes is not None:
            if final_yes > 0.9:
                asset_outcomes[asset]["UP"] += 1
            elif final_yes < 0.1:
                asset_outcomes[asset]["DOWN"] += 1
            else:
                asset_outcomes[asset]["UNCERTAIN"] += 1

        # Track average spread
        spreads = [t.get('spread', 0) for t in ticker_ticks if t.get('spread')]
        if spreads:
            asset_outcomes[asset]["spreads"].extend(spreads)

    print(f"\n{'Asset':<8} {'Windows':<10} {'UP':<12} {'DOWN':<12} {'Avg Spread':<12}")
    print("-" * 60)
    for asset in sorted(asset_outcomes.keys()):
        data = asset_outcomes[asset]
        total = data["UP"] + data["DOWN"] + data["UNCERTAIN"]
        if total > 0:
            up_pct = data["UP"] / total * 100
            down_pct = data["DOWN"] / total * 100
            avg_spread = sum(data["spreads"]) / len(data["spreads"]) * 100 if data["spreads"] else 0
            print(f"{asset:<8} {total:<10} {data['UP']:>3} ({up_pct:4.0f}%)   {data['DOWN']:>3} ({down_pct:4.0f}%)   {avg_spread:>6.2f}c")

    # Spread analysis
    print(f"\n{'='*70}")
    print("SPREAD ANALYSIS")
    print(f"{'='*70}")

    all_spreads = [t.get('spread', 0) for t in ticks if t.get('spread') is not None]
    if all_spreads:
        print(f"\nOverall spread:")
        print(f"  Min: {min(all_spreads)*100:.2f}c")
        print(f"  Max: {max(all_spreads)*100:.2f}c")
        print(f"  Avg: {sum(all_spreads)/len(all_spreads)*100:.2f}c")

        # Spread by distance from target
        close_spreads = [_num(t.get('spread')) for t in ticks
                        if t.get('distance_pct') is not None
                        and abs(t.get('distance_pct', 999)) < 0.05]
        far_spreads = [_num(t.get('spread')) for t in ticks
                      if t.get('distance_pct') is not None
                      and abs(t.get('distance_pct', 0)) >= 0.1]

        if close_spreads:
            print(f"\nWhen close to target (<0.05% distance):")
            print(f"  Avg spread: {sum(close_spreads)/len(close_spreads)*100:.2f}c")
        if far_spreads:
            print(f"\nWhen far from target (>0.1% distance):")
            print(f"  Avg spread: {sum(far_spreads)/len(far_spreads)*100:.2f}c")


def analyze_edge_opportunities(filepath):
    """Find systematic mispricings - where actual outcomes differ from market price."""
    print(f"\n{'='*70}")
    print("EDGE ANALYSIS: Where Does Market Misprice?")
    print(f"{'='*70}")

    ticks = []
    with open(filepath) as f:
        for line in f:
            if line.strip():
                try:
                    ticks.append(json.loads(line))
                except:
                    pass

    # Build window outcomes with opening data
    by_ticker = defaultdict(list)
    for t in ticks:
        if "15M" in t.get("ticker", ""):
            by_ticker[t.get("ticker")].append(t)

    windows = []
    for ticker, ticker_ticks in by_ticker.items():
        # Get opening tick (800-900s remaining) - prefer one with momentum signals
        opening = [t for t in ticker_ticks if 750 < _num(t.get("secs_remaining")) < 900
                   and _quoted_mid(t) is not None and t.get("momentum_1m") is not None]
        if not opening:
            opening = [t for t in ticker_ticks if 750 < _num(t.get("secs_remaining")) < 900
                       and _quoted_mid(t) is not None]
        # Get closing tick
        closing = [t for t in ticker_ticks if _num(t.get("secs_remaining"), 999) <= 5]

        if opening and closing:
            open_tick = opening[0]
            final_yes = _last_quoted_mid(closing)
            if final_yes is None:
                continue

            outcome = "UP" if final_yes > 0.9 else "DOWN" if final_yes < 0.1 else None
            if outcome is None:
                continue

            # Extract hour from UTC time
            time_str = open_tick.get("time_utc", "")
            try:
                hour = int(time_str.split()[1].split(":")[0])
            except:
                hour = -1

            # Get asset
            asset = open_tick.get("asset")
            if not asset:
                for a in ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB', 'BCH', 'ADA', 'HYPE']:
                    if a in ticker.upper():
                        asset = a
                        break

            windows.append({
                "ticker": ticker,
                "asset": asset or "UNKNOWN",
                "outcome": outcome,
                "outcome_binary": 1 if outcome == "UP" else 0,
                "open_yes": _quoted_mid(open_tick),
                "hour_utc": hour,
                "distance_pct": open_tick.get("distance_pct"),
                "momentum_1m": open_tick.get("momentum_1m"),
                "momentum_5m": open_tick.get("momentum_5m"),
                "volatility_5m": open_tick.get("volatility_5m"),
                "spread": _num(open_tick.get("spread")),
            })

    if len(windows) < 5:
        print(f"\nOnly {len(windows)} windows - need more data.")
        return

    print(f"\nTotal windows analyzed: {len(windows)}")

    # === 1. MARKET EFFICIENCY CHECK ===
    print(f"\n--- MARKET EFFICIENCY CHECK ---")
    print("Does market price = actual probability?")

    # Bucket by opening YES price
    buckets = [
        ("YES 0-20c (market says DOWN)", 0, 0.2),
        ("YES 20-40c", 0.2, 0.4),
        ("YES 40-60c (coin flip)", 0.4, 0.6),
        ("YES 60-80c", 0.6, 0.8),
        ("YES 80-100c (market says UP)", 0.8, 1.0),
    ]

    print(f"\n{'Price Bucket':<35} {'N':<5} {'Actual UP%':<12} {'Market%':<12} {'Edge':<10}")
    print("-" * 75)

    for label, low, high in buckets:
        bucket_windows = [w for w in windows if low <= w["open_yes"] < high]
        if len(bucket_windows) >= 2:
            actual_up = sum(w["outcome_binary"] for w in bucket_windows) / len(bucket_windows) * 100
            market_implied = sum(w["open_yes"] for w in bucket_windows) / len(bucket_windows) * 100
            edge = actual_up - market_implied
            flag = "***" if abs(edge) > 15 else ""
            print(f"{label:<35} {len(bucket_windows):<5} {actual_up:>8.1f}%    {market_implied:>8.1f}%    {edge:>+7.1f}% {flag}")

    # === 2. ASSET EFFICIENCY ===
    print(f"\n--- ASSET EFFICIENCY ---")
    print("Which coins have sloppier markets?")

    asset_data = defaultdict(list)
    for w in windows:
        asset_data[w["asset"]].append(w)

    print(f"\n{'Asset':<8} {'N':<5} {'Actual UP%':<12} {'Avg Spread':<12} {'Efficiency':<15}")
    print("-" * 60)

    for asset in sorted(asset_data.keys()):
        aws = asset_data[asset]
        if len(aws) >= 2:
            actual_up = sum(w["outcome_binary"] for w in aws) / len(aws) * 100
            avg_spread = sum(w["spread"] for w in aws) / len(aws) * 100
            # Efficiency = how close is actual to market implied
            market_avg = sum(w["open_yes"] for w in aws) / len(aws) * 100
            efficiency_error = abs(actual_up - market_avg)
            efficiency = "EFFICIENT" if efficiency_error < 10 else "INEFFICIENT ***"
            print(f"{asset:<8} {len(aws):<5} {actual_up:>8.1f}%    {avg_spread:>8.2f}c    {efficiency}")

    # === 3. TIME OF DAY EFFECT ===
    print(f"\n--- TIME OF DAY EFFECT (UTC) ---")
    print("Are certain hours less efficient?")

    hour_data = defaultdict(list)
    for w in windows:
        if w["hour_utc"] >= 0:
            hour_data[w["hour_utc"]].append(w)

    print(f"\n{'Hour':<8} {'N':<5} {'Actual UP%':<12} {'Market%':<12} {'Edge':<10}")
    print("-" * 55)

    for hour in sorted(hour_data.keys()):
        hws = hour_data[hour]
        if len(hws) >= 2:
            actual_up = sum(w["outcome_binary"] for w in hws) / len(hws) * 100
            market_implied = sum(w["open_yes"] for w in hws) / len(hws) * 100
            edge = actual_up - market_implied
            flag = "***" if abs(edge) > 15 else ""
            print(f"{hour:02d}:00    {len(hws):<5} {actual_up:>8.1f}%    {market_implied:>8.1f}%    {edge:>+7.1f}% {flag}")

    # === 4. LATE GAME ANALYSIS ===
    print(f"\n--- LATE GAME ANALYSIS ---")
    print("When market shows >80% confidence, does it hold?")

    high_confidence = [w for w in windows if w["open_yes"] > 0.8 or w["open_yes"] < 0.2]
    if high_confidence:
        # For YES > 80%, check if UP actually happens
        yes_high = [w for w in windows if w["open_yes"] > 0.8]
        yes_low = [w for w in windows if w["open_yes"] < 0.2]

        if yes_high:
            hit_rate = sum(w["outcome_binary"] for w in yes_high) / len(yes_high) * 100
            avg_price = sum(w["open_yes"] for w in yes_high) / len(yes_high) * 100
            print(f"\nYES > 80c (expecting UP): {len(yes_high)} windows")
            print(f"  Actual UP rate: {hit_rate:.1f}%")
            print(f"  Average price paid: {avg_price:.1f}c")
            print(f"  Edge: {hit_rate - avg_price:+.1f}%")
            if hit_rate > avg_price + 5:
                print(f"  >>> POTENTIAL EDGE: Market underprices high-confidence UP")
            elif hit_rate < avg_price - 5:
                print(f"  >>> MARKET OVERCONFIDENT: Bet NO when YES > 80c")

        if yes_low:
            hit_rate = sum(1 - w["outcome_binary"] for w in yes_low) / len(yes_low) * 100
            avg_price = sum(1 - w["open_yes"] for w in yes_low) / len(yes_low) * 100
            print(f"\nYES < 20c (expecting DOWN): {len(yes_low)} windows")
            print(f"  Actual DOWN rate: {hit_rate:.1f}%")
            print(f"  Average NO price: {avg_price:.1f}c")
            print(f"  Edge: {hit_rate - avg_price:+.1f}%")

    # === 5. DISTANCE FROM TARGET ===
    print(f"\n--- DISTANCE FROM TARGET ---")
    print("When spot is far from target, is market right?")

    dist_windows = [w for w in windows if w["distance_pct"] is not None]
    if dist_windows:
        far_above = [w for w in dist_windows if w["distance_pct"] > 0.15]
        far_below = [w for w in dist_windows if w["distance_pct"] < -0.15]
        close = [w for w in dist_windows if abs(w["distance_pct"]) <= 0.05]

        for label, group in [("Far ABOVE target (>+0.15%)", far_above),
                             ("Far BELOW target (<-0.15%)", far_below),
                             ("CLOSE to target (<0.05%)", close)]:
            if len(group) >= 2:
                actual_up = sum(w["outcome_binary"] for w in group) / len(group) * 100
                market = sum(w["open_yes"] for w in group) / len(group) * 100
                edge = actual_up - market
                print(f"\n{label}: {len(group)} windows")
                print(f"  Actual UP: {actual_up:.1f}% | Market: {market:.1f}% | Edge: {edge:+.1f}%")

    # === 6. ACTIONABLE SUMMARY ===
    print(f"\n{'='*70}")
    print("ACTIONABLE SUMMARY")
    print(f"{'='*70}")

    edges_found = []

    # Check each condition for significant edge
    for asset, aws in asset_data.items():
        if len(aws) >= 3:
            actual = sum(w["outcome_binary"] for w in aws) / len(aws) * 100
            market = sum(w["open_yes"] for w in aws) / len(aws) * 100
            if abs(actual - market) > 15:
                edges_found.append(f"{asset}: Actual {actual:.0f}% vs Market {market:.0f}% ({actual-market:+.0f}% edge)")

    for hour, hws in hour_data.items():
        if len(hws) >= 3:
            actual = sum(w["outcome_binary"] for w in hws) / len(hws) * 100
            market = sum(w["open_yes"] for w in hws) / len(hws) * 100
            if abs(actual - market) > 20:
                edges_found.append(f"Hour {hour:02d}:00 UTC: Actual {actual:.0f}% vs Market {market:.0f}% ({actual-market:+.0f}% edge)")

    if edges_found:
        print("\nPOTENTIAL EDGES DETECTED:")
        for e in edges_found:
            print(f"  - {e}")
        print("\n⚠️  WARNING: Small sample sizes. Need 50+ windows per condition for confidence.")
    else:
        print("\nNo significant edges found. Market appears efficient.")
        print("Keep collecting data or try different conditions.")


def analyze_signals(filepath):
    """Analyze momentum/volatility signals vs outcomes."""
    print(f"\n{'='*70}")
    print("SIGNAL ANALYSIS (Momentum/Volatility vs Outcomes)")
    print(f"{'='*70}")

    ticks = []
    with open(filepath) as f:
        for line in f:
            if line.strip():
                try:
                    ticks.append(json.loads(line))
                except:
                    pass

    # Check if signals exist (check multiple ticks since first few won't have signals)
    has_signals = False
    for t in ticks[100:200]:  # Check ticks after warmup period
        if "momentum_1m" in t:
            has_signals = True
            break

    if not has_signals:
        print("\nNo momentum/volatility signals in this data.")
        print("Run updated collector to capture these signals.")
        return

    # Group by ticker to find window outcomes
    by_ticker = defaultdict(list)
    for t in ticks:
        if "15M" in t.get("ticker", ""):
            by_ticker[t.get("ticker")].append(t)

    # For each window, get opening signals and final outcome
    window_data = []
    for ticker, ticker_ticks in by_ticker.items():
        # Get opening tick (around 800-900 seconds remaining)
        opening = [t for t in ticker_ticks if 750 < _num(t.get("secs_remaining")) < 900
                   and _quoted_mid(t) is not None]
        # Get closing tick (0-10 seconds remaining)
        closing = [t for t in ticker_ticks if _num(t.get("secs_remaining"), 999) <= 10]

        if opening and closing:
            open_tick = opening[0]
            final_yes = _last_quoted_mid(closing)
            if final_yes is None:
                continue

            outcome = "UP" if final_yes > 0.9 else "DOWN" if final_yes < 0.1 else "UNCERTAIN"
            if outcome == "UNCERTAIN":
                continue

            window_data.append({
                "ticker": ticker,
                "outcome": outcome,
                "open_yes": _quoted_mid(open_tick),
                "momentum_1m": _num(open_tick.get("momentum_1m")),
                "momentum_5m": _num(open_tick.get("momentum_5m")),
                "volatility_1m": _num(open_tick.get("volatility_1m")),
                "volatility_5m": _num(open_tick.get("volatility_5m")),
                "distance_pct": _num(open_tick.get("distance_pct")),
            })

    if not window_data:
        print("\nNot enough window data with signals.")
        return

    print(f"\nWindows with signal data: {len(window_data)}")

    # Analyze by momentum
    print(f"\n--- BY 1-MINUTE MOMENTUM ---")
    bullish = [w for w in window_data if w["momentum_1m"] > 0.02]
    bearish = [w for w in window_data if w["momentum_1m"] < -0.02]
    neutral = [w for w in window_data if -0.02 <= w["momentum_1m"] <= 0.02]

    for label, group in [("Bullish (>+0.02%)", bullish), ("Bearish (<-0.02%)", bearish), ("Neutral", neutral)]:
        if group:
            up_rate = len([w for w in group if w["outcome"] == "UP"]) / len(group) * 100
            avg_yes = sum(w["open_yes"] for w in group) / len(group) * 100
            edge = up_rate - avg_yes
            print(f"\n{label}: {len(group)} windows")
            print(f"  Actual UP rate: {up_rate:.1f}%")
            print(f"  Market implied (avg YES): {avg_yes:.1f}%")
            print(f"  Edge: {edge:+.1f}%")
            if abs(edge) > 10:
                print(f"  >>> POTENTIAL EDGE: Bet {'YES' if edge > 0 else 'NO'}")

    # Analyze by volatility
    print(f"\n--- BY 5-MINUTE VOLATILITY ---")
    high_vol = [w for w in window_data if w["volatility_5m"] > 0.1]
    low_vol = [w for w in window_data if w["volatility_5m"] <= 0.1]

    for label, group in [("High volatility (>0.1%)", high_vol), ("Low volatility (<=0.1%)", low_vol)]:
        if group:
            up_rate = len([w for w in group if w["outcome"] == "UP"]) / len(group) * 100
            avg_yes = sum(w["open_yes"] for w in group) / len(group) * 100
            edge = up_rate - avg_yes
            print(f"\n{label}: {len(group)} windows")
            print(f"  Actual UP rate: {up_rate:.1f}%")
            print(f"  Market implied: {avg_yes:.1f}%")
            print(f"  Edge: {edge:+.1f}%")

    # Analyze by distance from target
    print(f"\n--- BY DISTANCE FROM TARGET ---")
    far_above = [w for w in window_data if w["distance_pct"] and w["distance_pct"] > 0.1]
    far_below = [w for w in window_data if w["distance_pct"] and w["distance_pct"] < -0.1]
    close = [w for w in window_data if w["distance_pct"] and abs(w["distance_pct"]) <= 0.1]

    for label, group in [("Far above target (>+0.1%)", far_above), ("Far below target (<-0.1%)", far_below), ("Close to target", close)]:
        if group:
            up_rate = len([w for w in group if w["outcome"] == "UP"]) / len(group) * 100
            avg_yes = sum(w["open_yes"] for w in group) / len(group) * 100
            edge = up_rate - avg_yes
            print(f"\n{label}: {len(group)} windows")
            print(f"  Actual UP rate: {up_rate:.1f}%")
            print(f"  Market implied: {avg_yes:.1f}%")
            print(f"  Edge: {edge:+.1f}%")

    # Combined signals
    print(f"\n--- COMBINED SIGNALS ---")
    # Bullish momentum + above target = strong YES?
    strong_yes = [w for w in window_data if w["momentum_1m"] > 0.02 and w.get("distance_pct", 0) > 0.05]
    strong_no = [w for w in window_data if w["momentum_1m"] < -0.02 and w.get("distance_pct", 0) < -0.05]

    if strong_yes:
        up_rate = len([w for w in strong_yes if w["outcome"] == "UP"]) / len(strong_yes) * 100
        avg_yes = sum(w["open_yes"] for w in strong_yes) / len(strong_yes) * 100
        print(f"\nBullish momentum + above target: {len(strong_yes)} windows")
        print(f"  UP rate: {up_rate:.1f}% vs market {avg_yes:.1f}% (edge: {up_rate-avg_yes:+.1f}%)")

    if strong_no:
        down_rate = len([w for w in strong_no if w["outcome"] == "DOWN"]) / len(strong_no) * 100
        avg_no = (1 - sum(w["open_yes"] for w in strong_no) / len(strong_no)) * 100
        print(f"\nBearish momentum + below target: {len(strong_no)} windows")
        print(f"  DOWN rate: {down_rate:.1f}% vs market {avg_no:.1f}% (edge: {down_rate-avg_no:+.1f}%)")


def analyze_lag_data(filepath):
    """Analyze the lag events file."""
    print(f"\n{'='*70}")
    print(f"LAG ANALYSIS: {filepath}")
    print(f"{'='*70}")

    events = []
    with open(filepath) as f:
        for line in f:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except:
                    pass

    if not events:
        print("No lag events recorded.")
        print("(BTC may not have moved >0.03% within 3-second windows)")
        return

    print(f"\nTotal lag events: {len(events)}")

    caught_up = [e for e in events if e.get('caught_up')]
    timeouts = [e for e in events if e.get('timeout')]

    print(f"Kalshi caught up: {len(caught_up)} ({len(caught_up)/len(events)*100:.1f}%)")
    print(f"Timeouts: {len(timeouts)} ({len(timeouts)/len(events)*100:.1f}%)")

    if caught_up:
        lags = [e['lag_secs'] for e in caught_up]
        print(f"\nLag when Kalshi DID adjust:")
        print(f"  Min: {min(lags):.1f}s")
        print(f"  Max: {max(lags):.1f}s")
        print(f"  Avg: {sum(lags)/len(lags):.1f}s")
        print(f"  Median: {sorted(lags)[len(lags)//2]:.1f}s")

        # Distribution
        print(f"\nLag distribution:")
        for threshold in [1, 2, 3, 5, 7, 10]:
            count = len([l for l in lags if l <= threshold])
            print(f"  <= {threshold}s: {count} ({count/len(lags)*100:.1f}%)")

        # By move size
        print(f"\nLag by BTC move size:")
        small_moves = [e for e in caught_up if abs(e.get('pct_change', 0)) < 0.05]
        large_moves = [e for e in caught_up if abs(e.get('pct_change', 0)) >= 0.05]

        if small_moves:
            small_lags = [e['lag_secs'] for e in small_moves]
            print(f"  Small moves (<0.05%): {sum(small_lags)/len(small_lags):.1f}s avg lag")
        if large_moves:
            large_lags = [e['lag_secs'] for e in large_moves]
            print(f"  Large moves (>=0.05%): {sum(large_lags)/len(large_lags):.1f}s avg lag")

        # Kalshi price change magnitude
        print(f"\nKalshi price adjustment magnitude:")
        changes = [abs(e.get('kalshi_change', 0)) for e in caught_up]
        print(f"  Min: {min(changes)*100:.1f}c")
        print(f"  Max: {max(changes)*100:.1f}c")
        print(f"  Avg: {sum(changes)/len(changes)*100:.1f}c")


def main():
    if len(sys.argv) < 2:
        # Try to find files in current directory
        data_files = list(Path('.').glob('kalshi_data_*.jsonl'))
        lag_files = list(Path('.').glob('kalshi_lag_*.jsonl'))

        if not data_files and not lag_files:
            print("Usage: python analyze_collected_data.py <file.jsonl>")
            print("\nNo kalshi_data_*.jsonl or kalshi_lag_*.jsonl files found in current directory.")
            return

        files = data_files + lag_files
    else:
        files = [Path(f) for f in sys.argv[1:]]

    for filepath in files:
        if not filepath.exists():
            print(f"File not found: {filepath}")
            continue

        if 'lag' in filepath.name:
            analyze_lag_data(filepath)
        else:
            analyze_main_data(filepath)
            analyze_edge_opportunities(filepath)  # Core edge analysis
            analyze_signals(filepath)  # Momentum/volatility signals

    print(f"\n{'='*70}")
    print("COPY EVERYTHING ABOVE AND PASTE TO CLAUDE FOR ANALYSIS")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
