"""Base execution interface for trading."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class Order:
    """Order to be placed."""
    market_id: str
    side: OrderSide
    quantity: int
    price: Optional[Decimal] = None
    order_type: OrderType = OrderType.LIMIT
    is_yes: bool = True
    client_order_id: Optional[str] = None

    def __post_init__(self):
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("Limit orders require a price")


@dataclass
class OrderResult:
    """Result of order placement."""
    success: bool
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    filled_price: Optional[Decimal] = None
    fee: Decimal = Decimal("0")
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    raw_response: Optional[dict] = None


@dataclass
class Position:
    """Current position in a market."""
    market_id: str
    yes_quantity: int = 0
    no_quantity: int = 0
    avg_yes_price: Optional[Decimal] = None
    avg_no_price: Optional[Decimal] = None
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")


class BaseExecutor(ABC):
    """Abstract base class for trade execution."""

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self._positions: dict[str, Position] = {}
        self._orders: list[OrderResult] = []

    @property
    @abstractmethod
    def platform(self) -> str:
        """Platform identifier."""
        pass

    @abstractmethod
    async def connect(self) -> None:
        """Initialize connection/authentication."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Cleanup connection."""
        pass

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult:
        """Place an order."""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        pass

    @abstractmethod
    async def get_balance(self) -> Decimal:
        """Get available balance."""
        pass

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Get current positions."""
        pass

    async def place_orders_simultaneously(
        self,
        orders: list[Order],
    ) -> list[OrderResult]:
        """Place multiple orders simultaneously.

        For cross-platform arbitrage, both legs should execute
        as close to simultaneously as possible.
        """
        import asyncio
        results = await asyncio.gather(
            *[self.place_order(order) for order in orders],
            return_exceptions=True,
        )

        processed = []
        for r in results:
            if isinstance(r, Exception):
                processed.append(OrderResult(
                    success=False,
                    status=OrderStatus.FAILED,
                    error=str(r),
                ))
            else:
                processed.append(r)
        return processed

    def record_paper_trade(
        self,
        order: Order,
        fill_price: Decimal,
    ) -> OrderResult:
        """Record a paper trade for simulation."""
        result = OrderResult(
            success=True,
            order_id=f"PAPER-{datetime.utcnow().timestamp()}",
            client_order_id=order.client_order_id,
            status=OrderStatus.FILLED,
            filled_quantity=order.quantity,
            filled_price=fill_price,
            fee=Decimal("0"),
        )
        self._orders.append(result)

        if order.market_id not in self._positions:
            self._positions[order.market_id] = Position(market_id=order.market_id)

        pos = self._positions[order.market_id]
        qty = order.quantity if order.side == OrderSide.BUY else -order.quantity

        if order.is_yes:
            pos.yes_quantity += qty
        else:
            pos.no_quantity += qty

        return result
