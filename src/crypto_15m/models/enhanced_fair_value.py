"""Enhanced fair value calculator with multiple signal sources.

Combines:
1. Base probability (price distance + volatility)
2. Order flow signals (aggressive buying/selling)
3. Funding rate signals (market positioning)
4. Momentum signals (recent results)
5. Time decay (confidence increases near expiry)
"""

import math
from dataclasses import dataclass
from typing import Optional
from scipy import stats


@dataclass
class EnhancedFairValueResult:
    """Result of enhanced fair value calculation."""
    fair_value_yes: float
    fair_value_no: float
    confidence: float
    edge_yes: float
    edge_no: float
    signal: str
    strength: float  # 0-1, how strong the signal is
    reasoning: str
    components: dict  # Breakdown of what contributed


class EnhancedFairValueCalculator:
    """Enhanced fair value with multiple signal sources."""

    MIN_EDGE = 0.04  # 4% minimum edge (tighter than before)
    MIN_CONFIDENCE = 0.35

    # Signal weights (sum to 1.0)
    WEIGHTS = {
        "base_prob": 0.35,      # Price distance + vol
        "order_flow": 0.25,    # Aggressive trade flow
        "funding": 0.15,       # Funding rate positioning
        "momentum": 0.15,      # Recent market results
        "time_decay": 0.10,    # Confidence boost near expiry
    }

    def calculate(
        self,
        current_price: float,
        target_price: float,
        minutes_remaining: float,
        volatility_15m: float,
        # Order flow signal
        order_flow_signal: str = "NEUTRAL",
        order_flow_strength: float = 0.0,
        order_flow_delta: float = 0.0,
        # Funding signal
        funding_signal: str = "NEUTRAL",
        funding_strength: float = 0.0,
        funding_rate: float = 0.0,
        # Momentum (recent results)
        recent_results: list[str] = None,
        # Market prices
        market_yes_bid: float = 0.5,
        market_yes_ask: float = 0.5,
    ) -> EnhancedFairValueResult:
        """Calculate enhanced fair value."""

        if minutes_remaining <= 0:
            return self._expired_result(current_price, target_price)

        components = {}

        # 1. Base probability (price distance + volatility)
        base_prob, base_conf = self._calc_base_prob(
            current_price, target_price, minutes_remaining, volatility_15m
        )
        components["base_prob"] = {
            "value": base_prob,
            "confidence": base_conf,
            "weight": self.WEIGHTS["base_prob"],
        }

        # 2. Order flow adjustment
        of_adj = self._calc_order_flow_adj(
            order_flow_signal, order_flow_strength, order_flow_delta
        )
        components["order_flow"] = {
            "adjustment": of_adj,
            "signal": order_flow_signal,
            "strength": order_flow_strength,
            "weight": self.WEIGHTS["order_flow"],
        }

        # 3. Funding rate adjustment
        fund_adj = self._calc_funding_adj(
            funding_signal, funding_strength, funding_rate
        )
        components["funding"] = {
            "adjustment": fund_adj,
            "signal": funding_signal,
            "rate": funding_rate,
            "weight": self.WEIGHTS["funding"],
        }

        # 4. Momentum adjustment
        mom_adj = self._calc_momentum_adj(recent_results or [])
        components["momentum"] = {
            "adjustment": mom_adj,
            "recent": recent_results[:3] if recent_results else [],
            "weight": self.WEIGHTS["momentum"],
        }

        # 5. Time decay confidence boost
        time_conf = self._calc_time_confidence(minutes_remaining)
        components["time_decay"] = {
            "confidence_boost": time_conf,
            "minutes_left": minutes_remaining,
            "weight": self.WEIGHTS["time_decay"],
        }

        # Combine adjustments
        total_adj = (
            of_adj * self.WEIGHTS["order_flow"] +
            fund_adj * self.WEIGHTS["funding"] +
            mom_adj * self.WEIGHTS["momentum"]
        )

        # Final probability = base + adjustments
        fair_value_yes = base_prob + total_adj
        fair_value_yes = max(0.02, min(0.98, fair_value_yes))

        # Confidence combines base confidence and time confidence
        confidence = (
            base_conf * self.WEIGHTS["base_prob"] +
            time_conf * self.WEIGHTS["time_decay"]
        )

        # Add confidence from strong signals
        if order_flow_strength > 0.5:
            confidence += 0.1
        if funding_strength > 0.5:
            confidence += 0.05

        confidence = min(0.95, confidence)

        # Calculate edge
        edge_yes = fair_value_yes - market_yes_ask
        edge_no = (1 - fair_value_yes) - (1 - market_yes_bid)

        # Generate signal
        signal, strength, reasoning = self._generate_signal(
            fair_value_yes, edge_yes, edge_no, confidence,
            market_yes_bid, market_yes_ask, components
        )

        return EnhancedFairValueResult(
            fair_value_yes=fair_value_yes,
            fair_value_no=1 - fair_value_yes,
            confidence=confidence,
            edge_yes=edge_yes,
            edge_no=edge_no,
            signal=signal,
            strength=strength,
            reasoning=reasoning,
            components=components,
        )

    def _calc_base_prob(
        self, current: float, target: float, mins_left: float, vol: float
    ) -> tuple[float, float]:
        """Calculate base probability from price distance and volatility."""
        distance_pct = (current - target) / target
        time_ratio = mins_left / 15.0
        expected_vol = vol * math.sqrt(time_ratio)

        if expected_vol < 0.0001:
            expected_vol = 0.001

        z_score = distance_pct / expected_vol
        base_prob = stats.norm.cdf(z_score)

        # Confidence increases with |z_score| and as time passes
        distance_conf = min(1.0, abs(z_score) / 2)
        time_conf = 1 - time_ratio
        confidence = (distance_conf + time_conf) / 2

        return base_prob, confidence

    def _calc_order_flow_adj(
        self, signal: str, strength: float, delta: float
    ) -> float:
        """Calculate adjustment from order flow."""
        if signal == "NEUTRAL" or strength < 0.2:
            return 0.0

        # Max adjustment of +/- 15%
        base_adj = 0.15 * strength

        if signal == "BULLISH":
            return base_adj
        elif signal == "BEARISH":
            return -base_adj

        return 0.0

    def _calc_funding_adj(
        self, signal: str, strength: float, rate: float
    ) -> float:
        """Calculate adjustment from funding rate.

        Contrarian: high funding = bearish (crowded longs will unwind)
        """
        if signal == "NEUTRAL" or strength < 0.2:
            return 0.0

        # Max adjustment of +/- 10%
        base_adj = 0.10 * strength

        if signal == "BULLISH":  # Crowded shorts
            return base_adj
        elif signal == "BEARISH":  # Crowded longs
            return -base_adj

        return 0.0

    def _calc_momentum_adj(self, recent_results: list[str]) -> float:
        """Calculate momentum adjustment from recent results."""
        if len(recent_results) < 1:
            return 0.0

        # 2 in a row = momentum
        if len(recent_results) >= 2:
            if recent_results[0] == "yes" and recent_results[1] == "yes":
                return 0.12  # 65% continuation = +15% edge, scaled
            if recent_results[0] == "no" and recent_results[1] == "no":
                return -0.05  # Slight mean reversion expected

        # 1 result
        if recent_results[0] == "yes":
            return 0.05
        elif recent_results[0] == "no":
            return -0.02

        return 0.0

    def _calc_time_confidence(self, mins_left: float) -> float:
        """Calculate confidence boost from time remaining.

        Less time = less uncertainty = higher confidence
        """
        if mins_left <= 2:
            return 0.9  # Very high confidence near expiry
        elif mins_left <= 5:
            return 0.7
        elif mins_left <= 10:
            return 0.5
        else:
            return 0.3

    def _generate_signal(
        self, fair_yes: float, edge_yes: float, edge_no: float,
        confidence: float, bid: float, ask: float, components: dict
    ) -> tuple[str, float, str]:
        """Generate trading signal."""

        reasons = []

        # Check for edge
        if edge_yes > self.MIN_EDGE and confidence > self.MIN_CONFIDENCE:
            signal = "BUY_YES"
            strength = min(1.0, edge_yes / 0.15)  # Normalize to 15% max

            reasons.append(f"Fair: {fair_yes*100:.0f}c vs ask: {ask*100:.0f}c")
            reasons.append(f"Edge: {edge_yes*100:.1f}%")

            # Add component contributions
            if components["order_flow"]["adjustment"] > 0.05:
                reasons.append(f"Order flow: {components['order_flow']['signal']}")
            if components["momentum"]["adjustment"] > 0.05:
                reasons.append(f"Momentum: +{components['momentum']['adjustment']*100:.0f}%")

        elif edge_no > self.MIN_EDGE and confidence > self.MIN_CONFIDENCE:
            signal = "BUY_NO"
            strength = min(1.0, edge_no / 0.15)

            reasons.append(f"Fair NO: {(1-fair_yes)*100:.0f}c vs ask: {(1-bid)*100:.0f}c")
            reasons.append(f"Edge: {edge_no*100:.1f}%")

            if components["order_flow"]["adjustment"] < -0.05:
                reasons.append(f"Order flow: {components['order_flow']['signal']}")
            if components["funding"]["adjustment"] < -0.05:
                reasons.append(f"Funding: bearish positioning")

        else:
            signal = "NO_TRADE"
            strength = 0.0
            reasons.append(f"Edge thin (YES: {edge_yes*100:+.1f}%, NO: {edge_no*100:+.1f}%)")
            if confidence < self.MIN_CONFIDENCE:
                reasons.append(f"Low confidence: {confidence*100:.0f}%")

        return signal, strength, " | ".join(reasons)

    def _expired_result(self, current: float, target: float) -> EnhancedFairValueResult:
        """Return result for expired market."""
        is_yes = current >= target
        return EnhancedFairValueResult(
            fair_value_yes=1.0 if is_yes else 0.0,
            fair_value_no=0.0 if is_yes else 1.0,
            confidence=1.0,
            edge_yes=0, edge_no=0,
            signal="NO_TRADE", strength=0,
            reasoning="Market expired",
            components={},
        )


