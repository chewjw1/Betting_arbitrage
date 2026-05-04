"""Base WebSocket feed interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Optional, Callable, Awaitable
import asyncio


@dataclass
class PriceUpdate:
    """Real-time price update from a platform."""
    platform: str
    market_id: str
    event_id: str
    title: str
    yes_bid: Optional[Decimal] = None
    yes_ask: Optional[Decimal] = None
    no_bid: Optional[Decimal] = None
    no_ask: Optional[Decimal] = None
    yes_bid_size: Optional[Decimal] = None
    yes_ask_size: Optional[Decimal] = None
    no_bid_size: Optional[Decimal] = None
    no_ask_size: Optional[Decimal] = None
    last_price: Optional[Decimal] = None
    volume_24h: Optional[Decimal] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    raw_data: Optional[dict] = None

    @property
    def best_yes_buy(self) -> Optional[Decimal]:
        """Best price to buy YES (lowest ask)."""
        return self.yes_ask

    @property
    def best_yes_sell(self) -> Optional[Decimal]:
        """Best price to sell YES (highest bid)."""
        return self.yes_bid

    @property
    def best_no_buy(self) -> Optional[Decimal]:
        """Best price to buy NO (lowest ask)."""
        return self.no_ask

    @property
    def best_no_sell(self) -> Optional[Decimal]:
        """Best price to sell NO (highest bid)."""
        return self.no_bid

    def has_tradeable_prices(self) -> bool:
        """Check if we have actionable bid/ask prices."""
        return (self.yes_bid is not None and self.yes_ask is not None) or \
               (self.no_bid is not None and self.no_ask is not None)


PriceCallback = Callable[[PriceUpdate], Awaitable[None]]


class BaseFeed(ABC):
    """Abstract base class for real-time price feeds."""

    def __init__(self, callback: Optional[PriceCallback] = None):
        self.callback = callback
        self._running = False
        self._connected = False
        self._subscribed_markets: set[str] = set()
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

    @property
    @abstractmethod
    def platform(self) -> str:
        """Platform identifier (e.g., 'kalshi', 'polymarket')."""
        pass

    @abstractmethod
    async def connect(self) -> None:
        """Establish WebSocket connection."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close WebSocket connection."""
        pass

    @abstractmethod
    async def subscribe(self, market_ids: list[str]) -> None:
        """Subscribe to price updates for specific markets."""
        pass

    @abstractmethod
    async def unsubscribe(self, market_ids: list[str]) -> None:
        """Unsubscribe from markets."""
        pass

    @abstractmethod
    async def stream(self) -> AsyncIterator[PriceUpdate]:
        """Stream price updates. Yields PriceUpdate objects."""
        pass

    async def run(self) -> None:
        """Main run loop with auto-reconnect."""
        self._running = True
        while self._running:
            try:
                await self.connect()
                self._connected = True
                self._reconnect_delay = 1.0

                async for update in self.stream():
                    if not self._running:
                        break
                    if self.callback:
                        await self.callback(update)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                if self._running:
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2,
                        self._max_reconnect_delay
                    )

        await self.disconnect()

    async def stop(self) -> None:
        """Stop the feed."""
        self._running = False

    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self._connected
