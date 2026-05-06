"""Funding rate signals for crypto direction prediction.

Funding rates reveal market positioning:
- High positive funding = longs paying shorts = crowded long → bearish
- High negative funding = shorts paying longs = crowded short → bullish
- Extreme funding often precedes reversals
"""

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()


@dataclass
class FundingSignal:
    """Funding rate signal."""
    current_rate: float      # Current funding rate (e.g., 0.0001 = 0.01%)
    predicted_rate: float    # Predicted next funding
    rate_24h_avg: float      # 24h average
    open_interest: float     # Total open interest in USD
    oi_change_1h: float      # OI change last hour (%)
    signal: str              # "BULLISH", "BEARISH", "NEUTRAL"
    strength: float          # 0-1 signal strength
    reasoning: str


class FundingRateFeed:
    """Track funding rates from Binance Futures."""

    BASE_URL = "https://fapi.binance.com"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._funding_history: list[dict] = []
        self._oi_history: list[dict] = []

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def get_signal(self, symbol: str = "BTCUSDT") -> FundingSignal:
        """Get funding rate signal."""
        try:
            # Get current funding rate
            funding_resp = await self._client.get(
                f"{self.BASE_URL}/fapi/v1/premiumIndex",
                params={"symbol": symbol}
            )
            if funding_resp.status_code != 200:
                return self._neutral_signal("API error")

            funding_data = funding_resp.json()
            current_rate = float(funding_data.get("lastFundingRate", 0))
            predicted_rate = float(funding_data.get("interestRate", 0))

            # Get funding history for 24h average
            history_resp = await self._client.get(
                f"{self.BASE_URL}/fapi/v1/fundingRate",
                params={"symbol": symbol, "limit": 8}  # 8 x 8hr = ~24h
            )
            if history_resp.status_code == 200:
                rates = [float(r["fundingRate"]) for r in history_resp.json()]
                rate_24h_avg = sum(rates) / len(rates) if rates else 0
            else:
                rate_24h_avg = current_rate

            # Get open interest
            oi_resp = await self._client.get(
                f"{self.BASE_URL}/fapi/v1/openInterest",
                params={"symbol": symbol}
            )
            if oi_resp.status_code == 200:
                oi_data = oi_resp.json()
                open_interest = float(oi_data.get("openInterest", 0))
            else:
                open_interest = 0

            # Track OI changes
            now = datetime.now(timezone.utc)
            self._oi_history.append({"oi": open_interest, "time": now})
            self._oi_history = [
                x for x in self._oi_history
                if (now - x["time"]).total_seconds() < 3600
            ]

            # Calculate OI change
            if len(self._oi_history) > 1:
                old_oi = self._oi_history[0]["oi"]
                oi_change_1h = (open_interest - old_oi) / old_oi if old_oi > 0 else 0
            else:
                oi_change_1h = 0

            # Generate signal
            signal, strength, reasoning = self._classify_signal(
                current_rate, rate_24h_avg, oi_change_1h
            )

            return FundingSignal(
                current_rate=current_rate,
                predicted_rate=predicted_rate,
                rate_24h_avg=rate_24h_avg,
                open_interest=open_interest,
                oi_change_1h=oi_change_1h,
                signal=signal,
                strength=strength,
                reasoning=reasoning,
            )

        except Exception as e:
            logger.error("funding_fetch_error", error=str(e))
            return self._neutral_signal(f"Error: {str(e)}")

    def _classify_signal(
        self, current: float, avg_24h: float, oi_change: float
    ) -> tuple[str, float, str]:
        """Classify funding into trading signal.

        Logic:
        - Very high funding (>0.05% per 8h = 0.15%/day) → crowded longs → BEARISH
        - Very negative funding (<-0.03%) → crowded shorts → BULLISH
        - Funding + rising OI = position building in that direction
        - Funding + falling OI = positions closing (trend exhaustion)
        """
        reasons = []
        bullish_score = 0
        bearish_score = 0

        # Current funding level
        if current > 0.0005:  # >0.05% = very high
            bearish_score += 0.4
            reasons.append(f"Very high funding ({current*100:.3f}%) = crowded longs")
        elif current > 0.0003:  # >0.03% = elevated
            bearish_score += 0.2
            reasons.append(f"Elevated funding ({current*100:.3f}%)")
        elif current < -0.0003:  # <-0.03% = negative
            bullish_score += 0.3
            reasons.append(f"Negative funding ({current*100:.3f}%) = crowded shorts")
        elif current < -0.0005:
            bullish_score += 0.4
            reasons.append(f"Very negative funding ({current*100:.3f}%)")

        # Funding vs 24h average (trend in positioning)
        if current > avg_24h * 1.5 and current > 0.0002:
            bearish_score += 0.2
            reasons.append("Funding rising = more longs entering")
        elif current < avg_24h * 0.5 and current < -0.0001:
            bullish_score += 0.2
            reasons.append("Funding falling = shorts entering")

        # OI changes
        if oi_change > 0.02:  # OI up >2%
            if current > 0.0002:
                bearish_score += 0.15
                reasons.append("Rising OI + positive funding = long buildup")
            elif current < -0.0001:
                bullish_score += 0.15
                reasons.append("Rising OI + negative funding = short buildup")
        elif oi_change < -0.02:  # OI down >2%
            if current > 0.0003:
                bullish_score += 0.2
                reasons.append("Falling OI + high funding = longs closing (bullish)")

        # Determine signal
        if bullish_score > bearish_score and bullish_score > 0.25:
            return "BULLISH", min(1.0, bullish_score), "; ".join(reasons)
        elif bearish_score > bullish_score and bearish_score > 0.25:
            return "BEARISH", min(1.0, bearish_score), "; ".join(reasons)
        else:
            return "NEUTRAL", 0.0, "Funding neutral"

    def _neutral_signal(self, reason: str) -> FundingSignal:
        """Return neutral signal."""
        return FundingSignal(
            current_rate=0, predicted_rate=0, rate_24h_avg=0,
            open_interest=0, oi_change_1h=0,
            signal="NEUTRAL", strength=0, reasoning=reason
        )


async def main():
    """Test funding rate feed."""
    print("=" * 70)
    print("FUNDING RATE MONITOR")
    print("=" * 70)

    async with FundingRateFeed() as feed:
        signal = await feed.get_signal("BTCUSDT")

        print(f"\nBTCUSDT Funding Rate Signal:")
        print(f"  Current Rate: {signal.current_rate*100:.4f}%")
        print(f"  24h Average: {signal.rate_24h_avg*100:.4f}%")
        print(f"  Open Interest: ${signal.open_interest:,.0f}")
        print(f"  OI Change 1h: {signal.oi_change_1h*100:+.2f}%")
        print(f"  Signal: {signal.signal} ({signal.strength:.0%})")
        print(f"  Reasoning: {signal.reasoning}")

        # Annualized funding
        annual_rate = signal.current_rate * 3 * 365  # 3 fundings per day
        print(f"\n  Annualized: {annual_rate*100:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
