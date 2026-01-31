"""Logical arbitrage detection for related prediction markets.

This module detects arbitrage opportunities based on logical relationships
between markets, rather than the same market on different platforms.

Types of logical arbitrage:
1. Mutually exclusive outcomes (should sum to 100%)
2. Temporal relationships (P(X by March) ≤ P(X by June))
3. Conditional probabilities (P(A and B) ≤ min(P(A), P(B)))
4. Subset relationships (P(specific) ≤ P(general))
5. Complement violations (P(Yes) + P(No) should ≈ 100% on same platform)
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

import structlog

from src.arbitrage.fees import PLATFORM_FEES, calculate_total_fees
from src.collectors.base import MarketData

logger = structlog.get_logger()


class RelationshipType(Enum):
    """Types of logical relationships between markets."""

    MUTUALLY_EXCLUSIVE = "mutually_exclusive"  # Outcomes can't both happen
    EXHAUSTIVE = "exhaustive"  # One of them must happen
    TEMPORAL_BEFORE = "temporal_before"  # Earlier deadline implies lower prob
    TEMPORAL_AFTER = "temporal_after"
    SUBSET = "subset"  # One is a subset of another
    COMPLEMENT = "complement"  # Yes + No on same market
    CONDITIONAL = "conditional"  # A and B relationship


@dataclass
class LogicalRelationship:
    """Represents a logical relationship between markets."""

    market_a: MarketData
    market_b: MarketData
    relationship_type: RelationshipType
    confidence: float  # How confident we are this relationship exists
    expected_constraint: str  # Human-readable constraint
    notes: Optional[str] = None


@dataclass
class LogicalArbitrageResult:
    """Result of a logical arbitrage check."""

    relationship: LogicalRelationship
    is_violated: bool
    violation_amount: Decimal  # How much the constraint is violated by
    expected_sum: Optional[Decimal] = None
    actual_sum: Optional[Decimal] = None
    profit_opportunity_pct: Decimal = Decimal("0")
    recommended_action: Optional[str] = None
    estimated_fees: Decimal = Decimal("0")
    net_profit_pct: Decimal = Decimal("0")
    is_profitable: bool = False


class LogicalArbitrageDetector:
    """Detect arbitrage from logical inconsistencies in prediction markets."""

    # Patterns for extracting temporal information
    TEMPORAL_PATTERNS = [
        (r"by (\w+ \d{1,2})", "by_date"),
        (r"before (\w+ \d{1,2})", "before_date"),
        (r"in (\w+) (\d{4})", "in_month"),
        (r"by end of (\d{4})", "by_year_end"),
        (r"by (Q[1-4]) (\d{4})", "by_quarter"),
    ]

    # Patterns for mutually exclusive detection
    EXCLUSIVE_PATTERNS = [
        (r"^Will (.+) be (.+) or (.+)\?$", "or_question"),
        (r"^Who will win (.+)\?$", "winner_question"),
        (r"^Which (.+) will (.+)\?$", "which_question"),
    ]

    def __init__(
        self,
        min_violation_pct: float = 2.0,
        min_net_profit_pct: float = 1.0,
        min_relationship_confidence: float = 0.7,
    ):
        """Initialize detector.

        Args:
            min_violation_pct: Minimum constraint violation to flag.
            min_net_profit_pct: Minimum net profit after fees to alert.
            min_relationship_confidence: Minimum confidence in relationship.
        """
        self.min_violation_pct = min_violation_pct
        self.min_net_profit_pct = min_net_profit_pct
        self.min_relationship_confidence = min_relationship_confidence
        self.logger = logger.bind(component="LogicalArbitrageDetector")

    def find_relationships(
        self,
        markets: list[MarketData],
    ) -> list[LogicalRelationship]:
        """Identify logical relationships between markets.

        Args:
            markets: List of all markets to analyze.

        Returns:
            List of detected LogicalRelationship objects.
        """
        relationships = []

        # Group markets by platform for complement checking
        by_platform = {}
        for m in markets:
            by_platform.setdefault(m.platform, []).append(m)

        # Check for complement violations within each platform
        for platform, platform_markets in by_platform.items():
            for market in platform_markets:
                if market.yes_price and market.no_price:
                    # Create self-relationship for complement check
                    rel = LogicalRelationship(
                        market_a=market,
                        market_b=market,
                        relationship_type=RelationshipType.COMPLEMENT,
                        confidence=1.0,
                        expected_constraint="P(Yes) + P(No) = 1.0",
                    )
                    relationships.append(rel)

        # Find mutually exclusive groups (same event, different outcomes)
        exclusive_groups = self._find_mutually_exclusive_groups(markets)
        for group in exclusive_groups:
            if len(group) >= 2:
                # Create pairwise relationships
                for i, market_a in enumerate(group):
                    for market_b in group[i + 1:]:
                        rel = LogicalRelationship(
                            market_a=market_a,
                            market_b=market_b,
                            relationship_type=RelationshipType.MUTUALLY_EXCLUSIVE,
                            confidence=0.9,
                            expected_constraint="Sum of all outcomes ≤ 1.0",
                        )
                        relationships.append(rel)

                # Also check exhaustive constraint
                if len(group) >= 2:
                    rel = LogicalRelationship(
                        market_a=group[0],
                        market_b=group[-1],
                        relationship_type=RelationshipType.EXHAUSTIVE,
                        confidence=0.8,
                        expected_constraint="Sum of all mutually exclusive outcomes ≈ 1.0",
                        notes=f"Group of {len(group)} markets",
                    )
                    relationships.append(rel)

        # Find temporal relationships
        temporal_rels = self._find_temporal_relationships(markets)
        relationships.extend(temporal_rels)

        # Find subset relationships
        subset_rels = self._find_subset_relationships(markets)
        relationships.extend(subset_rels)

        self.logger.info(
            "Found relationships",
            total=len(relationships),
            complement=sum(1 for r in relationships if r.relationship_type == RelationshipType.COMPLEMENT),
            exclusive=sum(1 for r in relationships if r.relationship_type == RelationshipType.MUTUALLY_EXCLUSIVE),
            temporal=sum(1 for r in relationships if r.relationship_type in (RelationshipType.TEMPORAL_BEFORE, RelationshipType.TEMPORAL_AFTER)),
        )

        return relationships

    def _find_mutually_exclusive_groups(
        self,
        markets: list[MarketData],
    ) -> list[list[MarketData]]:
        """Find groups of mutually exclusive markets.

        E.g., "Trump wins", "Biden wins", "Other wins" for same election.

        Returns:
            List of market groups.
        """
        groups = []

        # Group by normalized base question
        by_base = {}
        for market in markets:
            base = self._extract_base_question(market.title)
            if base:
                by_base.setdefault(base, []).append(market)

        # Filter to groups with multiple markets
        for base, group in by_base.items():
            if len(group) >= 2:
                groups.append(group)

        return groups

    def _extract_base_question(self, title: str) -> Optional[str]:
        """Extract the base question from a market title.

        E.g., "Will Trump win the 2024 election?" -> "win the 2024 election"

        Returns:
            Normalized base question or None.
        """
        # Remove common prefixes
        normalized = title.lower().strip()

        # Remove "Will X..." pattern
        normalized = re.sub(r"^will\s+\w+\s+", "", normalized)

        # Remove question marks
        normalized = normalized.rstrip("?")

        # Remove specific outcome mentions
        normalized = re.sub(r"\b(yes|no|win|lose|pass|fail)\b", "", normalized)

        # Clean up
        normalized = re.sub(r"\s+", " ", normalized).strip()

        if len(normalized) > 10:
            return normalized
        return None

    def _find_temporal_relationships(
        self,
        markets: list[MarketData],
    ) -> list[LogicalRelationship]:
        """Find temporal relationships between markets.

        E.g., "X by March" vs "X by June" - earlier should have lower prob.

        Returns:
            List of temporal relationships.
        """
        relationships = []

        # Extract temporal info from each market
        temporal_markets = []
        for market in markets:
            temporal_info = self._extract_temporal_info(market.title)
            if temporal_info:
                temporal_markets.append((market, temporal_info))

        # Find matching events with different deadlines
        for i, (market_a, time_a) in enumerate(temporal_markets):
            for market_b, time_b in temporal_markets[i + 1:]:
                # Check if same underlying event
                if self._same_underlying_event(market_a.title, market_b.title):
                    # Determine which is earlier
                    if time_a < time_b:
                        rel = LogicalRelationship(
                            market_a=market_a,
                            market_b=market_b,
                            relationship_type=RelationshipType.TEMPORAL_BEFORE,
                            confidence=0.85,
                            expected_constraint=f"P(by {time_a}) ≤ P(by {time_b})",
                        )
                    else:
                        rel = LogicalRelationship(
                            market_a=market_b,
                            market_b=market_a,
                            relationship_type=RelationshipType.TEMPORAL_BEFORE,
                            confidence=0.85,
                            expected_constraint=f"P(by {time_b}) ≤ P(by {time_a})",
                        )
                    relationships.append(rel)

        return relationships

    def _extract_temporal_info(self, title: str) -> Optional[datetime]:
        """Extract temporal deadline from market title.

        Returns:
            Datetime representing the deadline, or None.
        """
        from dateutil import parser as date_parser

        title_lower = title.lower()

        # Try each pattern
        for pattern, _ in self.TEMPORAL_PATTERNS:
            match = re.search(pattern, title_lower)
            if match:
                try:
                    date_str = " ".join(match.groups())
                    return date_parser.parse(date_str, fuzzy=True)
                except Exception:
                    continue

        return None

    def _same_underlying_event(self, title_a: str, title_b: str) -> bool:
        """Check if two titles refer to the same underlying event.

        Returns:
            True if likely same event with different timeframes.
        """
        # Remove temporal info and compare
        def remove_temporal(s: str) -> str:
            s = s.lower()
            for pattern, _ in self.TEMPORAL_PATTERNS:
                s = re.sub(pattern, "", s)
            return re.sub(r"\s+", " ", s).strip()

        cleaned_a = remove_temporal(title_a)
        cleaned_b = remove_temporal(title_b)

        # Simple similarity check
        words_a = set(cleaned_a.split())
        words_b = set(cleaned_b.split())

        if not words_a or not words_b:
            return False

        overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
        return overlap > 0.7

    def _find_subset_relationships(
        self,
        markets: list[MarketData],
    ) -> list[LogicalRelationship]:
        """Find subset relationships.

        E.g., "Team X wins Super Bowl" is subset of "Team X makes playoffs"

        Returns:
            List of subset relationships.
        """
        relationships = []

        # Keywords that indicate subset relationships
        subset_indicators = [
            ("wins championship", "makes playoffs"),
            ("wins election", "is nominee"),
            ("wins super bowl", "wins conference"),
            ("wins world series", "wins pennant"),
        ]

        for market_a in markets:
            title_a_lower = market_a.title.lower()

            for market_b in markets:
                if market_a == market_b:
                    continue

                title_b_lower = market_b.title.lower()

                # Check for known subset patterns
                for specific, general in subset_indicators:
                    if specific in title_a_lower and general in title_b_lower:
                        # Check if same subject
                        if self._same_subject(title_a_lower, title_b_lower):
                            rel = LogicalRelationship(
                                market_a=market_a,  # More specific
                                market_b=market_b,  # More general
                                relationship_type=RelationshipType.SUBSET,
                                confidence=0.8,
                                expected_constraint=f"P({specific}) ≤ P({general})",
                            )
                            relationships.append(rel)

        return relationships

    def _same_subject(self, title_a: str, title_b: str) -> bool:
        """Check if two titles have the same subject (team, person, etc.)."""
        # Extract potential subjects (capitalized words, names)
        words_a = set(title_a.split())
        words_b = set(title_b.split())

        # Find common proper nouns or key terms
        common = words_a & words_b

        # Filter out common words
        stop_words = {"the", "a", "an", "will", "to", "in", "on", "by", "for"}
        common = common - stop_words

        return len(common) >= 2

    def check_relationship(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal = Decimal("100"),
    ) -> LogicalArbitrageResult:
        """Check if a relationship constraint is violated.

        Args:
            relationship: The relationship to check.
            position_size: Size for profit calculation.

        Returns:
            LogicalArbitrageResult with violation details.
        """
        market_a = relationship.market_a
        market_b = relationship.market_b

        if relationship.relationship_type == RelationshipType.COMPLEMENT:
            return self._check_complement(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.MUTUALLY_EXCLUSIVE:
            return self._check_mutually_exclusive(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.TEMPORAL_BEFORE:
            return self._check_temporal(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.SUBSET:
            return self._check_subset(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.EXHAUSTIVE:
            return self._check_exhaustive(relationship, position_size)

        # Default: no violation detected
        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=False,
            violation_amount=Decimal("0"),
        )

    def _check_complement(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check if Yes + No prices sum to approximately 1.

        If sum < 1: Buy both Yes and No, guaranteed profit
        If sum > 1: Sell both (if possible), or indicates market inefficiency
        """
        market = relationship.market_a

        if market.yes_price is None or market.no_price is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        actual_sum = market.yes_price + market.no_price
        expected_sum = Decimal("1.0")
        violation = abs(actual_sum - expected_sum)

        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        # Calculate profit opportunity
        profit_pct = Decimal("0")
        action = None

        if actual_sum < Decimal("0.98"):
            # Buy both sides - guaranteed profit when market resolves
            profit_pct = (Decimal("1") - actual_sum) * 100
            action = f"Buy YES at {market.yes_price} and NO at {market.no_price}"

        # Calculate fees
        fees = calculate_total_fees(
            market.platform,
            float(position_size * market.yes_price),
            float(position_size * (Decimal("1") - actual_sum)),
        )
        net_profit_pct = profit_pct - Decimal(str(fees / float(position_size) * 100))

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            expected_sum=expected_sum,
            actual_sum=actual_sum,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            estimated_fees=Decimal(str(fees)),
            net_profit_pct=net_profit_pct,
            is_profitable=net_profit_pct >= Decimal(str(self.min_net_profit_pct)),
        )

    def _check_mutually_exclusive(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check mutually exclusive constraint.

        If buying all outcomes costs < $1, there's arbitrage.
        """
        market_a = relationship.market_a
        market_b = relationship.market_b

        if market_a.yes_price is None or market_b.yes_price is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        # Sum of YES prices for mutually exclusive outcomes should be ≤ 1
        actual_sum = market_a.yes_price + market_b.yes_price
        expected_max = Decimal("1.0")

        violation = max(Decimal("0"), actual_sum - expected_max)
        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        profit_pct = Decimal("0")
        action = None

        if actual_sum < Decimal("0.98"):
            # Can buy YES on both - one must pay out
            profit_pct = (Decimal("1") - actual_sum) * 100
            action = f"Buy YES on both markets (cost: {actual_sum}, payout: 1.00)"

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            expected_sum=expected_max,
            actual_sum=actual_sum,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            is_profitable=profit_pct >= Decimal(str(self.min_net_profit_pct)),
        )

    def _check_temporal(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check temporal constraint: earlier deadline should have lower prob."""
        earlier = relationship.market_a
        later = relationship.market_b

        if earlier.yes_price is None or later.yes_price is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        # P(earlier) should be ≤ P(later)
        violation = max(Decimal("0"), earlier.yes_price - later.yes_price)
        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        action = None
        profit_pct = Decimal("0")

        if is_violated:
            # Short earlier, long later
            profit_pct = violation * 100
            action = (
                f"Sell YES on earlier deadline ({earlier.yes_price}), "
                f"Buy YES on later deadline ({later.yes_price})"
            )

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            is_profitable=profit_pct >= Decimal(str(self.min_net_profit_pct)),
        )

    def _check_subset(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check subset constraint: specific outcome should have lower prob."""
        specific = relationship.market_a
        general = relationship.market_b

        if specific.yes_price is None or general.yes_price is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        # P(specific) should be ≤ P(general)
        violation = max(Decimal("0"), specific.yes_price - general.yes_price)
        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        action = None
        profit_pct = Decimal("0")

        if is_violated:
            profit_pct = violation * 100
            action = (
                f"Sell YES on specific ({specific.yes_price}), "
                f"Buy YES on general ({general.yes_price})"
            )

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            is_profitable=profit_pct >= Decimal(str(self.min_net_profit_pct)),
        )

    def _check_exhaustive(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check exhaustive constraint: all outcomes should sum to ~1."""
        # This requires the full group, stored in notes
        # For now, treat as simple pair check
        return self._check_mutually_exclusive(relationship, position_size)

    def find_opportunities(
        self,
        markets: list[MarketData],
        position_size: Decimal = Decimal("100"),
    ) -> list[LogicalArbitrageResult]:
        """Find all logical arbitrage opportunities.

        Args:
            markets: All markets to analyze.
            position_size: Position size for calculations.

        Returns:
            List of profitable LogicalArbitrageResult objects.
        """
        # Find relationships
        relationships = self.find_relationships(markets)

        # Filter by confidence
        relationships = [
            r for r in relationships
            if r.confidence >= self.min_relationship_confidence
        ]

        # Check each relationship
        opportunities = []
        for rel in relationships:
            result = self.check_relationship(rel, position_size)

            if result.is_profitable:
                opportunities.append(result)
                self.logger.info(
                    "Logical arbitrage found",
                    type=rel.relationship_type.value,
                    profit_pct=float(result.net_profit_pct),
                    market_a=rel.market_a.title[:50],
                    market_b=rel.market_b.title[:50] if rel.market_b != rel.market_a else "same",
                )

        # Sort by profit
        opportunities.sort(key=lambda x: x.net_profit_pct, reverse=True)

        self.logger.info(
            "Logical arbitrage scan complete",
            relationships_checked=len(relationships),
            opportunities_found=len(opportunities),
        )

        return opportunities
