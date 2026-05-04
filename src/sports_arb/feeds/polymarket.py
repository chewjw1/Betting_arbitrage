"""Polymarket WebSocket feed for real-time prices."""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Optional
import structlog

from .base import BaseFeed, PriceUpdate, PriceCallback

logger = structlog.get_logger()

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketFeed(BaseFeed):
    """Real-time price feed from Polymarket CLOB WebSocket."""

    def __init__(
        self,
        callback: Optional[PriceCallback] = None,
    ):
        super().__init__(callback)
        self._ws = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._market_info: dict[str, dict] = {}

    @property
    def platform(self) -> str:
        return "polymarket"

    async def connect(self) -> None:
        """Connect to Polymarket WebSocket."""
        try:
            import websockets
        except ImportError:
            raise ImportError("websockets package required: pip install websockets")

        logger.info("polymarket_ws_connecting", url=POLYMARKET_WS_URL)

        self._ws = await websockets.connect(
            POLYMARKET_WS_URL,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        )
        self._connected = True
        logger.info("polymarket_ws_connected")

        asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False
        logger.info("polymarket_ws_disconnected")

    async def _receive_loop(self) -> None:
        """Background task to receive messages."""
        try:
            async for message in self._ws:
                data = json.loads(message)
                await self._message_queue.put(data)
        except Exception as e:
            logger.error("polymarket_ws_receive_error", error=str(e))
            self._connected = False

    async def subscribe(self, market_ids: list[str]) -> None:
        """Subscribe to orderbook updates for markets.

        Args:
            market_ids: List of condition_id values (the token IDs for Polymarket)
        """
        if not self._ws:
            raise RuntimeError("Not connected")

        for market_id in market_ids:
            msg = {
                "type": "subscribe",
                "channel": "market",
                "assets_id": market_id,
            }
            await self._ws.send(json.dumps(msg))
            self._subscribed_markets.add(market_id)
            logger.debug("polymarket_subscribed", market_id=market_id)

    async def subscribe_with_info(
        self,
        markets: list[dict],
    ) -> None:
        """Subscribe with market metadata for better logging.

        Args:
            markets: List of dicts with 'token_id' and optional 'title', 'event_id'
        """
        for m in markets:
            token_id = m.get("token_id") or m.get("condition_id")
            if token_id:
                self._market_info[token_id] = m

        await self.subscribe([
            m.get("token_id") or m.get("condition_id")
            for m in markets
            if m.get("token_id") or m.get("condition_id")
        ])

    async def unsubscribe(self, market_ids: list[str]) -> None:
        """Unsubscribe from markets."""
        if not self._ws:
            return

        for market_id in market_ids:
            msg = {
                "type": "unsubscribe",
                "channel": "market",
                "assets_id": market_id,
            }
            await self._ws.send(json.dumps(msg))
            self._subscribed_markets.discard(market_id)
            self._market_info.pop(market_id, None)

    async def stream(self) -> AsyncIterator[PriceUpdate]:
        """Stream price updates from Polymarket."""
        while self._running and self._connected:
            try:
                data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=30.0
                )

                msg_type = data.get("type")
                if msg_type in ("book", "price_change"):
                    update = self._parse_book(data)
                    if update:
                        yield update

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("polymarket_stream_error", error=str(e))

    def _parse_book(self, data: dict) -> Optional[PriceUpdate]:
        """Parse orderbook message into PriceUpdate."""
        try:
            asset_id = data.get("asset_id", "")
            market_info = self._market_info.get(asset_id, {})

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            yes_bid = yes_ask = no_bid = no_ask = None
            yes_bid_size = yes_ask_size = no_bid_size = no_ask_size = None

            if bids:
                sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
                if sorted_bids:
                    yes_bid = Decimal(str(sorted_bids[0]["price"]))
                    yes_bid_size = Decimal(str(sorted_bids[0].get("size", 0)))

            if asks:
                sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
                if sorted_asks:
                    yes_ask = Decimal(str(sorted_asks[0]["price"]))
                    yes_ask_size = Decimal(str(sorted_asks[0].get("size", 0)))

            if yes_bid is not None:
                no_ask = Decimal("1") - yes_bid
            if yes_ask is not None:
                no_bid = Decimal("1") - yes_ask

            return PriceUpdate(
                platform="polymarket",
                market_id=asset_id,
                event_id=market_info.get("event_id", asset_id[:16]),
                title=market_info.get("title", asset_id[:32]),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                yes_bid_size=yes_bid_size,
                yes_ask_size=yes_ask_size,
                timestamp=datetime.utcnow(),
                raw_data=data,
            )
        except Exception as e:
            logger.error("polymarket_parse_error", error=str(e), data=data)
            return None


async def test_polymarket_feed():
    """Test the Polymarket feed with a sample market."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": 5}
        )
        markets = resp.json()

    if not markets:
        print("No markets found")
        return

    market_data = []
    for m in markets[:3]:
        for token in m.get("tokens", []):
            market_data.append({
                "token_id": token.get("token_id"),
                "title": m.get("question", "")[:50],
                "event_id": m.get("condition_id", ""),
            })

    if not market_data:
        print("No tokens found")
        return

    print(f"Testing with {len(market_data)} tokens")

    async def on_update(update: PriceUpdate):
        print(f"[{update.timestamp}] {update.title[:30]}: "
              f"YES {update.yes_bid}/{update.yes_ask}")

    feed = PolymarketFeed(callback=on_update)
    await feed.connect()
    await feed.subscribe_with_info(market_data)

    try:
        count = 0
        async for update in feed.stream():
            print(f"Update: {update.market_id[:16]}... YES={update.yes_bid}")
            count += 1
            if count >= 10:
                break
    finally:
        await feed.disconnect()


if __name__ == "__main__":
    asyncio.run(test_polymarket_feed())
