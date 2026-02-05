"""Fuzzy string matching for market titles."""

import re
from datetime import datetime, timezone
from typing import Optional

import structlog
from fuzzywuzzy import fuzz

from src.collectors.base import MarketData

logger = structlog.get_logger()


def _dates_compatible(date_a: Optional[datetime], date_b: Optional[datetime], max_days_diff: int = 14) -> bool:
    """Check if two resolution dates are close enough to be the same event.

    Args:
        date_a: First date (can be None).
        date_b: Second date (can be None).
        max_days_diff: Maximum allowed difference in days.

    Returns:
        True if dates are compatible (both None, or within max_days_diff).
    """
    # If either is missing, we can't validate - assume compatible
    if date_a is None or date_b is None:
        return True

    # Normalize timezones
    if date_a.tzinfo is None:
        date_a = date_a.replace(tzinfo=timezone.utc)
    if date_b.tzinfo is None:
        date_b = date_b.replace(tzinfo=timezone.utc)

    diff = abs((date_a - date_b).days)
    return diff <= max_days_diff


class MarketMatcher:
    """Match markets across platforms using fuzzy string matching."""

    # Common words to normalize/remove for better matching
    STOP_WORDS = {
        "will", "the", "a", "an", "be", "to", "in", "on", "at", "by",
        "for", "of", "or", "and", "is", "are", "was", "were", "been",
        "being", "have", "has", "had", "do", "does", "did", "shall",
        "should", "would", "could", "may", "might", "must", "can",
    }

    # Platform-specific patterns to normalize
    PLATFORM_PATTERNS = {
        "kalshi": [
            (r"\[.*?\]", ""),  # Remove bracketed text
            (r"KXBTC-\w+", "bitcoin"),  # Normalize Kalshi ticker
        ],
        "polymarket": [
            (r"\?$", ""),  # Remove trailing question marks
        ],
        "predictit": [
            (r" - .*$", ""),  # Remove contract-specific suffix
        ],
    }

    def __init__(self, min_confidence: float = 0.8):
        """Initialize matcher.

        Args:
            min_confidence: Minimum similarity score (0-1) to consider a match.
        """
        self.min_confidence = min_confidence
        self.logger = logger.bind(component="MarketMatcher")

    def normalize_title(self, title: str, platform: Optional[str] = None) -> str:
        """Normalize market title for comparison.

        Args:
            title: Original market title.
            platform: Optional platform name for platform-specific normalization.

        Returns:
            Normalized title string.
        """
        normalized = title.lower().strip()

        # Apply platform-specific patterns
        if platform and platform in self.PLATFORM_PATTERNS:
            for pattern, replacement in self.PLATFORM_PATTERNS[platform]:
                normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

        # Remove punctuation except hyphens and apostrophes
        normalized = re.sub(r"[^\w\s\-']", " ", normalized)

        # Normalize whitespace
        normalized = re.sub(r"\s+", " ", normalized).strip()

        return normalized

    def extract_keywords(self, title: str) -> set[str]:
        """Extract important keywords from a title.

        Args:
            title: Normalized title.

        Returns:
            Set of keywords.
        """
        words = title.lower().split()
        keywords = {w for w in words if w not in self.STOP_WORDS and len(w) > 2}
        return keywords

    def calculate_similarity(
        self,
        title_a: str,
        title_b: str,
        platform_a: Optional[str] = None,
        platform_b: Optional[str] = None,
    ) -> float:
        """Calculate similarity between two market titles.

        Uses a combination of:
        - Token set ratio (handles word order differences)
        - Partial ratio (handles substring matches)
        - Keyword overlap

        Args:
            title_a: First title.
            title_b: Second title.
            platform_a: Platform for first title.
            platform_b: Platform for second title.

        Returns:
            Similarity score from 0.0 to 1.0.
        """
        # Normalize titles
        norm_a = self.normalize_title(title_a, platform_a)
        norm_b = self.normalize_title(title_b, platform_b)

        # Calculate fuzzy ratios
        token_set_ratio = fuzz.token_set_ratio(norm_a, norm_b) / 100
        partial_ratio = fuzz.partial_ratio(norm_a, norm_b) / 100
        token_sort_ratio = fuzz.token_sort_ratio(norm_a, norm_b) / 100

        # Keyword overlap
        keywords_a = self.extract_keywords(norm_a)
        keywords_b = self.extract_keywords(norm_b)

        if keywords_a and keywords_b:
            keyword_overlap = len(keywords_a & keywords_b) / max(
                len(keywords_a), len(keywords_b)
            )
        else:
            keyword_overlap = 0

        # Weighted average
        similarity = (
            0.35 * token_set_ratio
            + 0.25 * partial_ratio
            + 0.25 * token_sort_ratio
            + 0.15 * keyword_overlap
        )

        return similarity

    def find_matches(
        self,
        markets_a: list[MarketData],
        markets_b: list[MarketData],
        min_confidence: Optional[float] = None,
        max_date_diff_days: int = 14,
    ) -> list[tuple[MarketData, MarketData, float]]:
        """Find matching markets between two lists.

        Args:
            markets_a: First list of markets.
            markets_b: Second list of markets.
            min_confidence: Override minimum confidence threshold.
            max_date_diff_days: Maximum days difference in resolution dates.

        Returns:
            List of (market_a, market_b, confidence) tuples.
        """
        if min_confidence is None:
            min_confidence = self.min_confidence

        matches = []
        used_b_indices = set()

        for market_a in markets_a:
            best_match = None
            best_score = 0
            best_idx = -1

            for idx, market_b in enumerate(markets_b):
                if idx in used_b_indices:
                    continue

                # Skip if resolution dates are too far apart
                if not _dates_compatible(market_a.end_date, market_b.end_date, max_date_diff_days):
                    continue

                score = self.calculate_similarity(
                    market_a.title,
                    market_b.title,
                    market_a.platform,
                    market_b.platform,
                )

                if score > best_score and score >= min_confidence:
                    best_score = score
                    best_match = market_b
                    best_idx = idx

            if best_match is not None:
                matches.append((market_a, best_match, best_score))
                used_b_indices.add(best_idx)

        self.logger.debug(
            "Matching complete",
            markets_a_count=len(markets_a),
            markets_b_count=len(markets_b),
            matches_found=len(matches),
        )

        return matches

    def find_best_match(
        self,
        target: MarketData,
        candidates: list[MarketData],
    ) -> Optional[tuple[MarketData, float]]:
        """Find the best matching market for a target.

        Args:
            target: Target market to match.
            candidates: List of candidate markets.

        Returns:
            (best_match, confidence) or None if no match above threshold.
        """
        best_match = None
        best_score = 0

        for candidate in candidates:
            score = self.calculate_similarity(
                target.title,
                candidate.title,
                target.platform,
                candidate.platform,
            )

            if score > best_score:
                best_score = score
                best_match = candidate

        if best_match and best_score >= self.min_confidence:
            return (best_match, best_score)

        return None
