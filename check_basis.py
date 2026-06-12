#!/usr/bin/env python3
"""
Locate the basis between the collector's Binance prices and Kalshi's
resolution source, and re-price the residual YES edge net of it.

Motivation: validate_edge.py showed (a) spot sitting +0.07..0.10% above
the strike at window open, uniformly across six assets, during a net-down
12 days - impossible as drift, but the classic signature of a
quote-currency basis (Binance quotes in USDT; Kalshi strikes and
resolution are USD); and (b) a residual +9..14% "edge vs ask" measured
with Binance-sign outcomes.

If the basis is real, windows whose final Binance distance is between 0
and ~+0.1% actually resolve NO even though Binance says "above target".

This script:
1. Buckets windows by final Binance distance and shows the market's own
   final verdict (average final mid) per bucket. The distance at which
   the market flips from NO to YES IS the effective basis. If it flips
   at 0, there is no basis and the edge deserves another look. If it
   flips at ~+0.09%, the residual edge was the basis.
2. Recomputes UP rate and edge vs the late-entry ask for a range of
   assumed basis values, to show how much edge survives.

Usage:
    python check_basis.py kalshi_data_*.jsonl
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

ASSETS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB', 'BCH', 'ADA', 'HYPE']


def _num(value, default=0):
    return default if value is None else value


def _quoted_mid(tick):
    mid = tick.get("yes_mid")
    if mid is None:
        return None
    if tick.get("yes_bid") == 0 and tick.get("yes_ask") == 0:
        return None
    return mid


def _last_quoted_mid(ticks):
    for t in reversed(ticks):
        mid = _quoted_mid(t)
        if mid is not None:
            return mid
    return None


def load_ticks(files):
    ticks = []
    for filepath in files:
        with open(filepath) as f:
            for line in f:
                if line.strip():
                    try:
                        ticks.append(json.loads(line))
                    except:
                        pass
    return ticks


def asset_of(ticker, ticks):
    for a in ASSETS:
        if a in ticker.upper():
            return a
    for t in ticks:
        if t.get("asset"):
            return t["asset"]
    return "UNKNOWN"


def build_windows(ticks):
    by_ticker = defaultdict(list)
    for t in ticks:
        if "15M" in t.get("ticker", ""):
            by_ticker[t.get("ticker")].append(t)

    windows = []
    for ticker, tt in by_ticker.items():
        dist_ticks = [t for t in tt if t.get("distance_pct") is not None
                      and _num(t.get("secs_remaining"), 999) <= 30]
        end_dist = None
        if dist_ticks:
            last = min(dist_ticks, key=lambda t: _num(t.get("secs_remaining"), 999))
            end_dist = last.get("distance_pct")

        final_ticks = [t for t in tt if _num(t.get("secs_remaining"), 999) <= 5]
        final_mid = _last_quoted_mid(final_ticks)

        late = [t for t in tt if 120 < _num(t.get("secs_remaining")) < 300
                and _quoted_mid(t) is not None]
        late_ask = late[0].get("yes_ask") if late and late[0].get("yes_ask") else None

        # Earliest distance reading of the window = open basis estimate
        early = [t for t in tt if t.get("distance_pct") is not None
                 and _num(t.get("secs_remaining")) >= 870]
        open_dist = early[0].get("distance_pct") if early else None

        windows.append({
            "asset": asset_of(ticker, tt),
            "end_dist": end_dist,
            "final_mid": final_mid,
            "late_ask": late_ask,
            "open_dist": open_dist,
        })
    return windows


def crossover_table(ws, label):
    """Average final market verdict by final Binance distance bucket."""
    print(f"\n--- {label} ---")
    edges = [-0.30, -0.20, -0.12, -0.06, -0.03, 0.0, 0.03, 0.06, 0.09, 0.12, 0.20, 0.30]
    buckets = list(zip(edges[:-1], edges[1:]))

    print(f"{'Final dist (Binance)':<22} {'N':<6} {'Avg final mid':<15} {'Market says YES%':<16}")
    print("-" * 62)
    centers, mids = [], []
    for low, high in buckets:
        grp = [w for w in ws if low <= w["end_dist"] < high]
        if len(grp) < 10:
            continue
        avg_mid = sum(w["final_mid"] for w in grp) / len(grp)
        yes_pct = len([w for w in grp if w["final_mid"] > 0.5]) / len(grp) * 100
        print(f"{low:>+.2f}% to {high:>+.2f}%      {len(grp):<6} {avg_mid*100:>9.1f}c      {yes_pct:>8.1f}%")
        centers.append((low + high) / 2)
        mids.append(avg_mid)

    # Interpolate where the market's verdict crosses 50c
    crossover = None
    for i in range(1, len(centers)):
        if mids[i - 1] < 0.5 <= mids[i] or mids[i - 1] >= 0.5 > mids[i]:
            x0, x1, y0, y1 = centers[i - 1], centers[i], mids[i - 1], mids[i]
            if y1 != y0:
                crossover = x0 + (0.5 - y0) * (x1 - x0) / (y1 - y0)
            break
    if crossover is not None:
        print(f"\nMarket verdict crosses 50c at final distance ~{crossover:+.3f}%")
        print("=> effective basis between Binance prices and Kalshi resolution.")
    return crossover


def main():
    if len(sys.argv) < 2:
        files = sorted(Path('.').glob('kalshi_data_*.jsonl'))
        if not files:
            print("Usage: python check_basis.py <kalshi_data_*.jsonl>")
            return
    else:
        files = [Path(f) for f in sys.argv[1:] if Path(f).exists()]

    ticks = load_ticks(files)
    if not ticks:
        print("No data found.")
        return
    windows = build_windows(ticks)
    print(f"Loaded {len(ticks):,} ticks, {len(windows):,} 15M windows")

    # === 1. WHERE DOES THE MARKET FLIP FROM NO TO YES? ===
    print(f"\n{'='*70}")
    print("1. MARKET VERDICT vs FINAL BINANCE DISTANCE (crossover = basis)")
    print(f"{'='*70}")
    print("""
