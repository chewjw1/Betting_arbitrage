"""Fair value calculator for 15-minute binary options.

Uses a simplified Black-Scholes-ish model adjusted for:
1. Current price vs target
2. Time remaining
3. Recent volatility
4. Momentum signals
"""

import math
from decimal import Decimal
from dataclasses import dataclass
from typing import Optional
from scipy import stats


@dataclass
class FairValueResult:
    """Result of fair value calculation."""
    fair_value_yes: float  # 0-1, fair price for YES
    fair_value_no: float   # 0-1, fair price for NO
    confidence: float      # 0-1, model confidence
    edge_yes: float        # Edge if buying YES at market
    edge_no: float         # Edge if buying NO at market
    signal: str            # "BUY_YES", "BUY_NO", or "NO_TRADE"
    reasoning: str


class FairValueCalculator:
    """Calculate fair value for 15-minute binary options."""

    # Momentum adjustments based on historical data
    # After 2 UPs: 62% continuation → +12% adjustment
    # After 2 DOWNs: 52% reversal → +2% adjustment to UP
    MOMENTUM_ADJUSTMENTS = {
        "strong_up": 0.12,    # After 2+ UPs
        "weak_up": 0.05,      # After 1 UP
        "neutral": 0.0,       # Mixed
        "weak_down": -0.02,   # After 1 DOWN
        "strong_down": -0.05, # After 2+ DOWNs (slight mean reversion)
    }

    # Minimum edge to trade
    MIN_EDGE = 0.05  # 5%

    def __init__(self):
        pass

    def calculate(
        self,
        current_price: float,
        target_price: float,
        minutes_remaining: float,
        volatility_15m: float,
        recent_results: list[str] = None,
        market_yes_bid: float = 0.5,
        market_yes_ask: float = 0.5,
    ) -> FairValueResult:
        """
        Calculate fair value for a 15-minute binary.

        Args:
            current_price: Current BTC price
            target_price: Strike price (starting price of window)
            minutes_remaining: Minutes until market closes
            volatility_15m: Realized volatility over last 15 min
            recent_results: List of recent results ["yes", "no", ...]
            market_yes_bid: Current bid for YES
            market_yes_ask: Current ask for YES
        """
        # Step 1: Calculate base probability using normal distribution
        # Assume price follows random walk with given volatility

        if minutes_remaining <= 0:
            # Market closed
            return FairValueResult(
                fair_value_yes=1.0 if current_price >= target_price else 0.0,
                fair_value_no=0.0 if current_price >= target_price else 1.0,
                confidence=1.0,
                edge_yes=0,
                edge_no=0,
                signal="NO_TRADE",
                reasoning="Market closed",
            )

        # Distance from target as percentage
        distance_pct = (current_price - target_price) / target_price

        # Expected volatility for remaining time
        # Scale 15-min vol to remaining time
        time_ratio = minutes_remaining / 15.0
        expected_vol = volatility_15m * math.sqrt(time_ratio)

        # Avoid division by zero
        if expected_vol < 0.0001:
            expected_vol = 0.001  # Minimum vol assumption

        # Z-score: how many standard deviations is current price from target?
        z_score = distance_pct / expected_vol

        # Base probability using normal CDF
        # P(price > target at expiry) ≈ P(price stays above or drifts up)
        # Simplified: current position + random walk
        base_prob_yes = stats.norm.cdf(z_score)

        # Step 2: Apply momentum adjustment
        momentum_signal = self._get_momentum_signal(recent_results or [])
        momentum_adj = self.MOMENTUM_ADJUSTMENTS.get(momentum_signal, 0)

        # Adjusted probability
        adj_prob_yes = base_prob_yes + momentum_adj
        adj_prob_yes = max(0.01, min(0.99, adj_prob_yes))  # Clamp

        # Step 3: Calculate confidence
        # Higher confidence when:
        # - More time has passed (less uncertainty)
        # - Price is far from target
        # - Vol is stable

        time_confidence = 1 - time_ratio  # Higher as time passes
        distance_confidence = min(1.0, abs(z_score) / 2)  # Higher when far from target
        confidence = (time_confidence + distance_confidence) / 2

        # Step 4: Calculate edge vs market
        market_mid = (market_yes_bid + market_yes_ask) / 2
        edge_yes = adj_prob_yes - market_yes_ask  # Edge if buying YES at ask
        edge_no = (1 - adj_prob_yes) - (1 - market_yes_bid)  # Edge if buying NO

        # Step 5: Generate signal
        if edge_yes > self.MIN_EDGE and confidence > 0.3:
            signal = "BUY_YES"
            reasoning = (
                f"Model: {adj_prob_yes*100:.1f}% YES, Market: {market_yes_ask*100:.1f}c ask. "
                f"Edge: {edge_yes*100:.1f}%. "
                f"Price ${current_price:,.0f} vs target ${target_price:,.0f}. "
                f"Momentum: {momentum_signal}."
            )
        elif edge_no > self.MIN_EDGE and confidence > 0.3:
            signal = "BUY_NO"
            reasoning = (
                f"Model: {(1-adj_prob_yes)*100:.1f}% NO, Market: {(1-market_yes_bid)*100:.1f}c ask. "
                f"Edge: {edge_no*100:.1f}%. "
                f"Price ${current_price:,.0f} vs target ${target_price:,.0f}. "
                f"Momentum: {momentum_signal}."
            )
        else:
            signal = "NO_TRADE"
            reasoning = f"Edge too thin. YES edge: {edge_yes*100:.1f}%, NO edge: {edge_no*100:.1f}%"

        return FairValueResult(
            fair_value_yes=adj_prob_yes,
            fair_value_no=1 - adj_prob_yes,
            confidence=confidence,
            edge_yes=edge_yes,
            edge_no=edge_no,
            signal=signal,
            reasoning=reasoning,
        )

    def _get_momentum_signal(self, recent_results: list[str]) -> str:
        """Classify momentum based on recent results."""
        if len(recent_results) < 1:
            return "neutral"

        if len(recent_results) >= 2:
            if recent_results[0] == "yes" and recent_results[1] == "yes":
                return "strong_up"
            if recent_results[0] == "no" and recent_results[1] == "no":
                return "strong_down"

        if recent_results[0] == "yes":
            return "weak_up"
        if recent_results[0] == "no":
            return "weak_down"

        return "neutral"


