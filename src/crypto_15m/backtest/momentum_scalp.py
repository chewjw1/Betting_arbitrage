"""Momentum scalping backtest for 15-minute crypto contracts.

Strategy: Detect momentum bursts in BTC, enter position, exit when
contract reprices. NOT holding to expiration.

Key insight: When BTC moves $100 in 30 seconds, the YES/NO contract
should reprice. If we detect the move early, we capture that repricing.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
import math

import httpx
from scipy import stats
import structlog

logger = structlog.get_logger()


@dataclass
class MomentumTrade:
    """A momentum scalp trade."""
    entry_time: datetime
    exit_time: datetime
    side: str  # YES or NO
    entry_price: float  # Contract price at entry
    exit_price: float   # Contract price at exit
    btc_entry: float
    btc_exit: float
    btc_move_pct: float
    hold_seconds: int
    pnl_per_contract: float
    outcome: str  # WIN or LOSS


@dataclass
class MomentumSignal:
    """Detected momentum signal."""
    timestamp: datetime
    direction: str  # UP or DOWN
    strength: float  # 0-1
    btc_price: float
    move_30s: float  # BTC % move in last 30 seconds
    move_60s: float  # BTC % move in last 60 seconds


class MomentumScalpBacktest:
    """Backtest momentum scalping strategy."""

    def __init__(
        self,
        min_move_trigger: float = 0.0015,  # 0.15% BTC move to trigger
        hold_seconds: int = 60,  # How long to hold
        position_size: float = 50,
        spread: float = 0.02,  # 2 cent spread assumption
    ):
        self.min_move_trigger = min_move_trigger
        self.hold_seconds = hold_seconds
        self.position_size = position_size
        self.spread = spread

    async def run_backtest(self, days: int = 7) -> dict:
        """Run momentum scalping backtest."""

        print(f"Fetching {days} days of 1-second BTC data (using 1m candles)...")

        # Fetch 1-minute candles
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
                            "volume": float(k[5]),
                        })

                current = chunk_end
                await asyncio.sleep(0.3)

        print(f"  Fetched {len(all_klines)} candles")

        # Detect momentum signals and simulate trades
        trades = []
        signals_detected = 0

        for i in range(60, len(all_klines) - 2):  # Need history and future
            # Calculate momentum
            current_price = all_klines[i]["close"]
            price_30s_ago = all_klines[i-1]["close"]  # 1 min ago (closest we have)
            price_60s_ago = all_klines[i-2]["close"]  # 2 min ago

            move_30s = (current_price - price_30s_ago) / price_30s_ago
            move_60s = (current_price - price_60s_ago) / price_60s_ago

            # Detect strong momentum
            if abs(move_30s) >= self.min_move_trigger:
                signals_detected += 1

                direction = "UP" if move_30s > 0 else "DOWN"

                # Simulate the trade
                trade = self._simulate_momentum_trade(
                    all_klines, i, direction, current_price, move_30s
                )
                if trade:
                    trades.append(trade)

        # Calculate results
        wins = sum(1 for t in trades if t.outcome == "WIN")
        losses = sum(1 for t in trades if t.outcome == "LOSS")
        total_pnl = sum(t.pnl_per_contract * self.position_size for t in trades)

        win_rate = wins / len(trades) if trades else 0
        avg_pnl = total_pnl / len(trades) if trades else 0

        # Analyze by move size
        small_moves = [t for t in trades if abs(t.btc_move_pct) < 0.002]
        large_moves = [t for t in trades if abs(t.btc_move_pct) >= 0.002]

        return {
            "days": days,
            "signals_detected": signals_detected,
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_pnl_per_trade": avg_pnl,
            "small_move_trades": len(small_moves),
            "small_move_winrate": sum(1 for t in small_moves if t.outcome == "WIN") / len(small_moves) if small_moves else 0,
            "large_move_trades": len(large_moves),
            "large_move_winrate": sum(1 for t in large_moves if t.outcome == "WIN") / len(large_moves) if large_moves else 0,
            "trade_details": trades[-20:],  # Last 20 trades
        }

    def _simulate_momentum_trade(
        self, klines: list, entry_idx: int, direction: str,
        btc_entry: float, trigger_move: float
    ) -> Optional[MomentumTrade]:
        """Simulate a momentum scalp trade."""

        entry_time = klines[entry_idx]["time"]

        # Calculate contract price at entry
        # Assume we're in a 15-min window, estimate fair value
        # For simplicity, assume target is the price from 5 min ago
        if entry_idx < 5:
            return None

        target_price = klines[entry_idx - 5]["open"]
        mins_remaining = 7.5  # Assume mid-window on average

        entry_contract = self._estimate_contract_price(
            btc_entry, target_price, mins_remaining
        )

        # Look 1 minute ahead for exit (our hold period)
        if entry_idx + 1 >= len(klines):
            return None

        btc_exit = klines[entry_idx + 1]["close"]
        exit_time = klines[entry_idx + 1]["time"]

        exit_contract = self._estimate_contract_price(
            btc_exit, target_price, mins_remaining - 1
        )

        # Calculate P&L based on direction
        if direction == "UP":
            # We bought YES
            entry_paid = entry_contract + self.spread / 2  # Pay the ask
            exit_received = exit_contract - self.spread / 2  # Sell at bid
            pnl = exit_received - entry_paid
        else:
            # We bought NO (equivalent to selling YES)
            entry_paid = (1 - entry_contract) + self.spread / 2
            exit_received = (1 - exit_contract) - self.spread / 2
            pnl = exit_received - entry_paid

        btc_move_pct = (btc_exit - btc_entry) / btc_entry

        # Did the momentum continue?
        momentum_continued = (direction == "UP" and btc_move_pct > 0) or \
                            (direction == "DOWN" and btc_move_pct < 0)

        return MomentumTrade(
            entry_time=entry_time,
            exit_time=exit_time,
            side="YES" if direction == "UP" else "NO",
            entry_price=entry_paid,
            exit_price=exit_received,
            btc_entry=btc_entry,
            btc_exit=btc_exit,
            btc_move_pct=btc_move_pct,
            hold_seconds=60,
            pnl_per_contract=pnl,
            outcome="WIN" if pnl > 0 else "LOSS",
        )

    def _estimate_contract_price(
        self, btc_price: float, target: float, mins_left: float
    ) -> float:
        """Estimate YES contract price given BTC price and time."""

        # Assume 0.3% vol per 15 min
        vol_15m = 0.003

        distance_pct = (btc_price - target) / target
        time_ratio = mins_left / 15.0
        expected_vol = vol_15m * math.sqrt(time_ratio)

        if expected_vol < 0.0001:
            expected_vol = 0.001

        z_score = distance_pct / expected_vol
        fair_value = stats.norm.cdf(z_score)

        return max(0.05, min(0.95, fair_value))


async def main():
    """Run momentum scalping backtest."""
    print("=" * 70)
    print("MOMENTUM SCALPING BACKTEST")
    print("=" * 70)
    print("\nStrategy: Detect BTC momentum, enter contract, exit in 60s")
    print("Goal: Capture contract repricing, not hold to expiration\n")

    # Test different trigger thresholds
    for trigger in [0.001, 0.0015, 0.002, 0.003]:
        print(f"\n{'='*60}")
        print(f"Trigger: {trigger*100:.2f}% BTC move in 1 min")
        print("=" * 60)

        backtester = MomentumScalpBacktest(
            min_move_trigger=trigger,
            hold_seconds=60,
            position_size=50,
            spread=0.02,
        )

        result = await backtester.run_backtest(days=7)

        print(f"\nSignals detected: {result['signals_detected']}")
        print(f"Trades taken: {result['trades']}")
        print(f"Win rate: {result['win_rate']*100:.1f}%")
        print(f"Total P&L: ${result['total_pnl']:.2f}")
        print(f"Avg P&L per trade: ${result['avg_pnl_per_trade']:.2f}")

        if result['large_move_trades'] > 0:
            print(f"\nLarge moves (>0.2%): {result['large_move_trades']} trades, "
                  f"{result['large_move_winrate']*100:.0f}% win rate")

    # Detailed analysis with optimal trigger
    print("\n" + "=" * 70)
    print("DETAILED ANALYSIS (0.15% trigger)")
    print("=" * 70)

    backtester = MomentumScalpBacktest(
        min_move_trigger=0.0015,
        hold_seconds=60,
        position_size=50,
        spread=0.02,
    )

    result = await backtester.run_backtest(days=7)

    print(f"\nLast 10 trades:")
    for t in result['trade_details'][-10:]:
        print(
            f"  {t.entry_time.strftime('%m/%d %H:%M')} | "
            f"{t.side:3} | "
            f"BTC: {t.btc_move_pct*100:+.2f}% | "
            f"Contract: {t.entry_price*100:.0f}c→{t.exit_price*100:.0f}c | "
            f"P&L: ${t.pnl_per_contract*50:+.2f} | "
            f"{t.outcome}"
        )


if __name__ == "__main__":
    asyncio.run(main())