Caveat: only windows that kept final book data are included (these skew
UP), but the LOCATION of the flip is what matters, not the mix.""")

    usable = [w for w in windows if w["end_dist"] is not None and w["final_mid"] is not None]
    crossover_table(usable, f"ALL ASSETS ({len(usable)} windows)")

    btc = [w for w in usable if w["asset"] == "BTC"]
    if len(btc) >= 100:
        crossover_table(btc, f"BTC ONLY - tightest books, cleanest read ({len(btc)} windows)")

    # === 2. OPEN BASIS PER ASSET ===
    print(f"\n{'='*70}")
    print("2. DISTANCE AT THE VERY FIRST TICK OF EACH WINDOW (>=870s left)")
    print(f"{'='*70}")
    print("If the strike equals Kalshi's index at open, this IS the basis.")
    by_asset = defaultdict(list)
    for w in windows:
        if w["open_dist"] is not None:
            by_asset[w["asset"]].append(w["open_dist"])
    print(f"\n{'Asset':<8} {'N':<7} {'Avg first-tick dist%':<22} {'Median':<10}")
    print("-" * 50)
    for asset in sorted(by_asset.keys()):
        ds = sorted(by_asset[asset])
        if len(ds) < 20:
            continue
        print(f"{asset:<8} {len(ds):<7} {sum(ds)/len(ds):>+12.3f}          {ds[len(ds)//2]:>+8.3f}")

    # === 3. EDGE NET OF AN ASSUMED BASIS ===
    print(f"\n{'='*70}")
    print("3. LATE-ENTRY EDGE (2-5 min, at the ask) NET OF AN ASSUMED BASIS")
    print(f"{'='*70}")
    print("""
UP now means final Binance distance > basis (i.e. above the strike in
USD terms). Kalshi taker fees (~1-2c per contract near 50c) come on top
of whatever is left.""")

    tradeable = [w for w in windows if w["end_dist"] is not None and w["late_ask"]]
    if tradeable:
        avg_ask = sum(w["late_ask"] for w in tradeable) / len(tradeable) * 100
        print(f"\nWindows: {len(tradeable)}   Avg late ask: {avg_ask:.1f}c")
        print(f"\n{'Assumed basis':<15} {'UP rate':<10} {'Edge vs ask':<12}")
        print("-" * 40)
        for basis in [0.0, 0.03, 0.06, 0.09, 0.12, 0.15]:
            upr = len([w for w in tradeable if w["end_dist"] > basis]) / len(tradeable) * 100
            print(f"{basis:>+.2f}%         {upr:>6.1f}%    {upr - avg_ask:>+8.1f}%")
        print("\nRead off the row matching the crossover from section 1: that is")
        print("the honest residual edge of 'buy YES every window at 2-5 min'.")

    print(f"\n{'='*70}")
    print("COPY EVERYTHING ABOVE AND PASTE TO CLAUDE FOR ANALYSIS")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
