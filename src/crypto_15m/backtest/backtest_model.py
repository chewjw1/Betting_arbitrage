"""Backtesting framework for 15-minute crypto prediction model.

Uses historical Binance kline data to simulate:
1. 15-minute windows (did price go up or down?)
2. Our signal generation
3. Track hypothetical P&L

Note: We can't get historical Kalshi order books, so we simulate
market prices based on where BTC price is relative to window start.
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
class BacktestWindow:
    """One 15-minute window for backtesting."""
    start_time: datetime
    end_time: datetime
    start_price: float
    end_price: float
    high: float
    low: float
    outcome: str  # "yes" or "no"

    # Mid-window snapshots for simulation
    snapshots: list[dict] = field(default_factory=list)


@dataclass
class BacktestTrade:
    """A simulated trade."""
    timestamp: datetime
    side: str
    entry_price: float
    exit_price: float
    edge: float
    signal_strength: float
    outcome: str  # "WIN" or "LOSS"
    pnl: float
    reasoning: str


@dataclass
class BacktestResult:
    """Results of backtest."""
    total_windows: int
    trades_taken: int
    wins: int
    losses: int
    total_pnl: float
    win_rate: float
    avg_edge: float
    sharpe_ratio: float
    max_drawdown: float
    trades: list[BacktestTrade]


class HistoricalDataFetcher:
    """Fetch historical BTC data from Binance."""

    BASE_URL = "https://api.binance.us/api/v3"

    async def get_klines(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1m",  # 1-minute candles
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = 1000
    ) -> list[dict]:
        """Fetch kline/candlestick data."""
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }

        if start_time:
            params["startTime"] = int(start_time.timestamp() * 1000)
        if end_time:
            params["endTime"] = int(end_time.timestamp() * 1000)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{self.BASE_URL}/klines", params=params)

            if resp.status_code != 200:
                raise Exception(f"API error: {resp.status_code}")

            klines = []
            for k in resp.json():
                klines.append({
                    "open_time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc),
                })

            return klines


class Backtester:
    """Backtest the prediction model on historical data."""

    def __init__(
        self,
        min_edge: float = 0.04,
        min_strength: float = 0.3,
        position_size: float = 50,  # $50 per trade
    ):
        self.min_edge = min_edge
        self.min_strength = min_strength
        self.position_size = position_size
        self.entry_minute = 13  # Default to late window
        self.fetcher = HistoricalDataFetcher()

    async def run_backtest(
        self,
        days: int = 7,
        end_date: datetime = None
    ) -> BacktestResult:
        """Run backtest over historical period."""

        end_date = end_date or datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)

        print(f"Fetching {days} days of BTC data...")
        print(f"  From: {start_date.strftime('%Y-%m-%d %H:%M')}")
        print(f"  To: {end_date.strftime('%Y-%m-%d %H:%M')}")

        # Fetch 1-minute candles in chunks
        all_klines = []
        current = start_date

        while current < end_date:
            chunk_end = min(current + timedelta(hours=16), end_date)  # ~1000 candles
            klines = await self.fetcher.get_klines(
                start_time=current,
                end_time=chunk_end,
                limit=1000
            )
            all_klines.extend(klines)
            current = chunk_end
            await asyncio.sleep(0.5)  # Rate limiting

        print(f"  Fetched {len(all_klines)} candles")

        # Build 15-minute windows
        windows = self._build_windows(all_klines)
        print(f"  Built {len(windows)} 15-minute windows")

        # Run simulation
        trades = []
        pnls = []

        for window in windows:
            trade = self._simulate_window(window)
            if trade:
                trades.append(trade)
                pnls.append(trade.pnl)

        # Calculate metrics
        wins = sum(1 for t in trades if t.outcome == "WIN")
        losses = sum(1 for t in trades if t.outcome == "LOSS")
        total_pnl = sum(t.pnl for t in trades)

        win_rate = wins / len(trades) if trades else 0
        avg_edge = sum(t.edge for t in trades) / len(trades) if trades else 0

        # Sharpe ratio (assuming daily)
        if pnls:
            returns = [p / self.position_size for p in pnls]
            mean_ret = sum(returns) / len(returns)
            std_ret = math.sqrt(sum((r - mean_ret)**2 for r in returns) / len(returns))
            sharpe = (mean_ret / std_ret) * math.sqrt(252 * 4 * 24) if std_ret > 0 else 0
        else:
            sharpe = 0

        # Max drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for pnl in pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        return BacktestResult(
            total_windows=len(windows),
            trades_taken=len(trades),
            wins=wins,
            losses=losses,
            total_pnl=total_pnl,
            win_rate=win_rate,
            avg_edge=avg_edge,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            trades=trades,
        )

    def _build_windows(self, klines: list[dict]) -> list[BacktestWindow]:
        """Build 15-minute windows from 1-minute candles."""
        windows = []

        # Group into 15-minute chunks
        i = 0
        while i + 15 <= len(klines):
            chunk = klines[i:i+15]

            start_price = chunk[0]["open"]
            end_price = chunk[-1]["close"]
            high = max(k["high"] for k in chunk)
            low = min(k["low"] for k in chunk)
            outcome = "yes" if end_price >= start_price else "no"

            # Create snapshots at 5, 10, 13 minutes for signal generation
            snapshots = []
            for minute in [5, 10, 13]:
                if minute < len(chunk):
                    idx = minute
                    snapshots.append({
                        "minute": minute,
                        "price": chunk[idx]["close"],
                        "vol": self._calc_vol(chunk[:idx+1]),
                    })

            windows.append(BacktestWindow(
                start_time=chunk[0]["open_time"],
                end_time=chunk[-1]["close_time"],
                start_price=start_price,
                end_price=end_price,
                high=high,
                low=low,
                outcome=outcome,
                snapshots=snapshots,
            ))

            i += 15

        return windows

    def _calc_vol(self, klines: list[dict]) -> float:
        """Calculate realized volatility from klines."""
        if len(klines) < 2:
            return 0.003

        returns = []
        for j in range(1, len(klines)):
            ret = (klines[j]["close"] - klines[j-1]["close"]) / klines[j-1]["close"]
            returns.append(ret)

        if returns:
            return math.sqrt(sum(r**2 for r in returns) / len(returns))
        return 0.003

    def _simulate_window(self, window: BacktestWindow) -> Optional[BacktestTrade]:
        """Simulate trading decision for one window.

        Tests predictive power: given the state at minute X, does our
        prediction of YES/NO match the actual outcome?
        """

        # Use snapshot at configured entry minute
        snapshot = None
        for s in window.snapshots:
            if s["minute"] == self.entry_minute:
                snapshot = s
                break

        if not snapshot:
            return None

        current_price = snapshot["price"]
        target_price = window.start_price
        mins_left = 15 - self.entry_minute
        vol = snapshot["vol"]

        # Calculate our model's probability
        distance_pct = (current_price - target_price) / target_price
        time_ratio = mins_left / 15.0
        expected_vol = vol * math.sqrt(time_ratio)

        if expected_vol < 0.0001:
            expected_vol = 0.001

        z_score = distance_pct / expected_vol
        base_prob_yes = stats.norm.cdf(z_score)

        # Momentum adjustment (simulate having 2 prior results)
        # In real backtest we'd track this, for now use price trend
        price_trend = (current_price - window.start_price) / window.start_price
        if price_trend > 0.001:  # Trending up
            momentum_adj = 0.08
        elif price_trend < -0.001:  # Trending down
            momentum_adj = -0.05
        else:
            momentum_adj = 0

        our_prob_yes = base_prob_yes + momentum_adj
        our_prob_yes = max(0.05, min(0.95, our_prob_yes))

        # Signal: predict YES if prob > 55%, NO if prob < 45%
        # Only trade when we have conviction
        signal_strength = abs(our_prob_yes - 0.5) * 2  # 0-1 scale

        # Simulate market prices (assume market is at base_prob with spread)
        # This represents "what we'd pay" vs "what we think it's worth"
        market_mid = base_prob_yes
        spread = 0.04  # 4 cent spread
        market_ask_yes = market_mid + spread/2
        market_ask_no = (1 - market_mid) + spread/2

        # Our edge vs market
        edge_yes = our_prob_yes - market_ask_yes
        edge_no = (1 - our_prob_yes) - market_ask_no

        # Decision thresholds - only trade strong signals
        if our_prob_yes > 0.60 and edge_yes > 0.02:  # Predict YES
            entry_price = market_ask_yes
            exit_price = 1.0 if window.outcome == "yes" else 0.0
            pnl = (exit_price - entry_price) * self.position_size

            return BacktestTrade(
                timestamp=window.start_time + timedelta(minutes=self.entry_minute),
                side="YES",
                entry_price=entry_price,
                exit_price=exit_price,
                edge=edge_yes,
                signal_strength=signal_strength,
                outcome="WIN" if window.outcome == "yes" else "LOSS",
                pnl=pnl,
                reasoning=f"z={z_score:.2f}, prob={our_prob_yes:.2f}, trend={price_trend*100:+.2f}%",
            )

        elif our_prob_yes < 0.40 and edge_no > 0.02:  # Predict NO
            entry_price = market_ask_no
            exit_price = 1.0 if window.outcome == "no" else 0.0
            pnl = (exit_price - entry_price) * self.position_size

            return BacktestTrade(
                timestamp=window.start_time + timedelta(minutes=self.entry_minute),
                side="NO",
                entry_price=entry_price,
                exit_price=exit_price,
                edge=edge_no,
                signal_strength=signal_strength,
                outcome="WIN" if window.outcome == "no" else "LOSS",
                pnl=pnl,
                reasoning=f"z={z_score:.2f}, prob_no={1-our_prob_yes:.2f}",
            )

        return None


async def main():
    """Run backtest."""
    print("=" * 70)
    print("15-MINUTE CRYPTO PREDICTION BACKTEST")
    print("=" * 70)

    # Test different entry times
    for entry_minute in [5, 10, 13]:
        print(f"\n{'='*70}")
        print(f"Testing entry at minute {entry_minute} ({15-entry_minute} min remaining)")
        print("=" * 70)

        backtester = Backtester(
            min_edge=0.02,  # Lower threshold for more trades
            min_strength=0.2,
            position_size=50,
        )
        backtester.entry_minute = entry_minute

        result = await backtester.run_backtest(days=7)

        print(f"\nResults for minute {entry_minute}:")
        print(f"  Trades: {result.trades_taken}")
        print(f"  Win Rate: {result.win_rate*100:.1f}%")
        print(f"  Total P&L: ${result.total_pnl:.2f}")
        print(f"  Avg Edge: {result.avg_edge*100:.1f}%")

    print("\n" + "=" * 70)
    print("Running full analysis with optimal parameters...")
    print("=" * 70)

    backtester = Backtester(
        min_edge=0.02,
        min_strength=0.2,
        position_size=50,
    )
    backtester.entry_minute = 13  # Late window

    result = await backtester.run_backtest(days=7)

    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)

    print(f"\nPeriod: 7 days")
    print(f"Total 15-min windows: {result.total_windows}")
    print(f"Trades taken: {result.trades_taken} ({result.trades_taken/result.total_windows*100:.1f}% of windows)")

    print(f"\nPerformance:")
    print(f"  Wins: {result.wins}")
    print(f"  Losses: {result.losses}")
    print(f"  Win Rate: {result.win_rate*100:.1f}%")
    print(f"  Total P&L: ${result.total_pnl:.2f}")
    print(f"  Avg Edge: {result.avg_edge*100:.1f}%")
    print(f"  Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print(f"  Max Drawdown: ${result.max_drawdown:.2f}")

    # Show recent trades
    if result.trades:
        print(f"\nLast 10 trades:")
        for t in result.trades[-10:]:
            print(
                f"  {t.timestamp.strftime('%m/%d %H:%M')} | "
                f"{t.side:3} @ {t.entry_price*100:.0f}c | "
                f"Edge: {t.edge*100:+.0f}% | "
                f"{t.outcome}: ${t.pnl:+.2f}"
            )

    # Edge analysis
    if result.trades:
        print(f"\nEdge Analysis:")
        high_edge = [t for t in result.trades if t.edge > 0.08]
        if high_edge:
            high_wins = sum(1 for t in high_edge if t.outcome == "WIN")
            print(f"  High edge (>8%): {len(high_edge)} trades, {high_wins/len(high_edge)*100:.0f}% win rate")

        low_edge = [t for t in result.trades if t.edge <= 0.08]
        if low_edge:
            low_wins = sum(1 for t in low_edge if t.outcome == "WIN")
            print(f"  Low edge (4-8%): {len(low_edge)} trades, {low_wins/len(low_edge)*100:.0f}% win rate")


if __name__ == "__main__":
    asyncio.run(main())
