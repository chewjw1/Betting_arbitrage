"""Trade execution interfaces."""

from .base import BaseExecutor, Order, OrderResult, OrderSide, OrderType
from .kalshi import KalshiExecutor
from .polymarket import PolymarketExecutor

__all__ = [
    "BaseExecutor",
    "Order",
    "OrderResult",
    "OrderSide",
    "OrderType",
    "KalshiExecutor",
    "PolymarketExecutor",
]
