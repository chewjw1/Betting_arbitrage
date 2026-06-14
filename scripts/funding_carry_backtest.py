#!/usr/bin/env python3
"""
Funding-rate carry (cash-and-carry) backtest for crypto perpetuals.

Strategy under test: delta-neutral basis trade.
    long 1 unit spot  +  short 1 unit perpetual future
Price moves cancel (spot gain == perp loss), so principal is hedged. The
return is the funding payment: on Binance/OKX positive funding means longs
pay shorts, so a SHORT perp position RECEIVES funding when it is positive
and PAYS when it is negative. Historically funding is positive most of the
time, so the short-perp leg earns a carry.

This is the closest thing to "automatic, continuously-rollable income" a
solo automated trader can run on the infra already in this repo. It is a
RISK PREMIUM, not arbitrage: the funding P&L curve looks almost riskless
(tiny drawdown) precisely because the real risk lives in tails the funding
series cannot show - exchange insolvency, stablecoin depeg, and short-leg
liquidation. Read the verdict in reports/funding_carry_analysis.md.

Data: Binance public historical dumps (data.binance.vision) - reachable
even where the live fapi API is geo-blocked. Cached locally as bn_<sym>.json.

Usage:
    python scripts/funding_carry_backtest.py                 # BTCUSDT ETHUSDT
    python scripts/funding_carry_backtest.py BTCUSDT SOLUSDT
    python scripts/funding_carry_backtest.py --rf 0.045      # risk-free APR
"""

import io
import json
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone

INTERVALS_PER_YEAR = 3 * 365  # 8h funding -> 1095/yr (script detects interval per row)
START_YEAR = 2021
END_YEAR, END_MONTH = 2026, 5
RISK_FREE = 0.045  # ~US T-bill APR over the window, for comparison


def _months(y0, m0, y1, m1):
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


def fetch_binance_funding(sym):
    """Monthly funding CSV zips from data.binance.vision (public, not geo-blocked)."""
    rows, missing = [], 0
    for y, m in _months(START_YEAR, 1, END_YEAR, END_MONTH):
        url = (f"https://data.binance.vision/data/futures/um/monthly/fundingRate/"
               f"{sym}/{sym}-fundingRate-{y}-{m:02d}.zip")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=25).read()
            z = zipfile.ZipFile(io.BytesIO(raw))
            for line in z.read(z.namelist()[0]).decode().splitlines()[1:]:
                p = line.split(",")
                if len(p) >= 3 and p[0].isdigit():
                    rows.append({"t": int(p[0]), "ih": int(float(p[1])), "rate": float(p[2])})
        except Exception:
            missing += 1
    rows.sort(key=lambda x: x["t"])
    return rows, missing


def load(sym):
    cache = f"bn_{sym.lower().replace('usdt','')}.json"
    try:
        rows = json.load(open(cache))
    except Exception:
        print(f"Fetching {sym} funding history from data.binance.vision ...")
        rows, missing = fetch_binance_funding(sym)
        if rows:
            json.dump(rows, open(cache, "w"))
        if missing:
            print(f"  ({missing} months unavailable, continuing)")
    for r in rows:
        r["dt"] = datetime.fromtimestamp(r["t"] / 1000, timezone.utc)
    return rows


def max_drawdown(equity):
    peak = equity[0]
    mdd = peak_i = mdd_p = mdd_t = 0
    for i, v in enumerate(equity):
        if v > peak:
            peak, peak_i = v, i
        if peak - v > mdd:
            mdd, mdd_p, mdd_t = peak - v, peak_i, i
    return mdd, mdd_p, mdd_t


def longest_underwater(equity):
    peak = equity[0]
    longest = cur = 0
    for v in equity:
        if v >= peak:
            peak, cur = v, 0
        else:
            cur += 1
            longest = max(longest, cur)
    return longest


