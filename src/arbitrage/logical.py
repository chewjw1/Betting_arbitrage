"""Logical arbitrage detection for related prediction markets.

This module detects arbitrage opportunities based on logical relationships
between markets, rather than the same market on different platforms.

Types of logical arbitrage:
1. Mutually exclusive outcomes (should sum to 100%)
2. Temporal relationships (P(X by March) ≤ P(X by June))
3. Conditional probabilities (P(A and B) ≤ min(P(A), P(B)))
4. Subset relationships (P(specific) ≤ P(general))
5. Complement violations (P(Yes) + P(No) should ≈ 100% on same platform)
6. Cross-platform logical (same relationship across platforms)
7. Correlated events (highly correlated outcomes with price discrepancy)
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

import structlog

from src.arbitrage.fees import FeeCalculator
from src.collectors.base import MarketData
from src.config import PLATFORM_FEES

logger = structlog.get_logger()


def calculate_total_fees(platform: str, position_size: float, profit: float) -> float:
    """Calculate total fees for a position."""
    fees = PLATFORM_FEES.get(platform, {})
    trading_fee = position_size * fees.get("trading_fee_pct", 0)
    profit_fee = max(0, profit) * fees.get("profit_fee_pct", 0)
    return trading_fee + profit_fee


class RelationshipType(Enum):
    """Types of logical relationships between markets."""

    MUTUALLY_EXCLUSIVE = "mutually_exclusive"  # Outcomes can't both happen
    EXHAUSTIVE = "exhaustive"  # One of them must happen
    TEMPORAL_BEFORE = "temporal_before"  # Earlier deadline implies lower prob
    TEMPORAL_AFTER = "temporal_after"
    SUBSET = "subset"  # One is a subset of another
    COMPLEMENT = "complement"  # Yes + No on same market
    CONDITIONAL = "conditional"  # A and B relationship
    CORRELATED = "correlated"  # Highly correlated events
    CROSS_PLATFORM_LOGICAL = "cross_platform_logical"  # Logical relationship across platforms


class OpportunitySubtype(Enum):
    """Specific subtypes for detailed categorization."""

    # Cross-platform
    CROSS_PLATFORM_SAME_EVENT = "cross_platform_same_event"

    # Complement
    YES_NO_MISPRICING = "yes_no_mispricing"

    # Mutually exclusive
    ELECTION_CANDIDATES = "election_candidates"
    SPORTS_WINNER = "sports_winner"
    RANGE_BUCKETS = "range_buckets"  # e.g., "BTC > 100k" + "BTC 90-100k" + "BTC < 90k"

    # Temporal
    DEADLINE_INCONSISTENCY = "deadline_inconsistency"

    # Subset
    CHAMPIONSHIP_VS_PLAYOFFS = "championship_vs_playoffs"
    NOMINEE_VS_WINNER = "nominee_vs_winner"
    SPECIFIC_VS_GENERAL = "specific_vs_general"

    # Correlated
    HIGHLY_CORRELATED = "highly_correlated"
    CAUSALLY_LINKED = "causally_linked"


@dataclass
class LogicalRelationship:
    """Represents a logical relationship between markets."""

    market_a: MarketData
    market_b: MarketData
    relationship_type: RelationshipType
    confidence: float  # How confident we are this relationship exists
    expected_constraint: str  # Human-readable constraint
    subtype: Optional[OpportunitySubtype] = None
    notes: Optional[str] = None
    group_markets: Optional[list[MarketData]] = None  # For exhaustive groups


@dataclass
class FeeBreakdown:
    """Detailed fee breakdown for display."""

    platform: str
    position_size: Decimal
    entry_fee: Decimal
    profit_fee: Decimal
    total_fee: Decimal
    fee_pct: Decimal


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

    # Enhanced fields for dashboard
    opportunity_type: str = "logical"
    subtype: Optional[str] = None
    subtype_display: Optional[str] = None

    # Per-platform details
    fee_breakdown_a: Optional[FeeBreakdown] = None
    fee_breakdown_b: Optional[FeeBreakdown] = None

    # Prices for display
    price_a: Optional[Decimal] = None
    price_b: Optional[Decimal] = None
    side_a: Optional[str] = None
    side_b: Optional[str] = None


class LogicalArbitrageDetector:
    """Detect arbitrage from logical inconsistencies in prediction markets."""

    # Patterns for extracting temporal information
    # NOTE: Only include patterns that represent actual DEADLINES that can differ
    # for the same underlying event. "2026 election" is NOT a deadline variant -
    # 2026 and 2028 elections are completely different events.
    TEMPORAL_PATTERNS = [
        (r"by (\w+ \d{1,2},? \d{4})", "by_date_full"),
        (r"by (\w+ \d{1,2})", "by_date"),
        (r"before (\w+ \d{1,2},? \d{4})", "before_date_full"),
        (r"before (\w+ \d{1,2})", "before_date"),
        (r"by end of (\d{4})", "by_year_end"),
        (r"by (Q[1-4]) (\d{4})", "by_quarter"),
        (r"by (january|february|march|april|may|june|july|august|september|october|november|december) (\d{4})", "by_month_year"),
        # Removed: (r"in (\w+) (\d{4})", "in_month") - too broad, matches "in 2026"
        # Removed: (r"(\d{4}) (election|season|year)", "year_event") - different years are different events
    ]

    # Words that indicate mutually exclusive outcomes (not temporal variants)
    EXCLUSIVE_OUTCOME_WORDS = {
        # Parties
        "republican", "republicans", "democrat", "democrats", "democratic",
        "libertarian", "green", "independent",
        # Yes/No variants
        "yes", "no", "pass", "fail", "passes", "fails",
        # Candidates (will need to expand)
        "trump", "biden", "harris", "desantis", "haley", "newsom", "pence",
        # Other mutually exclusive
        "over", "under", "above", "below", "higher", "lower",
    }

    # Extended subset relationship patterns
    SUBSET_PATTERNS = [
        # Sports
        ("wins super bowl", "wins conference", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins super bowl", "makes playoffs", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins world series", "wins pennant", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins world series", "makes playoffs", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins nba championship", "wins conference", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins nba championship", "makes playoffs", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins stanley cup", "makes playoffs", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins champions league", "qualifies for champions league", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins premier league", "finishes top 4", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins mvp", "makes all-star", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("wins championship", "makes final", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins tournament", "reaches semifinals", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),
        ("wins gold medal", "medals", OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS),

        # Politics
        ("wins election", "is nominee", OpportunitySubtype.NOMINEE_VS_WINNER),
        ("wins election", "wins primary", OpportunitySubtype.NOMINEE_VS_WINNER),
        ("becomes president", "wins nomination", OpportunitySubtype.NOMINEE_VS_WINNER),
        ("wins general election", "wins primary", OpportunitySubtype.NOMINEE_VS_WINNER),
        ("passes senate", "passes house", OpportunitySubtype.SPECIFIC_VS_GENERAL),  # Not strict subset but often related
        ("signed into law", "passes congress", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("impeached and removed", "impeached", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("convicted", "indicted", OpportunitySubtype.SPECIFIC_VS_GENERAL),

        # Entertainment/Awards
        ("wins oscar", "nominated for oscar", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("wins grammy", "nominated for grammy", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("wins emmy", "nominated for emmy", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("wins best picture", "nominated", OpportunitySubtype.SPECIFIC_VS_GENERAL),

        # Tech/Business
        ("ipo above", "files for ipo", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("acquired for", "acquisition announced", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("bankrupt", "defaults on debt", OpportunitySubtype.SPECIFIC_VS_GENERAL),

        # Crypto/Finance
        ("bitcoin above 200k", "bitcoin above 150k", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("bitcoin above 150k", "bitcoin above 100k", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("bitcoin above 100k", "bitcoin above 75k", OpportunitySubtype.SPECIFIC_VS_GENERAL),
        ("ethereum above", "ethereum above", OpportunitySubtype.SPECIFIC_VS_GENERAL),  # Will need price comparison
    ]

    # Patterns for correlated events
    CORRELATION_PATTERNS = [
        # Fed rate decisions often correlate with market moves
        (r"fed (raises|cuts|holds) rates", r"s&p 500 (above|below)", 0.6),
        (r"fed (raises|cuts|holds) rates", r"bitcoin (above|below)", 0.5),

        # Election outcomes often correlate
        (r"(\w+) wins (presidency|election)", r"(\w+) wins (senate|house)", 0.7),

        # Economic indicators
        (r"recession in \d{4}", r"unemployment (above|below)", 0.65),
        (r"inflation (above|below)", r"fed (raises|cuts)", 0.6),
    ]

    # Mutually exclusive group patterns
    EXCLUSIVE_GROUP_PATTERNS = [
        # Elections - candidates for same position
        (r"(will )?(trump|biden|harris|desantis|haley|newsom|pence) (win|wins|to win)", "election_winner"),
        # Sports - teams in same league/conference
        (r"(will )?(chiefs|49ers|eagles|cowboys|ravens|bills) (win|wins) super bowl", "nfl_champion"),
        (r"(will )?(lakers|celtics|nuggets|heat|bucks|warriors) (win|wins) nba", "nba_champion"),
        # Ranges - price buckets
        (r"bitcoin (above|below|between) (\d+)", "btc_price_range"),
        (r"s&p 500 (above|below|between) (\d+)", "sp500_price_range"),
    ]

    def __init__(
        self,
        min_violation_pct: float = 2.0,
        min_net_profit_pct: float = 1.0,
        min_relationship_confidence: float = 0.7,
        default_position_size: float = 100.0,
    ):
        """Initialize detector.

        Args:
            min_violation_pct: Minimum constraint violation to flag.
            min_net_profit_pct: Minimum net profit after fees to alert.
            min_relationship_confidence: Minimum confidence in relationship.
            default_position_size: Default position size for calculations.
        """
        self.min_violation_pct = min_violation_pct
        self.min_net_profit_pct = min_net_profit_pct
        self.min_relationship_confidence = min_relationship_confidence
        self.default_position_size = Decimal(str(default_position_size))
        self.fee_calculator = FeeCalculator()
        self.logger = logger.bind(component="LogicalArbitrageDetector")

    def find_relationships(
        self,
        markets: list[MarketData],
        cross_platform: bool = True,
    ) -> list[LogicalRelationship]:
        """Identify logical relationships between markets.

        Args:
            markets: List of all markets to analyze.
            cross_platform: Whether to check cross-platform relationships.

        Returns:
            List of detected LogicalRelationship objects.
        """
        relationships = []

        # Group markets by platform for complement checking
        by_platform = {}
        for m in markets:
            by_platform.setdefault(m.platform, []).append(m)

        # Check for complement violations within each platform
        # IMPORTANT: Skip PredictIt - their YES+NO often > 1.0 due to bid-ask spread
        # and you can't short to exploit it (also 15%+ fees make it unprofitable)
        for platform, platform_markets in by_platform.items():
            # Skip PredictIt complement checks entirely
            if platform.lower() == "predictit":
                continue

            for market in platform_markets:
                if market.yes_price and market.no_price:
                    # Only check if sum < 1 (can buy both to lock in profit)
                    # Skip if sum >= 1 (would need to short, which most platforms don't allow)
                    total = market.yes_price + market.no_price
                    if total >= Decimal("1"):
                        continue

                    rel = LogicalRelationship(
                        market_a=market,
                        market_b=market,
                        relationship_type=RelationshipType.COMPLEMENT,
                        confidence=1.0,
                        expected_constraint="P(Yes) + P(No) = 1.0",
                        subtype=OpportunitySubtype.YES_NO_MISPRICING,
                    )
                    relationships.append(rel)

        # Find mutually exclusive groups (same event, different outcomes)
        exclusive_groups = self._find_mutually_exclusive_groups(markets)
        for group_info in exclusive_groups:
            group = group_info["markets"]
            subtype = group_info.get("subtype", OpportunitySubtype.ELECTION_CANDIDATES)

            if len(group) >= 2:
                # Check exhaustive constraint for the whole group
                rel = LogicalRelationship(
                    market_a=group[0],
                    market_b=group[-1],
                    relationship_type=RelationshipType.EXHAUSTIVE,
                    confidence=0.85,
                    expected_constraint="Sum of all mutually exclusive outcomes ≈ 1.0",
                    subtype=subtype,
                    notes=f"Group of {len(group)} markets",
                    group_markets=group,
                )
                relationships.append(rel)

        # Find temporal relationships
        temporal_rels = self._find_temporal_relationships(markets)
        relationships.extend(temporal_rels)

        # Find subset relationships
        subset_rels = self._find_subset_relationships(markets)
        relationships.extend(subset_rels)

        # Find correlated events
        correlated_rels = self._find_correlated_relationships(markets)
        relationships.extend(correlated_rels)

        # Cross-platform logical relationships
        if cross_platform and len(by_platform) > 1:
            cross_rels = self._find_cross_platform_logical(markets, by_platform)
            relationships.extend(cross_rels)

        self.logger.info(
            "Found relationships",
            total=len(relationships),
            complement=sum(1 for r in relationships if r.relationship_type == RelationshipType.COMPLEMENT),
            exclusive=sum(1 for r in relationships if r.relationship_type == RelationshipType.MUTUALLY_EXCLUSIVE),
            exhaustive=sum(1 for r in relationships if r.relationship_type == RelationshipType.EXHAUSTIVE),
            temporal=sum(1 for r in relationships if r.relationship_type in (RelationshipType.TEMPORAL_BEFORE, RelationshipType.TEMPORAL_AFTER)),
            subset=sum(1 for r in relationships if r.relationship_type == RelationshipType.SUBSET),
            correlated=sum(1 for r in relationships if r.relationship_type == RelationshipType.CORRELATED),
            cross_platform=sum(1 for r in relationships if r.relationship_type == RelationshipType.CROSS_PLATFORM_LOGICAL),
        )

        return relationships

    def _find_mutually_exclusive_groups(
        self,
        markets: list[MarketData],
    ) -> list[dict]:
        """Find groups of mutually exclusive markets.

        E.g., "Trump wins", "Biden wins", "Other wins" for same election.

        Returns:
            List of {"markets": [...], "subtype": ...} dicts.
        """
        groups = []

        # Method 1: Group by normalized base question
        by_base = {}
        for market in markets:
            base = self._extract_base_question(market.title)
            if base:
                by_base.setdefault(base, []).append(market)

        for base, group in by_base.items():
            if len(group) >= 2:
                # Determine subtype
                subtype = self._classify_exclusive_group(group)
                groups.append({"markets": group, "subtype": subtype})

        # Method 2: Pattern-based detection
        for pattern, group_type in self.EXCLUSIVE_GROUP_PATTERNS:
            matched_markets = []
            for market in markets:
                if re.search(pattern, market.title.lower()):
                    matched_markets.append(market)

            if len(matched_markets) >= 2:
                # Check if not already in a group
                market_ids = {m.platform_market_id for m in matched_markets}
                already_grouped = any(
                    market_ids & {m.platform_market_id for m in g["markets"]}
                    for g in groups
                )
                if not already_grouped:
                    subtype = self._group_type_to_subtype(group_type)
                    groups.append({"markets": matched_markets, "subtype": subtype})

        return groups

    def _classify_exclusive_group(self, group: list[MarketData]) -> OpportunitySubtype:
        """Classify a mutually exclusive group by subtype."""
        titles = " ".join(m.title.lower() for m in group)

        if any(word in titles for word in ["election", "president", "governor", "senator", "nominee"]):
            return OpportunitySubtype.ELECTION_CANDIDATES
        if any(word in titles for word in ["super bowl", "championship", "world series", "nba", "nfl", "mlb"]):
            return OpportunitySubtype.SPORTS_WINNER
        if any(word in titles for word in ["above", "below", "between", "range", "more than", "less than", "fewer"]):
            return OpportunitySubtype.RANGE_BUCKETS

        # Default to RANGE_BUCKETS for non-election/non-sports groups
        return OpportunitySubtype.RANGE_BUCKETS

    def _group_type_to_subtype(self, group_type: str) -> OpportunitySubtype:
        """Convert group type string to OpportunitySubtype."""
        mapping = {
            "election_winner": OpportunitySubtype.ELECTION_CANDIDATES,
            "nfl_champion": OpportunitySubtype.SPORTS_WINNER,
            "nba_champion": OpportunitySubtype.SPORTS_WINNER,
            "btc_price_range": OpportunitySubtype.RANGE_BUCKETS,
            "sp500_price_range": OpportunitySubtype.RANGE_BUCKETS,
        }
        return mapping.get(group_type, OpportunitySubtype.ELECTION_CANDIDATES)

    def _extract_base_question(self, title: str) -> Optional[str]:
        """Extract the base question from a market title.

        E.g., "Will Trump win the 2024 election?" -> "win the 2024 election"

        Returns:
            Normalized base question or None.
        """
        normalized = title.lower().strip()

        # Remove "Will X..." pattern
        normalized = re.sub(r"^will\s+\w+\s+", "", normalized)

        # Remove question marks
        normalized = normalized.rstrip("?")

        # Remove specific outcome mentions
        normalized = re.sub(r"\b(yes|no|win|lose|pass|fail)\b", "", normalized)

        # Remove names/entities that differentiate outcomes
        normalized = re.sub(r"\b(trump|biden|harris|desantis|haley|newsom)\b", "", normalized)

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
                # Must be same platform for clean logical comparison
                if market_a.platform != market_b.platform:
                    continue

                # Times must be different (at least 7 days apart)
                time_diff = abs((time_a - time_b).days)
                if time_diff < 7:
                    continue

                # Check if same underlying event
                if self._same_underlying_event(market_a.title, market_b.title):
                    # Determine which is earlier
                    if time_a < time_b:
                        rel = LogicalRelationship(
                            market_a=market_a,
                            market_b=market_b,
                            relationship_type=RelationshipType.TEMPORAL_BEFORE,
                            confidence=0.85,
                            expected_constraint=f"P(by {time_a.strftime('%b %Y')}) ≤ P(by {time_b.strftime('%b %Y')})",
                            subtype=OpportunitySubtype.DEADLINE_INCONSISTENCY,
                        )
                    else:
                        rel = LogicalRelationship(
                            market_a=market_b,
                            market_b=market_a,
                            relationship_type=RelationshipType.TEMPORAL_BEFORE,
                            confidence=0.85,
                            expected_constraint=f"P(by {time_b.strftime('%b %Y')}) ≤ P(by {time_a.strftime('%b %Y')})",
                            subtype=OpportunitySubtype.DEADLINE_INCONSISTENCY,
                        )
                    relationships.append(rel)

        return relationships

    def _extract_temporal_info(self, title: str) -> Optional[datetime]:
        """Extract temporal deadline from market title.

        Returns:
            Datetime representing the deadline, or None.
        """
        try:
            from dateutil import parser as date_parser
        except ImportError:
            return None

        title_lower = title.lower()

        # Try each pattern
        for pattern, pattern_type in self.TEMPORAL_PATTERNS:
            match = re.search(pattern, title_lower)
            if match:
                try:
                    date_str = " ".join(match.groups())
                    parsed = date_parser.parse(date_str, fuzzy=True)

                    # "by end of 2026" should be Dec 31, not Jan 1
                    if pattern_type == "by_year_end":
                        parsed = parsed.replace(month=12, day=31)

                    return parsed
                except Exception:
                    continue

        return None

    def _same_underlying_event(self, title_a: str, title_b: str) -> bool:
        """Check if two titles refer to the same underlying event.

        For temporal arbitrage, we need:
        1. Same underlying event (e.g., "Bitcoin hits 100k")
        2. Different deadlines (e.g., "by March" vs "by June")
        3. NOT mutually exclusive outcomes (e.g., Republican vs Democrat)

        Returns:
            True if likely same event with different timeframes.
        """
        title_a_lower = title_a.lower()
        title_b_lower = title_b.lower()

        # Check for mutually exclusive outcomes - these are NOT temporal variants
        exclusive_in_a = {w for w in self.EXCLUSIVE_OUTCOME_WORDS if w in title_a_lower}
        exclusive_in_b = {w for w in self.EXCLUSIVE_OUTCOME_WORDS if w in title_b_lower}

        # If one has "republican" and other has "democrat", they're exclusive outcomes
        if exclusive_in_a and exclusive_in_b and exclusive_in_a != exclusive_in_b:
            return False

        # If one has an exclusive word and the other doesn't, they're different
        if bool(exclusive_in_a) != bool(exclusive_in_b):
            return False

        # Remove temporal info and compare
        def remove_temporal(s: str) -> str:
            s = s.lower()
            for pattern, _ in self.TEMPORAL_PATTERNS:
                s = re.sub(pattern, "", s)
            return re.sub(r"\s+", " ", s).strip()

        cleaned_a = remove_temporal(title_a_lower)
        cleaned_b = remove_temporal(title_b_lower)

        # If titles are identical after removing temporal info, they're definitely different deadlines
        if cleaned_a == cleaned_b:
            return True

        # Simple similarity check
        words_a = set(cleaned_a.split())
        words_b = set(cleaned_b.split())

        if not words_a or not words_b:
            return False

        overlap = len(words_a & words_b) / max(len(words_a), len(words_b))

        # Require higher overlap (85%) and ensure there's actually a temporal difference
        return overlap > 0.85

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

        for market_a in markets:
            title_a_lower = market_a.title.lower()

            for market_b in markets:
                if market_a == market_b:
                    continue
                # Same platform preferred for cleaner comparison
                if market_a.platform != market_b.platform:
                    continue

                title_b_lower = market_b.title.lower()

                # Check for known subset patterns
                for specific, general, subtype in self.SUBSET_PATTERNS:
                    if specific in title_a_lower and general in title_b_lower:
                        # Check if same subject
                        if self._same_subject(title_a_lower, title_b_lower):
                            rel = LogicalRelationship(
                                market_a=market_a,  # More specific
                                market_b=market_b,  # More general
                                relationship_type=RelationshipType.SUBSET,
                                confidence=0.8,
                                expected_constraint=f"P({specific}) ≤ P({general})",
                                subtype=subtype,
                            )
                            relationships.append(rel)

        return relationships

    def _find_correlated_relationships(
        self,
        markets: list[MarketData],
    ) -> list[LogicalRelationship]:
        """Find highly correlated event relationships.

        Returns:
            List of correlated relationships.
        """
        relationships = []

        for pattern_a, pattern_b, min_correlation in self.CORRELATION_PATTERNS:
            markets_a = [m for m in markets if re.search(pattern_a, m.title.lower())]
            markets_b = [m for m in markets if re.search(pattern_b, m.title.lower())]

            for ma in markets_a:
                for mb in markets_b:
                    if ma.platform == mb.platform:  # Same platform
                        rel = LogicalRelationship(
                            market_a=ma,
                            market_b=mb,
                            relationship_type=RelationshipType.CORRELATED,
                            confidence=min_correlation,
                            expected_constraint=f"Events correlated ~{min_correlation*100:.0f}%",
                            subtype=OpportunitySubtype.HIGHLY_CORRELATED,
                        )
                        relationships.append(rel)

        return relationships

    def _find_cross_platform_logical(
        self,
        markets: list[MarketData],
        by_platform: dict[str, list[MarketData]],
    ) -> list[LogicalRelationship]:
        """Find logical relationships that exist across platforms.

        E.g., Temporal inconsistency where Kalshi has "X by March" higher than
        Polymarket has "X by June".

        Returns:
            List of cross-platform logical relationships.
        """
        relationships = []
        platforms = list(by_platform.keys())

        for i, platform_a in enumerate(platforms):
            for platform_b in platforms[i + 1:]:
                markets_a = by_platform[platform_a]
                markets_b = by_platform[platform_b]

                # Check for cross-platform temporal inconsistencies
                temporal_a = [(m, self._extract_temporal_info(m.title)) for m in markets_a]
                temporal_a = [(m, t) for m, t in temporal_a if t is not None]

                temporal_b = [(m, self._extract_temporal_info(m.title)) for m in markets_b]
                temporal_b = [(m, t) for m, t in temporal_b if t is not None]

                for ma, time_a in temporal_a:
                    for mb, time_b in temporal_b:
                        if self._same_underlying_event(ma.title, mb.title):
                            # Cross-platform temporal check
                            if time_a < time_b:
                                # Earlier deadline on platform A should have lower prob
                                rel = LogicalRelationship(
                                    market_a=ma,
                                    market_b=mb,
                                    relationship_type=RelationshipType.CROSS_PLATFORM_LOGICAL,
                                    confidence=0.75,
                                    expected_constraint=f"P({platform_a} by {time_a.strftime('%b')}) ≤ P({platform_b} by {time_b.strftime('%b')})",
                                    subtype=OpportunitySubtype.DEADLINE_INCONSISTENCY,
                                )
                                relationships.append(rel)
                            elif time_b < time_a:
                                rel = LogicalRelationship(
                                    market_a=mb,
                                    market_b=ma,
                                    relationship_type=RelationshipType.CROSS_PLATFORM_LOGICAL,
                                    confidence=0.75,
                                    expected_constraint=f"P({platform_b} by {time_b.strftime('%b')}) ≤ P({platform_a} by {time_a.strftime('%b')})",
                                    subtype=OpportunitySubtype.DEADLINE_INCONSISTENCY,
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
        stop_words = {"the", "a", "an", "will", "to", "in", "on", "by", "for", "and", "or", "be", "is", "are"}
        common = common - stop_words

        return len(common) >= 2

    def _calculate_fee_breakdown(
        self,
        platform: str,
        position_size: Decimal,
        expected_profit: Decimal,
    ) -> FeeBreakdown:
        """Calculate detailed fee breakdown for a position.

        IMPORTANT: Includes withdrawal fees (PredictIt has 5% on ALL withdrawals).
        For complement/exhaustive trades, you withdraw position + profit.
        """
        entry_fee = self.fee_calculator.calculate_entry_fee(platform, position_size)
        profit_fee = self.fee_calculator.calculate_profit_fee(platform, expected_profit)

        # Withdrawal fee - applied to total amount withdrawn (position + profit)
        # For PredictIt this is 5% on EVERYTHING you take out
        withdrawal_amount = position_size + expected_profit
        withdrawal_fee = self.fee_calculator.calculate_withdrawal_fee(platform, withdrawal_amount)

        total_fee = entry_fee + profit_fee + withdrawal_fee
        fee_pct = (total_fee / position_size) * 100 if position_size else Decimal("0")

        return FeeBreakdown(
            platform=platform,
            position_size=position_size,
            entry_fee=entry_fee,
            profit_fee=profit_fee + withdrawal_fee,  # Combine for display
            total_fee=total_fee,
            fee_pct=fee_pct,
        )

    def check_relationship(
        self,
        relationship: LogicalRelationship,
        position_size: Optional[Decimal] = None,
    ) -> LogicalArbitrageResult:
        """Check if a relationship constraint is violated.

        Args:
            relationship: The relationship to check.
            position_size: Size for profit calculation.

        Returns:
            LogicalArbitrageResult with violation details.
        """
        if position_size is None:
            position_size = self.default_position_size

        market_a = relationship.market_a
        market_b = relationship.market_b

        if relationship.relationship_type == RelationshipType.COMPLEMENT:
            return self._check_complement(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.EXHAUSTIVE:
            return self._check_exhaustive(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.TEMPORAL_BEFORE:
            return self._check_temporal(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.SUBSET:
            return self._check_subset(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.CORRELATED:
            return self._check_correlated(relationship, position_size)

        elif relationship.relationship_type == RelationshipType.CROSS_PLATFORM_LOGICAL:
            return self._check_cross_platform_logical(relationship, position_size)

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

        # Use ASK prices (what you'd actually pay to buy), not bid/midpoint.
        # Bid is what someone will pay you; ask is the cost to buy.
        yes_buy = market.yes_ask if market.yes_ask is not None else market.yes_price
        no_buy = market.no_ask if market.no_ask is not None else market.no_price

        if yes_buy is None or no_buy is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        actual_sum = yes_buy + no_buy
        expected_sum = Decimal("1.0")
        violation = abs(actual_sum - expected_sum)

        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        # Calculate profit opportunity
        profit_pct = Decimal("0")
        action = None
        side_a = None
        side_b = None

        if actual_sum < Decimal("0.98"):
            # Buy both sides - guaranteed profit when market resolves
            profit_pct = (Decimal("1") - actual_sum) * 100
            action = f"Buy YES at ${yes_buy:.2f} and NO at ${no_buy:.2f}"
            side_a = "yes"
            side_b = "no"

        # Calculate fees
        fee_breakdown = self._calculate_fee_breakdown(
            market.platform,
            position_size,
            position_size * (Decimal("1") - actual_sum),
        )

        net_profit_pct = profit_pct - fee_breakdown.fee_pct

        # Get subtype display name
        subtype_display = self._get_subtype_display(relationship.subtype)

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            expected_sum=expected_sum,
            actual_sum=actual_sum,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            estimated_fees=fee_breakdown.total_fee,
            net_profit_pct=net_profit_pct,
            is_profitable=net_profit_pct >= Decimal(str(self.min_net_profit_pct)),
            opportunity_type="logical",
            subtype=relationship.subtype.value if relationship.subtype else None,
            subtype_display=subtype_display,
            fee_breakdown_a=fee_breakdown,
            price_a=yes_buy,
            price_b=no_buy,
            side_a=side_a,
            side_b=side_b,
        )

    def _check_exhaustive(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check exhaustive constraint: all outcomes should sum to ~1.

        Important: Only valid if the group truly covers ALL possible outcomes.
        Small groups (e.g., 3 out of 20 La Liga teams) will have low sums
        that are NOT arbitrage — the missing probability belongs to unlisted outcomes.
        """
        group = relationship.group_markets or [relationship.market_a, relationship.market_b]

        # Sum all YES prices — use ASK price (what you'd pay to buy), not bid.
        # Bid is what someone will pay you; ask is what it costs to buy.
        # Using bid makes markets look artificially cheap → false positives.
        prices = []
        for m in group:
            buy_price = m.yes_ask if m.yes_ask is not None else m.yes_price
            if buy_price is not None:
                prices.append((m, buy_price))

        # Require at least 5 markets - small groups are almost never truly exhaustive
        # (e.g., 2 of 20 football teams, 3 of 15 presidential candidates)
        if len(prices) < 5:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        actual_sum = sum(p for _, p in prices)
        expected_sum = Decimal("1.0")

        violation = Decimal("1") - actual_sum if actual_sum < Decimal("1") else Decimal("0")

        # If violation > 15%, the group is probably incomplete (missing outcomes)
        # Real exhaustive arbitrage should be small (2-10%)
        if violation > Decimal("0.15"):
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        profit_pct = Decimal("0")
        action = None

        if actual_sum < Decimal("0.98"):
            # Can buy YES on all - one must pay out
            profit_pct = (Decimal("1") - actual_sum) * 100
            markets_str = ", ".join([f"{m.title[:30]}@${p:.2f}" for m, p in prices[:3]])
            action = f"Buy YES on all {len(prices)} outcomes (cost: ${actual_sum:.2f}, payout: $1.00): {markets_str}..."

        # Calculate average fees across platforms
        platforms = {m.platform for m, _ in prices}
        total_fees = Decimal("0")
        for platform in platforms:
            fee = self._calculate_fee_breakdown(platform, position_size / len(platforms), profit_pct / 100 * position_size)
            total_fees += fee.total_fee

        net_profit_pct = profit_pct - (total_fees / position_size * 100)

        subtype_display = self._get_subtype_display(relationship.subtype)

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            expected_sum=expected_sum,
            actual_sum=actual_sum,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            estimated_fees=total_fees,
            net_profit_pct=net_profit_pct,
            is_profitable=net_profit_pct >= Decimal(str(self.min_net_profit_pct)),
            opportunity_type="logical",
            subtype=relationship.subtype.value if relationship.subtype else None,
            subtype_display=subtype_display,
        )

    def _check_temporal(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check temporal constraint: earlier deadline should have lower prob.

        Trade: Buy NO on earlier deadline + Buy YES on later deadline.
        Use ASK prices (what you'd actually pay to buy), not midpoints.
        The violation is: P(earlier YES) > P(later YES), meaning
        no_ask_earlier + yes_ask_later < $1.00 guarantees profit.
        """
        earlier = relationship.market_a
        later = relationship.market_b

        # Use ASK prices (cost to buy), falling back to midpoint
        earlier_yes = earlier.yes_ask if earlier.yes_ask is not None else earlier.yes_price
        later_yes = later.yes_ask if later.yes_ask is not None else later.yes_price

        if earlier_yes is None or later_yes is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        # P(earlier) should be ≤ P(later)
        violation = max(Decimal("0"), earlier_yes - later_yes)
        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        action = None
        profit_pct = Decimal("0")

        # Actual trade prices: Buy NO on earlier, Buy YES on later
        earlier_no_cost = earlier.no_ask if earlier.no_ask is not None else (Decimal("1") - earlier_yes)
        later_yes_cost = later_yes

        if is_violated:
            total_cost = earlier_no_cost + later_yes_cost
            profit_pct = (Decimal("1") - total_cost) * 100 if total_cost < Decimal("1") else Decimal("0")
            action = (
                f"Buy NO on earlier deadline ({earlier.platform}: ${earlier_no_cost:.2f}), "
                f"Buy YES on later deadline ({later.platform}: ${later_yes_cost:.2f})"
            )

        # Fee breakdowns
        fee_a = self._calculate_fee_breakdown(earlier.platform, position_size, profit_pct / 100 * position_size / 2)
        fee_b = self._calculate_fee_breakdown(later.platform, position_size, profit_pct / 100 * position_size / 2)
        total_fees = fee_a.total_fee + fee_b.total_fee

        net_profit_pct = profit_pct - (total_fees / position_size * 100)
        subtype_display = self._get_subtype_display(relationship.subtype)

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            estimated_fees=total_fees,
            net_profit_pct=net_profit_pct,
            is_profitable=net_profit_pct >= Decimal(str(self.min_net_profit_pct)),
            opportunity_type="logical",
            subtype=relationship.subtype.value if relationship.subtype else None,
            subtype_display=subtype_display,
            fee_breakdown_a=fee_a,
            fee_breakdown_b=fee_b,
            price_a=earlier_no_cost,
            price_b=later_yes_cost,
            side_a="no",
            side_b="yes",
        )

    def _check_subset(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check subset constraint: specific outcome should have lower prob.

        Trade: Buy NO on specific + Buy YES on general.
        Uses ASK prices (what you'd actually pay to buy).
        """
        specific = relationship.market_a
        general = relationship.market_b

        # Use ASK prices (cost to buy), falling back to midpoint
        specific_yes = specific.yes_ask if specific.yes_ask is not None else specific.yes_price
        general_yes = general.yes_ask if general.yes_ask is not None else general.yes_price

        if specific_yes is None or general_yes is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        # P(specific) should be ≤ P(general)
        violation = max(Decimal("0"), specific_yes - general_yes)
        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        action = None
        profit_pct = Decimal("0")

        # Actual trade prices
        specific_no_cost = specific.no_ask if specific.no_ask is not None else (Decimal("1") - specific_yes)
        general_yes_cost = general_yes

        if is_violated:
            total_cost = specific_no_cost + general_yes_cost
            profit_pct = (Decimal("1") - total_cost) * 100 if total_cost < Decimal("1") else Decimal("0")
            action = (
                f"Buy NO on specific ({specific.platform}: ${specific_no_cost:.2f}), "
                f"Buy YES on general ({general.platform}: ${general_yes_cost:.2f})"
            )

        # Fee breakdowns
        fee_a = self._calculate_fee_breakdown(specific.platform, position_size, profit_pct / 100 * position_size / 2)
        fee_b = self._calculate_fee_breakdown(general.platform, position_size, profit_pct / 100 * position_size / 2)
        total_fees = fee_a.total_fee + fee_b.total_fee

        net_profit_pct = profit_pct - (total_fees / position_size * 100)
        subtype_display = self._get_subtype_display(relationship.subtype)

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            estimated_fees=total_fees,
            net_profit_pct=net_profit_pct,
            is_profitable=net_profit_pct >= Decimal(str(self.min_net_profit_pct)),
            opportunity_type="logical",
            subtype=relationship.subtype.value if relationship.subtype else None,
            subtype_display=subtype_display,
            fee_breakdown_a=fee_a,
            fee_breakdown_b=fee_b,
            price_a=specific_no_cost,
            price_b=general_yes_cost,
            side_a="no",
            side_b="yes",
        )

    def _check_correlated(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check correlated events for pricing inconsistencies."""
        market_a = relationship.market_a
        market_b = relationship.market_b

        if market_a.yes_price is None or market_b.yes_price is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        # For correlated events, we look for unusual divergence
        # This is informational - hard to trade directly without more context
        price_diff = abs(market_a.yes_price - market_b.yes_price)

        # If correlation is high but prices diverge significantly, flag it
        expected_diff = Decimal("1") - Decimal(str(relationship.confidence))  # Higher correlation = lower expected diff
        violation = max(Decimal("0"), price_diff - expected_diff)

        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        subtype_display = self._get_subtype_display(relationship.subtype)

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            profit_opportunity_pct=violation * 100,
            recommended_action=f"Correlated events diverge: {market_a.title[:40]} vs {market_b.title[:40]}",
            is_profitable=False,  # Informational only
            opportunity_type="logical",
            subtype=relationship.subtype.value if relationship.subtype else None,
            subtype_display=subtype_display,
            price_a=market_a.yes_price,
            price_b=market_b.yes_price,
        )

    def _check_cross_platform_logical(
        self,
        relationship: LogicalRelationship,
        position_size: Decimal,
    ) -> LogicalArbitrageResult:
        """Check cross-platform logical relationships.

        Trade: Buy NO on earlier deadline + Buy YES on later deadline.
        Uses ASK prices (what you'd actually pay to buy).
        """
        market_a = relationship.market_a  # Earlier deadline
        market_b = relationship.market_b  # Later deadline

        # Use ASK prices (cost to buy), falling back to midpoint
        a_yes = market_a.yes_ask if market_a.yes_ask is not None else market_a.yes_price
        b_yes = market_b.yes_ask if market_b.yes_ask is not None else market_b.yes_price

        if a_yes is None or b_yes is None:
            return LogicalArbitrageResult(
                relationship=relationship,
                is_violated=False,
                violation_amount=Decimal("0"),
            )

        # For temporal cross-platform: earlier deadline should have lower price
        violation = max(Decimal("0"), a_yes - b_yes)
        is_violated = violation > Decimal(str(self.min_violation_pct / 100))

        action = None
        profit_pct = Decimal("0")

        # Actual trade prices
        a_no_cost = market_a.no_ask if market_a.no_ask is not None else (Decimal("1") - a_yes)
        b_yes_cost = b_yes

        if is_violated:
            total_cost = a_no_cost + b_yes_cost
            profit_pct = (Decimal("1") - total_cost) * 100 if total_cost < Decimal("1") else Decimal("0")
            action = (
                f"Buy NO on {market_a.platform} (${a_no_cost:.2f}), "
                f"Buy YES on {market_b.platform} (${b_yes_cost:.2f})"
            )

        # Fee breakdowns
        fee_a = self._calculate_fee_breakdown(market_a.platform, position_size, profit_pct / 100 * position_size / 2)
        fee_b = self._calculate_fee_breakdown(market_b.platform, position_size, profit_pct / 100 * position_size / 2)
        total_fees = fee_a.total_fee + fee_b.total_fee

        net_profit_pct = profit_pct - (total_fees / position_size * 100)
        subtype_display = self._get_subtype_display(relationship.subtype)

        return LogicalArbitrageResult(
            relationship=relationship,
            is_violated=is_violated,
            violation_amount=violation,
            profit_opportunity_pct=profit_pct,
            recommended_action=action,
            estimated_fees=total_fees,
            net_profit_pct=net_profit_pct,
            is_profitable=net_profit_pct >= Decimal(str(self.min_net_profit_pct)),
            opportunity_type="cross_platform_logical",
            subtype=relationship.subtype.value if relationship.subtype else None,
            subtype_display=subtype_display,
            fee_breakdown_a=fee_a,
            fee_breakdown_b=fee_b,
            price_a=a_no_cost,
            price_b=b_yes_cost,
            side_a="no",
            side_b="yes",
        )

    def _get_subtype_display(self, subtype: Optional[OpportunitySubtype]) -> str:
        """Get human-readable display name for subtype."""
        if subtype is None:
            return "Unknown"

        display_names = {
            OpportunitySubtype.CROSS_PLATFORM_SAME_EVENT: "Cross-Platform Same Event",
            OpportunitySubtype.YES_NO_MISPRICING: "Yes/No Mispricing",
            OpportunitySubtype.ELECTION_CANDIDATES: "Election Candidates",
            OpportunitySubtype.SPORTS_WINNER: "Sports Winner",
            OpportunitySubtype.RANGE_BUCKETS: "Price Range Buckets",
            OpportunitySubtype.DEADLINE_INCONSISTENCY: "Deadline Inconsistency",
            OpportunitySubtype.CHAMPIONSHIP_VS_PLAYOFFS: "Championship vs Playoffs",
            OpportunitySubtype.NOMINEE_VS_WINNER: "Nominee vs Winner",
            OpportunitySubtype.SPECIFIC_VS_GENERAL: "Specific vs General",
            OpportunitySubtype.HIGHLY_CORRELATED: "Highly Correlated Events",
            OpportunitySubtype.CAUSALLY_LINKED: "Causally Linked Events",
        }
        return display_names.get(subtype, subtype.value.replace("_", " ").title())

    def find_opportunities(
        self,
        markets: list[MarketData],
        position_size: Optional[Decimal] = None,
    ) -> list[LogicalArbitrageResult]:
        """Find all logical arbitrage opportunities.

        Args:
            markets: All markets to analyze.
            position_size: Position size for calculations.

        Returns:
            List of profitable LogicalArbitrageResult objects.
        """
        if position_size is None:
            position_size = self.default_position_size

        # Find relationships
        relationships = self.find_relationships(markets, cross_platform=True)

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
                    subtype=rel.subtype.value if rel.subtype else None,
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
