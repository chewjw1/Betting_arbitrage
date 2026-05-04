"""Polymarket trade execution via CLOB API."""

from datetime import datetime
from decimal import Decimal
from typing import Optional
import structlog

from .base import (
    BaseExecutor,
    Order,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)

logger = structlog.get_logger()

POLYMARKET_CLOB_URL = "https://clob.polymarket.com"


class PolymarketExecutor(BaseExecutor):
    """Execute trades on Polymarket CLOB.

    Polymarket uses a CLOB (Central Limit Order Book) with EIP-712
    signatures for order placement. Requires:
    - API key and secret
    - Ethereum wallet for signing
    - USDC balance on Polygon
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        paper_mode: bool = True,
    ):
        super().__init__(paper_mode)
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self._client = None
        self._authenticated = False

    @property
    def platform(self) -> str:
        return "polymarket"

    def _get_headers(self) -> dict:
        """Get authenticated headers."""
        headers = {
            "Content-Type": "application/json",
        }

        if self.api_key and self.api_secret and self.passphrase:
            import time
            timestamp = str(int(time.time()))
            headers.update({
                "POLY-ADDRESS": self.api_key,
                "POLY-SIGNATURE": self.api_secret,
                "POLY-TIMESTAMP": timestamp,
                "POLY-PASSPHRASE": self.passphrase,
            })

        return headers

    async def connect(self) -> None:
        """Initialize HTTP client."""
        import httpx

        self._client = httpx.AsyncClient(
            base_url=POLYMARKET_CLOB_URL,
            timeout=10.0,
        )

        if self.api_key and self.api_secret:
            try:
                resp = await self._client.get(
                    "/auth/api-keys",
                    headers=self._get_headers(),
                )
                if resp.status_code == 200:
                    self._authenticated = True
                    logger.info("polymarket_authenticated")
                else:
                    logger.warning("polymarket_auth_failed", status=resp.status_code)
            except Exception as e:
                logger.error("polymarket_connect_error", error=str(e))
        else:
            logger.info("polymarket_paper_mode", paper=self.paper_mode)

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def place_order(self, order: Order) -> OrderResult:
        """Place an order on Polymarket."""
        if self.paper_mode:
            fill_price = order.price or Decimal("0.50")
            result = self.record_paper_trade(order, fill_price)
            logger.info(
                "polymarket_paper_order",
                market=order.market_id[:16],
                side=order.side.value,
                qty=order.quantity,
                price=str(fill_price),
            )
            return result

        if not self._authenticated:
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error="Not authenticated",
            )

        payload = {
            "tokenID": order.market_id,
            "side": "BUY" if order.side == OrderSide.BUY else "SELL",
            "size": str(order.quantity),
            "type": "GTC",
        }

        if order.order_type == OrderType.LIMIT and order.price:
            payload["price"] = str(order.price)

        try:
            resp = await self._client.post(
                "/order",
                json=payload,
                headers=self._get_headers(),
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                return OrderResult(
                    success=True,
                    order_id=data.get("orderID"),
                    status=OrderStatus.SUBMITTED,
                    raw_response=data,
                )
            else:
                return OrderResult(
                    success=False,
                    status=OrderStatus.REJECTED,
                    error=f"HTTP {resp.status_code}: {resp.text}",
                )

        except Exception as e:
            logger.error("polymarket_order_error", error=str(e))
            return OrderResult(
                success=False,
                status=OrderStatus.FAILED,
                error=str(e),
            )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        if self.paper_mode:
            return True

        if not self._authenticated:
            return False

        try:
            resp = await self._client.delete(
                f"/order/{order_id}",
                headers=self._get_headers(),
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error("polymarket_cancel_error", error=str(e))
            return False

    async def get_balance(self) -> Decimal:
        """Get available USDC balance."""
        if self.paper_mode:
            return Decimal("10000.00")

        return Decimal("0")

    async def get_positions(self) -> list[Position]:
        """Get current positions."""
        if self.paper_mode:
            return list(self._positions.values())

        return []