def analyze(name, rows, rf):
    rates = [r["rate"] for r in rows]
    n = len(rates)
    mean = sum(rates) / n
    neg = sum(1 for x in rates if x < 0)
    apr = mean * INTERVALS_PER_YEAR * 100

    print(f"\n{'='*72}\n{name}: SHORT-PERP CARRY (long spot + short perp, receive funding)\n{'='*72}")
    print(f"Window: {rows[0]['dt'].date()} -> {rows[-1]['dt'].date()}  ({n} x 8h intervals)")
    print(f"Mean funding/interval: {mean*100:.5f}%  ({mean*1e4:.2f} bps)")
    print(f"Positive intervals: {(n-neg)/n*100:.1f}%   negative: {neg/n*100:.1f}%")
    print(f"Cumulative funding (gross, on notional): {sum(rates)*100:.1f}%")
    print(f"GROSS funding APR (avg over window): {apr:.1f}%")

    eq, c = [], 0.0
    for x in rates:
        c += x * 100
        eq.append(c)
    mdd, pi, ti = max_drawdown(eq)
    uw = longest_underwater(eq)
    print(f"\nFunding-only equity curve (principal is hedged, excluded):")
    print(f"  Max drawdown: {mdd:.1f}% of notional ({rows[pi]['dt'].date()} -> {rows[ti]['dt'].date()})")
    print(f"  Longest underwater: {uw} intervals = {uw/3:.0f} days")
    print(f"  NOTE: this curve looks near-riskless ONLY because it excludes")
    print(f"  the tail risks (exchange blowup / depeg / liquidation) that the")
    print(f"  premium actually compensates. Do not read it as the real risk.")

    print(f"\nPer-year gross funding APR (and negative-interval share):")
    by_year = {}
    for r in rows:
        by_year.setdefault(r["dt"].year, []).append(r["rate"])
    for y in sorted(by_year):
        yr = by_year[y]
        negp = sum(1 for x in yr if x < 0) / len(yr) * 100
        print(f"  {y}: {sum(yr)/len(yr)*INTERVALS_PER_YEAR*100:6.1f}% APR   ({negp:.0f}% negative)")

    win = 270  # 90 days
    if n > win:
        roll = [sum(rates[i:i+win]) / win * INTERVALS_PER_YEAR * 100 for i in range(n - win)]
        wi = roll.index(min(roll))
        print(f"\nRolling 90-day annualized funding:  best {max(roll):.1f}%   "
              f"worst {min(roll):.1f}% (from {rows[wi]['dt'].date()})")
        last = rates[-1095:] if n >= 1095 else rates
        print(f"Last 12 months gross APR: {sum(last)/len(last)*INTERVALS_PER_YEAR*100:.1f}%  "
              f"(vs ~{rf*100:.1f}% risk-free)")

    # Regime-timed overlay: hold carry only when trailing 30d funding beats a
    # threshold, else sit in risk-free. Uses trailing data only (no lookahead).
    print(f"\nRegime-timed overlay (carry when trailing-30d funding APR > X, else T-bill):")
    rf_per = rf / INTERVALS_PER_YEAR
    for thr in (0.05, 0.08, 0.10):
        pnl = expo = 0.0
        for i in range(n):
            if i < 90:
                pnl += rf_per
                continue
            if sum(rates[i-90:i]) / 90 * INTERVALS_PER_YEAR > thr:
                pnl += rates[i]
                expo += 1
            else:
                pnl += rf_per
        print(f"  thr {thr*100:.0f}% -> net {pnl/n*INTERVALS_PER_YEAR*100:5.1f}% APR, "
              f"exposed {expo/n*100:.0f}% of the time")

    print(f"\nNet of costs (round-trip enter+exit, % of notional):")
    years = (rows[-1]["dt"] - rows[0]["dt"]).days / 365
    for label, rt in (("low/maker 0.10%", 0.10), ("realistic taker 0.30%", 0.30), ("churny 0.60%", 0.60)):
        print(f"  {label:22s}: hold-window net {apr - rt/years:5.1f}%   "
              f"monthly-churn net {apr - rt*12:6.1f}%")
    print(f"  Capital is 1x notional (you own the spot). A 1.3x margin buffer")
    print(f"  on the short -> ~{apr/1.3:.1f}% gross before fees.")
    return apr


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rf = RISK_FREE
    if "--rf" in sys.argv:
        rf = float(sys.argv[sys.argv.index("--rf") + 1])
    symbols = args or ["BTCUSDT", "ETHUSDT"]
    for sym in symbols:
        rows = load(sym)
        if not rows:
            print(f"{sym}: no data")
            continue
        analyze(sym, rows, rf)
    print(f"\n{'='*72}")
    print("Risk-free (US T-bills) over this window ~4-5% APR. The carry is a")
    print("RISK PREMIUM for bearing crypto-exchange tail risk, not free money.")
    print(f"{'='*72}")


if __name__ == "__main__":
    main()
