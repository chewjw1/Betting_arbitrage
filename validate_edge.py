#!/usr/bin/env python3
"""
Validate the apparent always-bet-YES edge in Kalshi 15M crypto markets.

The edge analysis showed actual UP rates of ~66-69% vs market-implied ~50%,
uniformly across momentum, hour-of-day, and distance conditions. A uniform
offset usually means measurement artifact or regime dependence, not real
conditional edge. This script tests the most likely explanations:

1. INFERENCE BIAS - outcomes were inferred from the final market mid
   (>90c = UP). Here we compute TRUE outcomes from spot vs target,
   reconstructed from distance_pct, with no market prices involved.
2. SELECTION BIAS - windows missing final order-book data were dropped
   from the edge analysis. True outcomes need only spot data, so we can
   compare the dropped windows against the counted ones.
3. TREND BIAS - per-day UP rates vs that day's BTC drift. If the "edge"
   tracks the direction of the market that day, it is the rally, not a
   mispricing, and it will reverse in a downtrend.
4. EXECUTION BIAS - edge measured against the executable ASK (what a
   taker actually pays), not the mid of a possibly 15-20c-wide book.

Usage:
    python validate_edge.py kalshi_data_*.jsonl
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

ASSETS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB', 'BCH', 'ADA', 'HYPE']


def _num(value, default=0):
    """Coerce a possibly-None numeric field."""
    return default if value is None else value


def _quoted_mid(tick):
    """Return yes_mid only when the tick carries a real quote."""
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


def load_ticks(filepath):
    ticks = []
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


def pearson(xs, ys):
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def build_windows(ticks):
    by_ticker = defaultdict(list)
    for t in ticks:
        if "15M" in t.get("ticker", ""):
            by_ticker[t.get("ticker")].append(t)

    windows = []
    for ticker, tt in by_ticker.items():
        # TRUE outcome: spot vs target from the last distance reading near expiry
        dist_ticks = [t for t in tt if t.get("distance_pct") is not None
                      and _num(t.get("secs_remaining"), 999) <= 30]
        true_outcome = None
        end_dist = None
        if dist_ticks:
            last = min(dist_ticks, key=lambda t: _num(t.get("secs_remaining"), 999))
            end_dist = last.get("distance_pct")
            if end_dist > 0:
                true_outcome = "UP"
            elif end_dist < 0:
                true_outcome = "DOWN"

        # Market-inferred outcome (same rule the analyzer uses)
        final_ticks = [t for t in tt if _num(t.get("secs_remaining"), 999) <= 5]
        final_yes = _last_quoted_mid(final_ticks)
        inferred = None
        if final_yes is not None:
            inferred = "UP" if final_yes > 0.9 else "DOWN" if final_yes < 0.1 else "UNCERTAIN"

        # Opening entry: first usable quote with 750-900s remaining
        opening = [t for t in tt if 750 < _num(t.get("secs_remaining")) < 900
                   and _quoted_mid(t) is not None]
        open_tick = opening[0] if opening else None

        # Late entry: first usable quote with 2-5 min remaining
        late = [t for t in tt if 120 < _num(t.get("secs_remaining")) < 300
                and _quoted_mid(t) is not None]
        late_tick = late[0] if late else None

        day = ""
        for t in (open_tick, tt[0] if tt else None):
            if t and t.get("time_utc"):
                day = t["time_utc"].split()[0]
                break

        windows.append({
            "ticker": ticker,
            "asset": asset_of(ticker, tt),
            "day": day,
            "true_outcome": true_outcome,
            "end_dist": end_dist,
            "inferred": inferred,
            "open_tick": open_tick,
            "late_tick": late_tick,
        })
    return windows


def section(title):
    print(f"\n{'='*70}")
    print(title)
    print(f"{'='*70}")


def up_rate(ws, key="true_outcome"):
    decided = [w for w in ws if w[key] in ("UP", "DOWN")]
    if not decided:
        return None, 0
    ups = len([w for w in decided if w[key] == "UP"])
    return ups / len(decided) * 100, len(decided)


def entry_table(windows, tick_key, label):
    print(f"\n--- {label} ---")
    print("Edge vs MID is what the analyzer reported; edge vs ASK is what a")
    print("taker buying YES could actually capture.")
    rows = defaultdict(list)
    for w in windows:
        t = w[tick_key]
        if w["true_outcome"] in ("UP", "DOWN") and t and t.get("yes_ask"):
            rows[w["asset"]].append(w)

    print(f"\n{'Asset':<8} {'N':<6} {'TrueUP%':<9} {'OpenDist%':<11} {'Mid':<7} {'Ask':<7} {'vs Mid':<9} {'vs Ask':<9}")
    print("-" * 70)
    all_ws = []
    for asset in sorted(rows.keys()):
        ws = rows[asset]
        all_ws.extend(ws)
        if len(ws) < 20:
            continue
        upr, n = up_rate(ws)
        mids = [_quoted_mid(w[tick_key]) for w in ws]
        asks = [w[tick_key]["yes_ask"] for w in ws]
        dists = [w[tick_key].get("distance_pct") for w in ws
                 if w[tick_key].get("distance_pct") is not None]
        avg_mid = sum(mids) / len(mids) * 100
        avg_ask = sum(asks) / len(asks) * 100
        avg_dist = sum(dists) / len(dists) if dists else 0
        print(f"{asset:<8} {n:<6} {upr:>6.1f}%   {avg_dist:>+8.3f}   {avg_mid:>5.1f}c  {avg_ask:>5.1f}c  "
              f"{upr - avg_mid:>+6.1f}%   {upr - avg_ask:>+6.1f}%")
    if all_ws:
        upr, n = up_rate(all_ws)
        mids = [_quoted_mid(w[tick_key]) for w in all_ws]
        asks = [w[tick_key]["yes_ask"] for w in all_ws]
        avg_mid = sum(mids) / len(mids) * 100
        avg_ask = sum(asks) / len(asks) * 100
        print("-" * 70)
        print(f"{'ALL':<8} {n:<6} {upr:>6.1f}%   {'':<11}{avg_mid:>5.1f}c  {avg_ask:>5.1f}c  "
              f"{upr - avg_mid:>+6.1f}%   {upr - avg_ask:>+6.1f}%")
    return all_ws


def main():
    if len(sys.argv) < 2:
        files = sorted(Path('.').glob('kalshi_data_*.jsonl'))
        if not files:
            print("Usage: python validate_edge.py <kalshi_data_*.jsonl>")
            return
    else:
        files = [Path(f) for f in sys.argv[1:]]

    ticks = []
    for f in files:
        if f.exists():
            ticks.extend(load_ticks(f))
    if not ticks:
        print("No data found.")
        return

    windows = build_windows(ticks)
    print(f"Loaded {len(ticks):,} ticks, {len(windows):,} 15M windows")

    # === 1. TRUE OUTCOMES vs MARKET-INFERRED OUTCOMES ===
    section("1. TRUE OUTCOMES (spot vs target) vs INFERRED (final mid >90c)")

    true_ws = [w for w in windows if w["true_outcome"] in ("UP", "DOWN")]
    true_up, n_true = up_rate(windows)
    inf_up, n_inf = up_rate(windows, key="inferred")
    print(f"\nWindows with TRUE outcome (spot data near expiry): {n_true}")
    if true_up is not None:
        print(f"  TRUE UP rate:     {true_up:.1f}%")
    print(f"Windows with INFERRED outcome (final market mid):  {n_inf}")
    if inf_up is not None:
        print(f"  INFERRED UP rate: {inf_up:.1f}%")

    both = [w for w in windows if w["true_outcome"] in ("UP", "DOWN")
            and w["inferred"] in ("UP", "DOWN")]
    if both:
        agree = len([w for w in both if w["true_outcome"] == w["inferred"]])
        inf_up_true_down = len([w for w in both if w["inferred"] == "UP" and w["true_outcome"] == "DOWN"])
        inf_down_true_up = len([w for w in both if w["inferred"] == "DOWN" and w["true_outcome"] == "UP"])
        print(f"\nWindows with both: {len(both)}")
        print(f"  Agree: {agree} ({agree/len(both)*100:.1f}%)")
        print(f"  Inferred UP but spot finished BELOW target:  {inf_up_true_down}")
        print(f"  Inferred DOWN but spot finished ABOVE target: {inf_down_true_up}")

    # Selection check: windows the edge analysis dropped (no final book data)
    dropped = [w for w in windows if w["true_outcome"] in ("UP", "DOWN") and w["inferred"] is None]
    counted = [w for w in windows if w["true_outcome"] in ("UP", "DOWN") and w["inferred"] is not None]
    d_up, d_n = up_rate(dropped)
    c_up, c_n = up_rate(counted)
    if d_up is not None and c_up is not None:
        print(f"\nSelection check (TRUE UP rate):")
        print(f"  Windows WITH final book data (counted): {c_up:.1f}% UP of {c_n}")
        print(f"  Windows WITHOUT final book data (dropped): {d_up:.1f}% UP of {d_n}")
        if abs(d_up - c_up) > 10:
            print("  >>> Dropped windows behave differently - selection bias present.")

    # === 2. TREND CHECK: per-day UP rate vs BTC drift ===
    section("2. TREND CHECK: does the edge just track market direction?")

    btc_by_day = defaultdict(list)
    for t in ticks:
        if t.get("btc_price") and t.get("time_utc"):
            btc_by_day[t["time_utc"].split()[0]].append((t.get("ts", 0), t["btc_price"]))

    day_ws = defaultdict(list)
    for w in true_ws:
        if w["day"]:
            day_ws[w["day"]].append(w)

    print(f"\n{'Day':<12} {'N':<6} {'TrueUP%':<10} {'BTC drift%':<12}")
    print("-" * 45)
    drifts, uprates = [], []
    up_days, down_days = [], []
    for day in sorted(day_ws.keys()):
        ws = day_ws[day]
        upr, n = up_rate(ws)
        if upr is None or n < 10:
            continue
        drift = None
        if day in btc_by_day and len(btc_by_day[day]) > 1:
            prices = sorted(btc_by_day[day])
            drift = (prices[-1][1] / prices[0][1] - 1) * 100
        drift_s = f"{drift:>+8.2f}" if drift is not None else "     n/a"
        print(f"{day:<12} {n:<6} {upr:>6.1f}%   {drift_s}")
        if drift is not None:
            drifts.append(drift)
            uprates.append(upr)
            (up_days if drift > 0 else down_days).append(upr)

    if up_days and down_days:
        avg_up = sum(up_days) / len(up_days)
        avg_down = sum(down_days) / len(down_days)
        print(f"\nAvg UP rate on BTC-up days ({len(up_days)}):   {avg_up:.1f}%")
        print(f"Avg UP rate on BTC-down days ({len(down_days)}): {avg_down:.1f}%")
        if avg_up - avg_down > 15:
            print(">>> UP rate strongly tracks daily direction - the 'edge' is the trend.")
        elif avg_down > 55:
            print(">>> UP bias persists even on down days - not purely trend.")
    r = pearson(drifts, uprates)
    if r is not None:
        print(f"Correlation (day drift vs day UP rate): r = {r:+.2f}")

    # === 3 & 4. EXECUTABLE EDGE ===
    section("3. EXECUTABLE EDGE AT WINDOW OPEN (750-900s remaining)")
    entry_table(windows, "open_tick", "ENTRY AT OPEN, TRUE OUTCOMES, PRICED AT THE ASK")

    section("4. EXECUTABLE EDGE AT LATE ENTRY (2-5 min remaining)")
    entry_table(windows, "late_tick", "ENTRY AT 2-5 MIN LEFT, TRUE OUTCOMES, PRICED AT THE ASK")

    print(f"\n{'='*70}")
    print("HOW TO READ THIS")
    print(f"{'='*70}")
    print("""
- If TRUE UP rate is well below the INFERRED UP rate, the analyzer's
  outcome inference was inflating the edge.
- If UP rate collapses on BTC-down days, the strategy is long the trend:
  it wins in rallies and gives it back in selloffs.
- 'vs Ask' is the honest per-trade edge before fees. If it is small or
  negative for an asset, the apparent edge lives inside the spread.
- OpenDist% shows how far spot already sits above the strike at entry.
  If it is positive and 'vs Ask' is still large, check whether the ask
  is genuinely fillable at size before believing it.
""")
    print(f"{'='*70}")
    print("COPY EVERYTHING ABOVE AND PASTE TO CLAUDE FOR ANALYSIS")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