def main():
    """Test the calculator."""
    print("=" * 70)
    print("FAIR VALUE CALCULATOR TEST")
    print("=" * 70)

    calc = FairValueCalculator()

    # Test scenarios
    scenarios = [
        {
            "name": "Early window, price at target",
            "current_price": 81000,
            "target_price": 81000,
            "minutes_remaining": 14,
            "volatility_15m": 0.003,
            "recent_results": ["yes", "yes"],  # 2 UPs
            "market_yes_bid": 0.48,
            "market_yes_ask": 0.52,
        },
        {
            "name": "Late window, price above target",
            "current_price": 81200,
            "target_price": 81000,
            "minutes_remaining": 2,
            "volatility_15m": 0.002,
            "recent_results": ["no", "yes"],
            "market_yes_bid": 0.85,
            "market_yes_ask": 0.88,
        },
        {
            "name": "Mid window, price below target, high vol",
            "current_price": 80800,
            "target_price": 81000,
            "minutes_remaining": 7,
            "volatility_15m": 0.005,
            "recent_results": ["no", "no"],  # 2 DOWNs
            "market_yes_bid": 0.40,
            "market_yes_ask": 0.45,
        },
    ]

    for s in scenarios:
        print(f"\n{s['name']}")
        print("-" * 50)

        result = calc.calculate(
            current_price=s["current_price"],
            target_price=s["target_price"],
            minutes_remaining=s["minutes_remaining"],
            volatility_15m=s["volatility_15m"],
            recent_results=s["recent_results"],
            market_yes_bid=s["market_yes_bid"],
            market_yes_ask=s["market_yes_ask"],
        )

        print(f"  Fair Value YES: {result.fair_value_yes*100:.1f}%")
        print(f"  Fair Value NO:  {result.fair_value_no*100:.1f}%")
        print(f"  Confidence: {result.confidence*100:.0f}%")
        print(f"  Edge YES: {result.edge_yes*100:+.1f}%")
        print(f"  Edge NO:  {result.edge_no*100:+.1f}%")
        print(f"  Signal: {result.signal}")
        print(f"  Reasoning: {result.reasoning}")


if __name__ == "__main__":
    main()
