"""Real-time BTC price feed from Binance WebSocket."""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional
from collections import deque
import math

import websockets
import httpx
import structlog

logger = structlog.get_logger()


class BinanceBTCFeed:
    """Real-time BTC/USDT price feed with momentum/volatility features."""

    WS_URL = "wss://stream.binance.us:9443/ws/btcusdt@trade"
    REST_URL = "https://api.binance.us/api/v3"

    def __init__(self, price_callback: Optional[Callable] = None):
        self.price_callback = price_callback
        self.current_price: Decimal = Decimal(0)
        self.last_update: datetime = datetime.now(timezone.utc)

        # Price history for features (last 60 minutes of minute candles)
        self.price_history: deque = deque(maxlen=60)
        # Tick data for volatility (last 15 min of ticks)
        self.recent_ticks: deque = deque(maxlen=5000)

        self._ws = None
        self._running = False

    async def connect(self):
        """Connect to Binance WebSocket and start streaming."""
        self._running = True

        # Get initial price
        await self._fetch_initial_price()

        # Start WebSocket
        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws
                    logger.info("binance_ws_connected")

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except Exception as e:
                logger.error("binance_ws_error", error=str(e))
                if self._running:
                    await asyncio.sleep(1)

    async def disconnect(self):
        """Disconnect from WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _fetch_initial_price(self):
        """Get current price via REST API."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.REST_URL}/ticker/price", params={"symbol": "BTCUSDT"})
            if resp.status_code == 200:
                self.current_price = Decimal(resp.json()["price"])
                logger.info("initial_price_fetched", price=float(self.current_price))

    async def _handle_message(self, message: str):
        """Handle incoming trade message."""
        try:
            data = json.loads(message)
            price = Decimal(data["p"])
            timestamp = datetime.fromtimestamp(data["T"] / 1000, tz=timezone.utc)

            self.current_price = price
            self.last_update = timestamp

            # Store tick for volatility calculation
            self.recent_ticks.append({
                "price": float(price),
                "time": timestamp,
            })

            if self.price_callback:
                await self.price_callback(price, timestamp)

        except Exception as e:
            logger.error("message_parse_error", error=str(e))

    def get_momentum_features(self) -> dict:
        """Calculate momentum features from recent price action."""
        if len(self.recent_ticks) < 100:
            return {"valid": False}

        ticks = list(self.recent_ticks)
        current = ticks[-1]["price"]
        now = ticks[-1]["time"]

        features = {"valid": True, "current_price": current}

        # Returns over different windows
        for mins, label in [(1, "1m"), (5, "5m"), (15, "15m")]:
            cutoff = now.timestamp() - (mins * 60)
            old_ticks = [t for t in ticks if t["time"].timestamp() < cutoff]
            if old_ticks:
                old_price = old_ticks[-1]["price"]
                features[f"return_{label}"] = (current - old_price) / old_price
            else:
                features[f"return_{label}"] = 0.0

        return features

    def get_volatility_features(self) -> dict:
        """Calculate volatility features."""
        if len(self.recent_ticks) < 100:
            return {"valid": False}

        ticks = list(self.recent_ticks)
        now = ticks[-1]["time"]

        # Get prices from last 15 minutes
        cutoff = now.timestamp() - (15 * 60)
        recent = [t["price"] for t in ticks if t["time"].timestamp() > cutoff]

        if len(recent) < 50:
            return {"valid": False}

        # Calculate realized volatility
        returns = []
        for i in range(1, len(recent)):
            ret = (recent[i] - recent[i-1]) / recent[i-1]
            returns.append(ret)

        if returns:
            vol_15m = math.sqrt(sum(r**2 for r in returns) / len(returns))
            # Annualize (roughly)
            vol_annualized = vol_15m * math.sqrt(365 * 24 * 4)  # 4 15-min periods per hour
        else:
            vol_15m = 0
            vol_annualized = 0

        # Expected move in remaining time
        # This will be used by the fair value calculator

        return {
            "valid": True,
            "vol_15m": vol_15m,
            "vol_annualized": vol_annualized,
            "price_range_15m": max(recent) - min(recent),
        }

    def get_all_features(self) -> dict:
        """Get all features for model input."""
        momentum = self.get_momentum_features()
        volatility = self.get_volatility_features()

        return {
            "price": float(self.current_price),
            "timestamp": self.last_update.isoformat(),
            **momentum,
            **volatility,
        }


async def main():
    """Test the feed."""
    async def on_price(price, timestamp):
        pass  # Silent callback for testing

    feed = BinanceBTCFeed(price_callback=on_price)

    # Run for 30 seconds to collect data
    task = asyncio.create_task(feed.connect())

    print("Collecting BTC data for 30 seconds...")
    await asyncio.sleep(30)

    features = feed.get_all_features()
    print(f"\nFeatures after 30 seconds:")
    for k, v in features.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.6f}")
        else:
            print(f"  {k}: {v}")

    await feed.disconnect()
    task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
