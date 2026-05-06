"""Order flow analysis from Binance for short-term direction prediction.

Tracks:
1. Aggressive trade flow (market orders hitting bids vs asks)
2. Large trade detection
3. Trade flow delta (buy pressure - sell pressure)
"""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional
from collections import deque
from dataclasses import dataclass

import websockets
import structlog

logger = structlog.get_logger()


@dataclass
class TradeFlowSignal:
    """Order flow signal for direction prediction."""
    delta_30s: float      # Net buy pressure last 30s (positive = bullish)
    delta_60s: float      # Net buy pressure last 60s
    delta_5m: float       # Net buy pressure last 5m
    large_buy_count: int  # Large buys (>$50k) last 5m
    large_sell_count: int # Large sells last 5m
    imbalance_ratio: float  # buy_volume / total_volume (0.5 = neutral)
    signal: str           # "BULLISH", "BEARISH", "NEUTRAL"
    strength: float       # 0-1 signal strength


class BinanceOrderFlowFeed:
    """Track aggressive order flow from Binance trades."""

    WS_URL = "wss://stream.binance.us:9443/ws/btcusdt@aggTrade"
    LARGE_TRADE_THRESHOLD = 50000  # $50k USD

    def __init__(self):
        self.trades: deque = deque(maxlen=10000)
        self._ws = None
        self._running = False

    async def connect(self):
        """Connect to Binance aggTrade stream."""
        self._running = True

        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws
                    logger.info("orderflow_ws_connected")

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_trade(message)

            except Exception as e:
                logger.error("orderflow_ws_error", error=str(e))
                if self._running:
                    await asyncio.sleep(1)

    async def disconnect(self):
        """Disconnect from WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _handle_trade(self, message: str):
        """Handle incoming aggTrade message."""
        try:
            data = json.loads(message)

            price = float(data["p"])
            quantity = float(data["q"])
            timestamp = datetime.fromtimestamp(data["T"] / 1000, tz=timezone.utc)
            is_buyer_maker = data["m"]  # True = sell aggressor, False = buy aggressor

            usd_value = price * quantity

            self.trades.append({
                "price": price,
                "quantity": quantity,
                "usd_value": usd_value,
                "timestamp": timestamp,
                "is_buy": not is_buyer_maker,  # Buy aggressor = bullish
                "is_large": usd_value >= self.LARGE_TRADE_THRESHOLD,
            })

        except Exception as e:
            logger.error("trade_parse_error", error=str(e))

    def get_signal(self) -> TradeFlowSignal:
        """Calculate order flow signal."""
        if len(self.trades) < 100:
            return TradeFlowSignal(
                delta_30s=0, delta_60s=0, delta_5m=0,
                large_buy_count=0, large_sell_count=0,
                imbalance_ratio=0.5, signal="NEUTRAL", strength=0
            )

        trades = list(self.trades)
        now = trades[-1]["timestamp"].timestamp()

        # Calculate deltas for different windows
        delta_30s = self._calc_delta(trades, now, 30)
        delta_60s = self._calc_delta(trades, now, 60)
        delta_5m = self._calc_delta(trades, now, 300)

        # Count large trades
        large_buys = sum(
            1 for t in trades
            if t["is_large"] and t["is_buy"]
            and now - t["timestamp"].timestamp() < 300
        )
        large_sells = sum(
            1 for t in trades
            if t["is_large"] and not t["is_buy"]
            and now - t["timestamp"].timestamp() < 300
        )

        # Imbalance ratio
        recent = [t for t in trades if now - t["timestamp"].timestamp() < 60]
        buy_vol = sum(t["usd_value"] for t in recent if t["is_buy"])
        sell_vol = sum(t["usd_value"] for t in recent if not t["is_buy"])
        total_vol = buy_vol + sell_vol
        imbalance = buy_vol / total_vol if total_vol > 0 else 0.5

        # Generate signal
        signal, strength = self._classify_signal(
            delta_30s, delta_60s, delta_5m,
            large_buys, large_sells, imbalance
        )

        return TradeFlowSignal(
            delta_30s=delta_30s,
            delta_60s=delta_60s,
            delta_5m=delta_5m,
            large_buy_count=large_buys,
            large_sell_count=large_sells,
            imbalance_ratio=imbalance,
            signal=signal,
            strength=strength,
        )

    def _calc_delta(self, trades: list, now: float, seconds: int) -> float:
        """Calculate net buy pressure (buy - sell volume) in USD."""
        recent = [t for t in trades if now - t["timestamp"].timestamp() < seconds]
        buy_vol = sum(t["usd_value"] for t in recent if t["is_buy"])
        sell_vol = sum(t["usd_value"] for t in recent if not t["is_buy"])
        return buy_vol - sell_vol

    def _classify_signal(
        self, delta_30s: float, delta_60s: float, delta_5m: float,
        large_buys: int, large_sells: int, imbalance: float
    ) -> tuple[str, float]:
        """Classify order flow into signal."""

        # Score components
        scores = []

        # Short-term delta (most important)
        if delta_30s > 100000:  # $100k net buy
            scores.append(("BULLISH", 0.3))
        elif delta_30s < -100000:
            scores.append(("BEARISH", 0.3))

        # Medium-term delta
        if delta_60s > 200000:
            scores.append(("BULLISH", 0.2))
        elif delta_60s < -200000:
            scores.append(("BEARISH", 0.2))

        # Large trade imbalance
        if large_buys > large_sells + 2:
            scores.append(("BULLISH", 0.25))
        elif large_sells > large_buys + 2:
            scores.append(("BEARISH", 0.25))

        # Imbalance ratio
        if imbalance > 0.6:
            scores.append(("BULLISH", 0.25))
        elif imbalance < 0.4:
            scores.append(("BEARISH", 0.25))

        if not scores:
            return "NEUTRAL", 0.0

        # Aggregate
        bullish = sum(s[1] for s in scores if s[0] == "BULLISH")
        bearish = sum(s[1] for s in scores if s[0] == "BEARISH")

        if bullish > bearish and bullish > 0.3:
            return "BULLISH", min(1.0, bullish)
        elif bearish > bullish and bearish > 0.3:
            return "BEARISH", min(1.0, bearish)
        else:
            return "NEUTRAL", 0.0


async def main():
    """Test order flow feed."""
    print("=" * 70)
    print("BINANCE ORDER FLOW MONITOR")
    print("=" * 70)
    print("\nCollecting trade data for 60 seconds...\n")

    feed = BinanceOrderFlowFeed()
    task = asyncio.create_task(feed.connect())

    for i in range(12):
        await asyncio.sleep(5)
        signal = feed.get_signal()
        print(
            f"[{i*5+5}s] "
            f"Delta30s: ${signal.delta_30s:+,.0f} | "
            f"Delta60s: ${signal.delta_60s:+,.0f} | "
            f"Imbalance: {signal.imbalance_ratio:.2f} | "
            f"Large B/S: {signal.large_buy_count}/{signal.large_sell_count} | "
            f"Signal: {signal.signal} ({signal.strength:.0%})"
        )

    await feed.disconnect()
    task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
