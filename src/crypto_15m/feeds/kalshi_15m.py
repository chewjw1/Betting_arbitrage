"""Kalshi 15-minute crypto market monitor."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger()


@dataclass
class Kalshi15MMarket:
    """A single 15-minute crypto market."""
    ticker: str
    asset: str  # BTC, ETH, etc.
    target_price: Decimal
    close_time: datetime
    yes_bid: Decimal
    yes_ask: Decimal
    no_bid: Decimal
    no_ask: Decimal
    volume: Decimal
    open_interest: Decimal
    status: str

    @property
    def mid_price(self) -> Decimal:
        """Mid-market price for YES."""
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> Decimal:
        """Bid-ask spread in cents."""
        return (self.yes_ask - self.yes_bid) * 100

    @property
    def time_remaining(self) -> float:
        """Seconds until close."""
        now = datetime.now(timezone.utc)
        return max(0, (self.close_time - now).total_seconds())

    @property
    def minutes_remaining(self) -> float:
        """Minutes until close."""
        return self.time_remaining / 60


class Kalshi15MFeed:
    """Monitor Kalshi 15-minute crypto markets."""

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    SERIES = {
        "BTC": "KXBTC15M",
        "ETH": "KXETH15M",
        "SOL": "KXSOL15M",
        "DOGE": "KXDOGE15M",
    }

    def __init__(self, assets: list[str] = None):
        self.assets = assets or ["BTC"]
        self._markets: dict[str, Kalshi15MMarket] = {}
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def get_active_markets(self) -> list[Kalshi15MMarket]:
        """Fetch all active 15-minute markets."""
        markets = []

        for asset in self.assets:
            series = self.SERIES.get(asset)
            if not series:
                continue

            try:
                resp = await self._client.get(
                    f"{self.BASE_URL}/markets",
                    params={"series_ticker": series, "status": "active", "limit": 5},
                )

                if resp.status_code != 200:
                    continue

                for m in resp.json().get("markets", []):
                    market = self._parse_market(m, asset)
                    if market:
                        markets.append(market)
                        self._markets[market.ticker] = market

            except Exception as e:
                logger.error("kalshi_fetch_error", asset=asset, error=str(e))

        return markets

    def _parse_market(self, data: dict, asset: str) -> Optional[Kalshi15MMarket]:
        """Parse API response into market object."""
        try:
            close_time = datetime.fromisoformat(
                data.get("close_time", "").replace("Z", "+00:00")
            )

            return Kalshi15MMarket(
                ticker=data.get("ticker", ""),
                asset=asset,
                target_price=Decimal(str(data.get("floor_strike", 0))),
                close_time=close_time,
                yes_bid=Decimal(str(data.get("yes_bid_dollars", 0) or 0)),
                yes_ask=Decimal(str(data.get("yes_ask_dollars", 0) or 0)),
                no_bid=Decimal(str(data.get("no_bid_dollars", 0) or 0)),
                no_ask=Decimal(str(data.get("no_ask_dollars", 0) or 0)),
                volume=Decimal(str(data.get("volume_fp", 0) or 0)),
                open_interest=Decimal(str(data.get("open_interest_fp", 0) or 0)),
                status=data.get("status", ""),
            )
        except Exception as e:
            logger.error("parse_error", error=str(e))
            return None

    async def get_recent_results(self, asset: str = "BTC", limit: int = 50) -> list[str]:
        """Get recent market results for momentum analysis."""
        series = self.SERIES.get(asset)
        if not series:
            return []

        try:
            resp = await self._client.get(
                f"{self.BASE_URL}/markets",
                params={"series_ticker": series, "status": "settled", "limit": limit},
            )

            if resp.status_code != 200:
                return []

            return [m.get("result", "") for m in resp.json().get("markets", [])]

        except Exception as e:
            logger.error("results_fetch_error", error=str(e))
            return []


async def main():
    """Test the feed."""
    print("=" * 70)
    print("KALSHI 15-MINUTE MARKET MONITOR")
    print("=" * 70)

    async with Kalshi15MFeed(assets=["BTC", "ETH"]) as feed:
        markets = await feed.get_active_markets()

        print(f"\nFound {len(markets)} active markets:")

        for m in markets:
            print(f"\n{m.ticker}")
            print(f"  Asset: {m.asset}")
            print(f"  Target: ${m.target_price:,.2f}")
            print(f"  Time remaining: {m.minutes_remaining:.1f} min")
            print(f"  YES: {float(m.yes_bid)*100:.0f}c / {float(m.yes_ask)*100:.0f}c")
            print(f"  Spread: {float(m.spread):.1f}c")
            print(f"  Volume: ${float(m.volume):,.0f}")

        # Get recent results for momentum analysis
        print("\n" + "-" * 70)
        results = await feed.get_recent_results("BTC", limit=20)
        print(f"Last 20 BTC results: {' '.join(['↑' if r == 'yes' else '↓' for r in results])}")

        # Calculate momentum signal
        if len(results) >= 2:
            if results[0] == "yes" and results[1] == "yes":
                print("  → MOMENTUM SIGNAL: 2 UPs in a row (62% continuation)")
            elif results[0] == "no" and results[1] == "no":
                print("  → REVERSAL SIGNAL: 2 DOWNs in a row (52% reversal)")


if __name__ == "__main__":
    asyncio.run(main())
