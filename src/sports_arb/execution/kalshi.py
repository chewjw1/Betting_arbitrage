"""Kalshi trade execution via REST API."""

import hashlib
import hmac
import time
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

KALSHI_API_URL = "https://trading-api.kalshi.com/trade-api/v2"
KALSHI_ELECTIONS_API_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiExecutor(BaseExecutor):
    """Execute trades on Kalshi."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        private_key: Optional[str] = None,
        paper_mode: bool = True,
        use_elections_api: bool = True,
    ):
        super().__init__(paper_mode)
        self.api_key = api_key
        self.private_key = private_key
        self.api_url = KALSHI_ELECTIONS_API_URL if use_elections_api else KALSHI_API_URL
        self._client = None
        self._authenticated = False

    @property
    def platform(self) -> str:
        return "kalshi"

    def _sign_request(
        self,
        method: str,
        path: str,
        timestamp: int,
    ) -> str:
        """Sign a request using HMAC-SHA256."""
        if not self.private_key:
            raise ValueError("Private key required for signing")

        message = f"{timestamp}{method}{path}"
        signature = hmac.new(
            self.private_key.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return signature

    def _get_headers(self, method: str, path: str) -> dict:
        """Get authenticated headers for a request."""
        timestamp = int(time.time() * 1000)
        headers = {
            "Content-Type": "application/json",
        }

        if self.api_key and self.private_key:
            signature = self._sign_request(method, path, timestamp)
            headers.update({
                "KALSHI-ACCESS-KEY": self.api_key,
                "KALSHI-ACCESS-SIGNATURE": signature,
                "KALSHI-ACCESS-TIMESTAMP": str(timestamp),
            })

        return headers

    async def connect(self) -> None:
        """Initialize HTTP client."""
        import httpx

        self._client = httpx.AsyncClient(
            base_url=self.api_url,
            timeout=10.0,
        )

        if self.api_key and self.private_key:
            try:
                resp = await self._client.get(
                    "/portfolio/balance",
                    headers=self._get_headers("GET", "/trade-api/v2/portfolio/balance"),
                )
                if resp.status_code == 200:
                    self._authenticated = True
                    logger.info("kalshi_authenticated")
                else:
                    logger.warning("kalshi_auth_failed", status=resp.status_code)
            except Exception as e:
                logger.error("kalshi_connect_error", error=str(e))
        else:
            logger.info("kalshi_paper_mode", paper=self.paper_mode)

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def place_order(self, order: Order) -> OrderResult:
        """Place an order on Kalshi."""
        if self.paper_mode:
            fill_price = order.price or Decimal("0.50")
            result = self.record_paper_trade(order, fill_price)
            logger.info(
                "kalshi_paper_order",
                market=order.market_id,
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

        path = "/trade-api/v2/portfolio/orders"
        payload = {
            "ticker": order.market_id,
            "action": "buy" if order.side == OrderSide.BUY else "sell",
            "type": "limit" if order.order_type == OrderType.LIMIT else "market",
            "count": order.quantity,
            "side": "yes" if order.is_yes else "no",
        }

        if order.order_type == OrderType.LIMIT and order.price:
            payload["yes_price"] = int(order.price * 100)

        if order.client_order_id:
            payload["client_order_id"] = order.client_order_id

        try:
            resp = await self._client.post(
                "/portfolio/orders",
                json=payload,
                headers=self._get_headers("POST", path),
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                order_data = data.get("order", {})
                return OrderResult(
                    success=True,
                    order_id=order_data.get("order_id"),
                    client_order_id=order_data.get("client_order_id"),
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
            logger.error("kalshi_order_error", error=str(e))
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

        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        try:
            resp = await self._client.delete(
                f"/portfolio/orders/{order_id}",
                headers=self._get_headers("DELETE", path),
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error("kalshi_cancel_error", error=str(e))
            return False

    async def get_balance(self) -> Decimal:
        """Get available balance."""
        if self.paper_mode:
            return Decimal("10000.00")

        if not self._authenticated:
            return Decimal("0")

        path = "/trade-api/v2/portfolio/balance"
        try:
            resp = await self._client.get(
                "/portfolio/balance",
                headers=self._get_headers("GET", path),
            )
            if resp.status_code == 200:
                data = resp.json()
                return Decimal(str(data.get("balance", 0))) / 100
        except Exception as e:
            logger.error("kalshi_balance_error", error=str(e))

        return Decimal("0")

    async def get_positions(self) -> list[Position]:
        """Get current positions."""
        if self.paper_mode:
            return list(self._positions.values())

        if not self._authenticated:
            return []

        path = "/trade-api/v2/portfolio/positions"
        try:
            resp = await self._client.get(
                "/portfolio/positions",
                headers=self._get_headers("GET", path),
            )
            if resp.status_code == 200:
                data = resp.json()
                positions = []
                for p in data.get("market_positions", []):
                    positions.append(Position(
                        market_id=p.get("ticker", ""),
                        yes_quantity=p.get("position", 0) if p.get("position", 0) > 0 else 0,
                        no_quantity=abs(p.get("position", 0)) if p.get("position", 0) < 0 else 0,
                    ))
                return positions
        except Exception as e:
            logger.error("kalshi_positions_error", error=str(e))

        return []
