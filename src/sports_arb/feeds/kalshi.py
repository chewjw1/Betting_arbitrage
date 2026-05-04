"""Kalshi WebSocket feed for real-time prices."""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Optional
import structlog

from .base import BaseFeed, PriceUpdate, PriceCallback

logger = structlog.get_logger()

KALSHI_WS_URL = "wss://trading-api.kalshi.com/trade-api/ws/v2"
KALSHI_ELECTIONS_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


class KalshiFeed(BaseFeed):
    """Real-time price feed from Kalshi WebSocket API."""

    def __init__(
        self,
        callback: Optional[PriceCallback] = None,
        api_key: Optional[str] = None,
        use_elections_api: bool = True,
    ):
        super().__init__(callback)
        self.api_key = api_key
        self.ws_url = KALSHI_ELECTIONS_WS_URL if use_elections_api else KALSHI_WS_URL
        self._ws = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._seq_num = 0

    @property
    def platform(self) -> str:
        return "kalshi"

    async def connect(self) -> None:
        """Connect to Kalshi WebSocket."""
        try:
            import websockets
        except ImportError:
            raise ImportError("websockets package required: pip install websockets")

        logger.info("kalshi_ws_connecting", url=self.ws_url)

        self._ws = await websockets.connect(
            self.ws_url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        )
        self._connected = True
        logger.info("kalshi_ws_connected")

        asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False
        logger.info("kalshi_ws_disconnected")

    async def _receive_loop(self) -> None:
        """Background task to receive messages."""
        try:
            async for message in self._ws:
                data = json.loads(message)
                await self._message_queue.put(data)
        except Exception as e:
            logger.error("kalshi_ws_receive_error", error=str(e))
            self._connected = False

    async def subscribe(self, market_ids: list[str]) -> None:
        """Subscribe to orderbook updates for markets."""
        if not self._ws:
            raise RuntimeError("Not connected")

        for market_id in market_ids:
            self._seq_num += 1
            msg = {
                "id": self._seq_num,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_ticker": market_id,
                }
            }
            await self._ws.send(json.dumps(msg))
            self._subscribed_markets.add(market_id)
            logger.debug("kalshi_subscribed", market_id=market_id)

    async def unsubscribe(self, market_ids: list[str]) -> None:
        """Unsubscribe from markets."""
        if not self._ws:
            return

        for market_id in market_ids:
            self._seq_num += 1
            msg = {
                "id": self._seq_num,
                "cmd": "unsubscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_ticker": market_id,
                }
            }
            await self._ws.send(json.dumps(msg))
            self._subscribed_markets.discard(market_id)

    async def stream(self) -> AsyncIterator[PriceUpdate]:
        """Stream price updates from Kalshi."""
        while self._running and self._connected:
            try:
                data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=30.0
                )

                if data.get("type") == "orderbook_snapshot":
                    update = self._parse_orderbook(data)
                    if update:
                        yield update
                elif data.get("type") == "orderbook_delta":
                    update = self._parse_orderbook(data)
                    if update:
                        yield update

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("kalshi_stream_error", error=str(e))

    def _parse_orderbook(self, data: dict) -> Optional[PriceUpdate]:
        """Parse orderbook message into PriceUpdate."""
        try:
            msg = data.get("msg", {})
            market_ticker = msg.get("market_ticker", "")

            yes_bids = msg.get("yes", [])
            no_bids = msg.get("no", [])

            yes_bid = yes_ask = no_bid = no_ask = None
            yes_bid_size = yes_ask_size = no_bid_size = no_ask_size = None

            if yes_bids:
                sorted_bids = sorted(yes_bids, key=lambda x: x[0], reverse=True)
                if sorted_bids:
                    yes_bid = Decimal(str(sorted_bids[0][0])) / 100
                    yes_bid_size = Decimal(str(sorted_bids[0][1]))

            if no_bids:
                sorted_no = sorted(no_bids, key=lambda x: x[0], reverse=True)
                if sorted_no:
                    no_bid = Decimal(str(sorted_no[0][0])) / 100
                    no_bid_size = Decimal(str(sorted_no[0][1]))

            if yes_bid is not None:
                yes_ask = Decimal("1") - (no_bid if no_bid else Decimal("0"))
            if no_bid is not None:
                no_ask = Decimal("1") - (yes_bid if yes_bid else Decimal("0"))

            return PriceUpdate(
                platform="kalshi",
                market_id=market_ticker,
                event_id=market_ticker.rsplit("-", 1)[0] if "-" in market_ticker else market_ticker,
                title=market_ticker,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                yes_bid_size=yes_bid_size,
                no_bid_size=no_bid_size,
                timestamp=datetime.utcnow(),
                raw_data=data,
            )
        except Exception as e:
            logger.error("kalshi_parse_error", error=str(e), data=data)
            return None


async def test_kalshi_feed():
    """Test the Kalshi feed with a sample market."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            params={"limit": 5, "status": "open"}
        )
        markets = resp.json().get("markets", [])

    if not markets:
        print("No markets found")
        return

    market_ids = [m["ticker"] for m in markets[:3]]
    print(f"Testing with markets: {market_ids}")

    async def on_update(update: PriceUpdate):
        print(f"[{update.timestamp}] {update.market_id}: "
              f"YES {update.yes_bid}/{update.yes_ask} "
              f"NO {update.no_bid}/{update.no_ask}")

    feed = KalshiFeed(callback=on_update)
    await feed.connect()
    await feed.subscribe(market_ids)

    try:
        count = 0
        async for update in feed.stream():
            print(f"Update: {update.market_id} YES={update.yes_bid}")
            count += 1
            if count >= 10:
                break
    finally:
        await feed.disconnect()


if __name__ == "__main__":
    asyncio.run(test_kalshi_feed())