def main():
    """Test enhanced calculator."""
    print("=" * 70)
    print("ENHANCED FAIR VALUE CALCULATOR TEST")
    print("=" * 70)

    calc = EnhancedFairValueCalculator()

    # Scenario: Strong buy signal
    result = calc.calculate(
        current_price=81000,
        target_price=80900,  # 0.12% above target
        minutes_remaining=5,
        volatility_15m=0.003,
        order_flow_signal="BULLISH",
        order_flow_strength=0.6,
        order_flow_delta=150000,
        funding_signal="BULLISH",  # Shorts crowded
        funding_strength=0.4,
        funding_rate=-0.0004,
        recent_results=["yes", "yes"],  # 2 UPs
        market_yes_bid=0.55,
        market_yes_ask=0.58,
    )

    print("\nScenario: BTC above target + bullish signals")
    print("-" * 50)
    print(f"  Fair Value YES: {result.fair_value_yes*100:.1f}%")
    print(f"  Market Ask: {0.58*100:.0f}c")
    print(f"  Edge YES: {result.edge_yes*100:+.1f}%")
    print(f"  Confidence: {result.confidence*100:.0f}%")
    print(f"  Signal: {result.signal} (strength: {result.strength:.0%})")
    print(f"  Reasoning: {result.reasoning}")

    print("\n  Components:")
    for name, comp in result.components.items():
        print(f"    {name}: {comp}")


if __name__ == "__main__":
    main()
