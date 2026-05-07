"""Mean reversion scalping backtest.

Strategy: When BTC spikes in one direction, fade it.
Hypothesis: Short-term moves often overshoot and revert.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional
import math

import httpx
from scipy import stats


@dataclass
class ReversionTrade:
    """A mean reversion trade."""
    entry_time: datetime
    exit_time: datetime
    side: str
    entry_price: float
    exit_price: float
    btc_entry: float
    btc_exit: float
    trigger_move: float  # The move we faded
    reversion_pct: float  # How much it reverted
    pnl_per_contract: float
    outcome: str


class MeanReversionBacktest:
    """Backtest mean reversion scalping."""

    def __init__(
        self,
        min_move_trigger: float = 0.002,  # 0.2% move to trigger fade
        hold_minutes: int = 2,  # Hold for reversion
        position_size: float = 50,
        spread: float = 0.02,
    ):
        self.min_move_trigger = min_move_trigger
        self.hold_minutes = hold_minutes
        self.position_size = position_size
        self.spread = spread

    async def run_backtest(self, days: int = 7) -> dict:
        """Run mean reversion backtest."""

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

        trades = []
        cooldown_until = None

        for i in range(2, len(all_klines) - self.hold_minutes):
            current_time = all_klines[i]["time"]

            # Cooldown to avoid overlapping trades
            if cooldown_until and current_time < cooldown_until:
                continue

            # Detect spike (2-minute move)
            current_price = all_klines[i]["close"]
            price_2m_ago = all_klines[i-2]["close"]
            move_2m = (current_price - price_2m_ago) / price_2m_ago

            if abs(move_2m) >= self.min_move_trigger:
                # Fade the move
                trade = self._simulate_fade(all_klines, i, move_2m, current_price)
                if trade:
                    trades.append(trade)
                    cooldown_until = current_time + timedelta(minutes=self.hold_minutes + 1)

        # Calculate results
        wins = sum(1 for t in trades if t.outcome == "WIN")
        losses = len(trades) - wins
        total_pnl = sum(t.pnl_per_contract * self.position_size for t in trades)

        # Analyze by trigger size
        small_triggers = [t for t in trades if abs(t.trigger_move) < 0.003]
        large_triggers = [t for t in trades if abs(t.trigger_move) >= 0.003]

        return {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(trades) if trades else 0,
            "total_pnl": total_pnl,
            "avg_reversion": sum(t.reversion_pct for t in trades) / len(trades) if trades else 0,
            "small_trigger_winrate": sum(1 for t in small_triggers if t.outcome == "WIN") / len(small_triggers) if small_triggers else 0,
            "large_trigger_winrate": sum(1 for t in large_triggers if t.outcome == "WIN") / len(large_triggers) if large_triggers else 0,
            "trade_details": trades[-15:],
        }

    def _simulate_fade(
        self, klines: list, entry_idx: int, trigger_move: float, btc_entry: float
    ) -> Optional[ReversionTrade]:
        """Simulate fading a momentum spike."""

        entry_time = klines[entry_idx]["time"]

        # Target price (assume 5 min ago)
        if entry_idx < 5:
            return None

        target_price = klines[entry_idx - 5]["open"]
        mins_remaining = 7.5

        # If price spiked UP, we BUY NO (bet on reversion down)
        # If price spiked DOWN, we BUY YES (bet on reversion up)
        fade_direction = "NO" if trigger_move > 0 else "YES"

        entry_contract = self._estimate_contract_price(
            btc_entry, target_price, mins_remaining
        )

        # Look ahead for exit
        exit_idx = entry_idx + self.hold_minutes
        if exit_idx >= len(klines):
            return None

        btc_exit = klines[exit_idx]["close"]
        exit_time = klines[exit_idx]["time"]

        exit_contract = self._estimate_contract_price(
            btc_exit, target_price, mins_remaining - self.hold_minutes
        )

        # Calculate reversion
        reversion_pct = (btc_exit - btc_entry) / btc_entry
        # Positive reversion_pct when fading UP spike means it went up more (bad)
        # We want it to go down (negative reversion_pct) when fading UP

        if fade_direction == "NO":
            # We bought NO, betting price drops
            entry_paid = (1 - entry_contract) + self.spread / 2
            exit_received = (1 - exit_contract) - self.spread / 2
            pnl = exit_received - entry_paid
        else:
            # We bought YES, betting price rises
            entry_paid = entry_contract + self.spread / 2
            exit_received = exit_contract - self.spread / 2
            pnl = exit_received - entry_paid

        return ReversionTrade(
            entry_time=entry_time,
            exit_time=exit_time,
            side=fade_direction,
            entry_price=entry_paid,
            exit_price=exit_received,
            btc_entry=btc_entry,
            btc_exit=btc_exit,
            trigger_move=trigger_move,
            reversion_pct=reversion_pct,
            pnl_per_contract=pnl,
            outcome="WIN" if pnl > 0 else "LOSS",
        )

    def _estimate_contract_price(
        self, btc_price: float, target: float, mins_left: float
    ) -> float:
        """Estimate YES contract price."""
        vol_15m = 0.003
        distance_pct = (btc_price - target) / target
        time_ratio = max(0.01, mins_left / 15.0)
        expected_vol = vol_15m * math.sqrt(time_ratio)

        if expected_vol < 0.0001:
            expected_vol = 0.001

        z_score = distance_pct / expected_vol
        return max(0.05, min(0.95, stats.norm.cdf(z_score)))


async def main():
    """Run mean reversion backtest."""
    print("=" * 70)
    print("MEAN REVERSION SCALPING BACKTEST")
    print("=" * 70)
    print("\nStrategy: Fade BTC spikes, bet on reversion")
    print("When BTC spikes up 0.2%+, buy NO. When it spikes down, buy YES.\n")

    for trigger in [0.002, 0.003, 0.004, 0.005]:
        print(f"\n{'='*60}")
        print(f"Trigger: {trigger*100:.1f}% spike to fade")
        print("=" * 60)

        backtester = MeanReversionBacktest(
            min_move_trigger=trigger,
            hold_minutes=2,
            position_size=50,
            spread=0.02,
        )

        result = await backtester.run_backtest(days=7)

        print(f"\nTrades: {result['trades']}")
        print(f"Win rate: {result['win_rate']*100:.1f}%")
        print(f"Total P&L: ${result['total_pnl']:.2f}")
        print(f"Avg reversion: {result['avg_reversion']*100:.3f}%")

        if result['trades'] > 0:
            print(f"Small trigger (<0.3%) win rate: {result['small_trigger_winrate']*100:.0f}%")
            print(f"Large trigger (>0.3%) win rate: {result['large_trigger_winrate']*100:.0f}%")

    print("\n" + "=" * 70)
    print("DETAILED ANALYSIS (0.3% trigger)")
    print("=" * 70)

    backtester = MeanReversionBacktest(
        min_move_trigger=0.003,
        hold_minutes=2,
        position_size=50,
        spread=0.02,
    )

    result = await backtester.run_backtest(days=7)

    print(f"\nLast 15 trades:")
    for t in result['trade_details']:
        revert_dir = "✓" if (t.trigger_move > 0 and t.reversion_pct < 0) or \
                           (t.trigger_move < 0 and t.reversion_pct > 0) else "✗"
        print(
            f"  {t.entry_time.strftime('%m/%d %H:%M')} | "
            f"Spike: {t.trigger_move*100:+.2f}% | "
            f"Fade: {t.side:3} | "
            f"Revert: {t.reversion_pct*100:+.2f}% {revert_dir} | "
            f"P&L: ${t.pnl_per_contract*50:+.2f} | "
            f"{t.outcome}"
        )


if __name__ == "__main__":
    asyncio.run(main())
