"""Combined strategy backtest: Late window + momentum confirmation.

Hypothesis: Late window works, but adding momentum confirmation
might increase trade frequency without hurting win rate.

Strategy variations:
1. Late window only (baseline)
2. Late window + momentum in same direction
3. Late window + fade recent momentum (contrarian)
4. Any time with strong momentum signals
"""

import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional
import math

import httpx
from scipy import stats


@dataclass
class StrategyTrade:
    """Trade result."""
    timestamp: datetime
    strategy: str
    side: str
    entry_price: float
    btc_at_entry: float
    btc_at_close: float
    target: float
    mins_left: float
    momentum_1m: float
    momentum_5m: float
    outcome: str
    pnl: float


class CombinedStrategyBacktest:
    """Test combined strategies."""

    def __init__(self, position_size: float = 50, spread: float = 0.02):
        self.position_size = position_size
        self.spread = spread

    async def fetch_data(self, days: int = 7) -> list[dict]:
        """Fetch historical data."""
        print(f"Fetching {days} days of data...")

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)

        all_klines = []
        current = start_date

        async with httpx.AsyncClient(timeout=30) as client:
            while current < end_date:
                chunk_end = min(current + timedelta(hours=16), end_date)

                resp = await client.get(
                    "https://api.binance.us/api/v3/klines",
                    params={
                        "symbol": "BTCUSDT",
                        "interval": "1m",
                        "startTime": int(current.timestamp() * 1000),
                        "endTime": int(chunk_end.timestamp() * 1000),
                        "limit": 1000,
                    }
                )

                if resp.status_code == 200:
                    for k in resp.json():
                        all_klines.append({
                            "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                        })

                current = chunk_end
                await asyncio.sleep(0.3)

        print(f"  Fetched {len(all_klines)} candles")
        return all_klines

    def build_windows(self, klines: list) -> list[dict]:
        """Build 15-minute windows with all data points."""
        windows = []

        for i in range(0, len(klines) - 15, 15):
            window_candles = klines[i:i+15]

            start_price = window_candles[0]["open"]
            end_price = window_candles[-1]["close"]
            outcome = "yes" if end_price >= start_price else "no"

            # Store snapshots at each minute
            snapshots = []
            for j, candle in enumerate(window_candles):
                # Calculate momentum at this point
                mom_1m = 0
                mom_5m = 0

                if j >= 1:
                    mom_1m = (candle["close"] - window_candles[j-1]["close"]) / window_candles[j-1]["close"]
                if j >= 5:
                    mom_5m = (candle["close"] - window_candles[j-5]["close"]) / window_candles[j-5]["close"]

                snapshots.append({
                    "minute": j,
                    "price": candle["close"],
                    "momentum_1m": mom_1m,
                    "momentum_5m": mom_5m,
                })

            windows.append({
                "start_time": window_candles[0]["time"],
                "start_price": start_price,
                "end_price": end_price,
                "outcome": outcome,
                "snapshots": snapshots,
            })

        return windows

    def run_strategy(
        self, windows: list, strategy: str, params: dict
    ) -> list[StrategyTrade]:
        """Run a specific strategy on windows."""
        trades = []

        for window in windows:
            trade = self._evaluate_window(window, strategy, params)
            if trade:
                trades.append(trade)

        return trades

    def _evaluate_window(
        self, window: dict, strategy: str, params: dict
    ) -> Optional[StrategyTrade]:
        """Evaluate window for trading opportunity."""

        entry_minute = params.get("entry_minute", 13)
        prob_threshold = params.get("prob_threshold", 0.60)
        momentum_threshold = params.get("momentum_threshold", 0.001)

        if entry_minute >= len(window["snapshots"]):
            return None

        snapshot = window["snapshots"][entry_minute]
        current_price = snapshot["price"]
        target_price = window["start_price"]
        mins_left = 15 - entry_minute
        mom_1m = snapshot["momentum_1m"]
        mom_5m = snapshot["momentum_5m"]

        # Calculate base probability
        vol_15m = 0.003
        distance_pct = (current_price - target_price) / target_price
        time_ratio = mins_left / 15.0
        expected_vol = vol_15m * math.sqrt(time_ratio)
        if expected_vol < 0.0001:
            expected_vol = 0.001

        z_score = distance_pct / expected_vol
        prob_yes = stats.norm.cdf(z_score)

        # Strategy-specific logic
        should_trade = False
        side = None

        if strategy == "late_only":
            # Original: just probability threshold
            if prob_yes > prob_threshold:
                should_trade = True
                side = "YES"
            elif prob_yes < (1 - prob_threshold):
                should_trade = True
                side = "NO"

        elif strategy == "late_plus_momentum":
            # Late window + momentum must confirm direction
            if prob_yes > prob_threshold and mom_5m > momentum_threshold:
                should_trade = True
                side = "YES"
            elif prob_yes < (1 - prob_threshold) and mom_5m < -momentum_threshold:
                should_trade = True
                side = "NO"

        elif strategy == "late_fade_momentum":
            # Late window + fade recent momentum (contrarian)
            if prob_yes > prob_threshold and mom_1m < 0:
                # Price is above target but just dipped - expect recovery
                should_trade = True
                side = "YES"
            elif prob_yes < (1 - prob_threshold) and mom_1m > 0:
                # Price is below target but just bounced - expect fade
                should_trade = True
                side = "NO"

        elif strategy == "early_strong_momentum":
            # Earlier entry (minute 10) but require very strong momentum
            if mom_5m > 0.003:  # Strong 5-min momentum up
                should_trade = True
                side = "YES"
            elif mom_5m < -0.003:  # Strong 5-min momentum down
                should_trade = True
                side = "NO"

        if not should_trade:
            return None

        # Calculate entry price and P&L
        if side == "YES":
            entry_price = prob_yes + self.spread / 2
            pnl = (1.0 if window["outcome"] == "yes" else 0.0) - entry_price
        else:
            entry_price = (1 - prob_yes) + self.spread / 2
            pnl = (1.0 if window["outcome"] == "no" else 0.0) - entry_price

        return StrategyTrade(
            timestamp=window["start_time"] + timedelta(minutes=entry_minute),
            strategy=strategy,
            side=side,
            entry_price=entry_price,
            btc_at_entry=current_price,
            btc_at_close=window["end_price"],
            target=target_price,
            mins_left=mins_left,
            momentum_1m=mom_1m,
            momentum_5m=mom_5m,
            outcome="WIN" if pnl > 0 else "LOSS",
            pnl=pnl * self.position_size,
        )


async def main():
    """Compare strategies."""
    print("=" * 70)
    print("COMBINED STRATEGY COMPARISON")
    print("=" * 70)

    bt = CombinedStrategyBacktest(position_size=50, spread=0.02)

    klines = await bt.fetch_data(days=7)
    windows = bt.build_windows(klines)
    print(f"Built {len(windows)} windows\n")

    strategies = [
        ("late_only", {"entry_minute": 13, "prob_threshold": 0.60}),
        ("late_plus_momentum", {"entry_minute": 13, "prob_threshold": 0.60, "momentum_threshold": 0.001}),
        ("late_fade_momentum", {"entry_minute": 13, "prob_threshold": 0.60}),
        ("early_strong_momentum", {"entry_minute": 10, "prob_threshold": 0.55}),
    ]

    print(f"{'Strategy':<25} {'Trades':>7} {'Wins':>6} {'Win%':>7} {'P&L':>10}")
    print("-" * 60)

    for strategy_name, params in strategies:
        trades = bt.run_strategy(windows, strategy_name, params)

        wins = sum(1 for t in trades if t.outcome == "WIN")
        total_pnl = sum(t.pnl for t in trades)
        win_rate = wins / len(trades) if trades else 0

        print(
            f"{strategy_name:<25} "
            f"{len(trades):>7} "
            f"{wins:>6} "
            f"{win_rate*100:>6.1f}% "
            f"${total_pnl:>9.2f}"
        )

    # Detailed look at best strategy
    print("\n" + "=" * 70)
    print("DETAILED: Late Window Only")
    print("=" * 70)

    trades = bt.run_strategy(windows, "late_only", {"entry_minute": 13, "prob_threshold": 0.60})

    print(f"\nLast 15 trades:")
    for t in trades[-15:]:
        print(
            f"  {t.timestamp.strftime('%m/%d %H:%M')} | "
            f"{t.side:3} @ {t.entry_price*100:.0f}c | "
            f"BTC: ${t.btc_at_entry:,.0f} vs ${t.target:,.0f} | "
            f"Mom5m: {t.momentum_5m*100:+.2f}% | "
            f"{t.outcome}: ${t.pnl:+.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
