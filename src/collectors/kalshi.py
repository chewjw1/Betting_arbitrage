"""Kalshi API collector.

Kalshi API Documentation: https://docs.kalshi.com/welcome
"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import BaseCollector, MarketData
from src.config import get_settings


class KalshiCollector(BaseCollector):
    """Collector for Kalshi prediction market API."""

    platform_name = "kalshi"

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self.client: Optional[httpx.AsyncClient] = None
        self.token: Optional[str] = None
        self.token_expires: Optional[datetime] = None
        self._private_key: Optional[rsa.RSAPrivateKey] = None

    async def connect(self) -> None:
        """Initialize HTTP client.

        Note: The markets endpoint is public and doesn't need auth.
        Auth is only needed for trading/portfolio endpoints.
        """
        self.client = httpx.AsyncClient(
            base_url=self.settings.kalshi_api_host,
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        self.logger.info("Connected to Kalshi API (read-only)")

    async def disconnect(self) -> None:
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    def _load_private_key(self) -> rsa.RSAPrivateKey:
        """Load RSA private key from file."""
        if self._private_key is None:
            key_path = Path(self.settings.kalshi_private_key_path)
            if not key_path.exists():
                raise FileNotFoundError(
                    f"Kalshi private key not found at {key_path}. "
                    "Generate an API key at https://kalshi.com and download the private key."
                )
            with open(key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(
                    f.read(),
                    password=None,
                )
        return self._private_key

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def _authenticate(self) -> None:
        """Authenticate with Kalshi API using RSA signature."""
        if not self.settings.kalshi_api_key:
            self.logger.warning("No Kalshi API key configured, running in read-only mode")
            return

        # For RSA authentication, we need to sign a timestamp
        import base64
        import time

        timestamp = str(int(time.time() * 1000))
        method = "GET"
        path = "/trade-api/v2/portfolio/balance"

        # Create message to sign
        message = f"{timestamp}{method}{path}"
        private_key = self._load_private_key()

        # Sign with RSA-PSS
        signature = private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        signature_b64 = base64.b64encode(signature).decode()

        # Set authentication headers for subsequent requests
        self.client.headers.update(
            {
                "KALSHI-ACCESS-KEY": self.settings.kalshi_api_key,
                "KALSHI-ACCESS-SIGNATURE": signature_b64,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
            }
        )

        self.logger.info("Authenticated with Kalshi API")

    def _parse_market(self, data: dict[str, Any]) -> MarketData:
        """Parse Kalshi API market response into MarketData."""
        # Extract prices - Kalshi uses cents (0-100)
        yes_price = None
        no_price = None

        if "yes_bid" in data and data["yes_bid"]:
            yes_price = Decimal(str(data["yes_bid"])) / 100
        elif "last_price" in data and data["last_price"]:
            yes_price = Decimal(str(data["last_price"])) / 100

        if yes_price is not None:
            no_price = Decimal("1") - yes_price

        # Parse end date
        end_date = None
        if data.get("close_time"):
            try:
                end_date = datetime.fromisoformat(
                    data["close_time"].replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        return MarketData(
            platform=self.platform_name,
            platform_market_id=data.get("ticker", data.get("id", "")),
            title=data.get("title", ""),
            description=data.get("subtitle", data.get("rules_primary", "")),
            resolution_criteria=data.get("rules_primary"),
            category=data.get("category"),
            end_date=end_date,
            status=data.get("status", "open"),
            url=f"https://kalshi.com/markets/{data.get('ticker', '')}",
            yes_price=yes_price,
            no_price=no_price,
            yes_bid=Decimal(str(data["yes_bid"])) / 100 if data.get("yes_bid") else None,
            yes_ask=Decimal(str(data["yes_ask"])) / 100 if data.get("yes_ask") else None,
            no_bid=Decimal(str(data["no_bid"])) / 100 if data.get("no_bid") else None,
            no_ask=Decimal(str(data["no_ask"])) / 100 if data.get("no_ask") else None,
            total_volume=Decimal(str(data["volume"])) if data.get("volume") else None,
        )

    async def fetch_markets(
        self,
        category: Optional[str] = None,
        max_markets: int = 1000,
    ) -> list[MarketData]:
        """Fetch available markets from Kalshi.

        Args:
            category: Optional category filter (e.g., 'Politics', 'Economics').
            max_markets: Maximum markets to fetch (default 1000).

        Returns:
            List of MarketData objects.
        """
        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        markets = []
        cursor = None

        while len(markets) < max_markets:
            params = {"limit": 100, "status": "open"}
            if category:
                params["category"] = category
            if cursor:
                params["cursor"] = cursor

            try:
                response = await self.client.get("/trade-api/v2/markets", params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                self.logger.warning(
                    "Kalshi API error",
                    status_code=e.response.status_code,
                    page=len(markets) // 100 + 1,
                    markets_so_far=len(markets),
                )
                # Return what we have so far instead of failing completely
                if markets:
                    break
                raise

            data = response.json()

            for market in data.get("markets", []):
                try:
                    markets.append(self._parse_market(market))
                except Exception as e:
                    self.logger.warning(
                        "Failed to parse market",
                        market_id=market.get("ticker"),
                        error=str(e),
                    )

            cursor = data.get("cursor")
            if not cursor:
                break

            # Small delay between pages to avoid rate limiting
            import asyncio
            await asyncio.sleep(0.3)

        self.logger.info("Fetched markets from Kalshi", count=len(markets))
        return markets

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def fetch_market(self, market_id: str) -> Optional[MarketData]:
        """Fetch a specific market by ticker.

        Args:
            market_id: Kalshi market ticker (e.g., 'KXBTC-24DEC31').

        Returns:
            MarketData if found, None otherwise.
        """
        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        try:
            response = await self.client.get(f"/trade-api/v2/markets/{market_id}")
            response.raise_for_status()
            data = response.json()
            return self._parse_market(data.get("market", data))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def fetch_orderbook(self, market_id: str) -> dict:
        """Fetch orderbook for a specific market.

        Args:
            market_id: Kalshi market ticker.

        Returns:
            Orderbook data with bids and asks.
        """
        if not self.client:
            raise RuntimeError("Collector not connected. Call connect() first.")

        response = await self.client.get(f"/trade-api/v2/markets/{market_id}/orderbook")
        response.raise_for_status()
        return response.json()
