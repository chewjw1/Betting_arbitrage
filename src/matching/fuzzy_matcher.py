"""Fuzzy string matching for market titles.

Enhanced with semantic normalization, entity extraction, and
multi-signal matching to find cross-platform market pairs that
use very different wording for the same event.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import structlog
from fuzzywuzzy import fuzz

from src.collectors.base import MarketData

if TYPE_CHECKING:
    from src.matching.llm_validator import LLMMatchValidator

logger = structlog.get_logger()


# --- Aliases & Synonyms ---
# Canonical form -> list of aliases (all lowercase)
ENTITY_ALIASES = {
    "bitcoin": ["btc", "xbt", "bitcoin"],
    "ethereum": ["eth", "ether", "ethereum"],
    "solana": ["sol", "solana"],
    "dogecoin": ["doge", "dogecoin"],
    "xrp": ["xrp", "ripple"],
    "trump": ["trump", "donald trump", "donald j trump", "djt"],
    "trump jr": ["trump jr", "donald trump jr", "trump junior", "don jr"],
    "biden": ["biden", "joe biden", "joseph biden"],
    "harris": ["harris", "kamala harris", "kamala"],
    "desantis": ["desantis", "ron desantis"],
    "haley": ["haley", "nikki haley"],
    "newsom": ["newsom", "gavin newsom"],
    "vance": ["vance", "jd vance", "j.d. vance"],
    "musk": ["musk", "elon musk", "elon"],
    "fed": ["fed", "federal reserve", "fomc", "the fed"],
    "gdp": ["gdp", "gross domestic product"],
    "cpi": ["cpi", "consumer price index", "inflation rate"],
    "s&p 500": ["s&p 500", "sp500", "s&p500", "spx", "sp 500"],
    "nasdaq": ["nasdaq", "qqq", "nasdaq 100", "nasdaq composite"],
    "dow": ["dow", "djia", "dow jones", "dow jones industrial"],
    "president": ["president", "potus", "presidency", "presidential"],
    "republican": ["republican", "republicans", "gop", "rep"],
    "democrat": ["democrat", "democrats", "democratic", "dem", "dems"],
    "congress": ["congress", "congressional"],
    "senate": ["senate", "us senate", "u.s. senate"],
    "house": ["house", "house of representatives", "us house"],
    "supreme court": ["supreme court", "scotus"],
    "super bowl": ["super bowl", "superbowl", "sb"],
    "world series": ["world series", "ws"],
    "nba championship": ["nba championship", "nba finals", "nba title"],
    "stanley cup": ["stanley cup"],
    "oscar": ["oscar", "academy award", "oscars", "academy awards"],
    "recession": ["recession", "economic recession"],
    "government shutdown": ["government shutdown", "govt shutdown", "gov shutdown"],
    "rate cut": ["rate cut", "interest rate cut", "rate reduction", "cut rates", "cuts rates"],
    "rate hike": ["rate hike", "interest rate hike", "fed hike", "rate increase"],
}

# Build reverse lookup: alias -> canonical
_ALIAS_TO_CANONICAL = {}
for canonical, aliases in ENTITY_ALIASES.items():
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias] = canonical

# Entity classification sets
PERSON_ENTITIES = {
    "trump", "trump jr", "biden", "harris", "desantis", "haley",
    "newsom", "vance", "musk",
}
PARTY_ENTITIES = {"republican", "democrat"}

# Market type keyword groups - titles with conflicting types shouldn't match
MARKET_TYPE_KEYWORDS = {
    "election": {"elect", "elected", "election"},
    "endorsement": {"endorse", "endorsed", "endorsement"},
    "nomination": {"nominate", "nominated", "nomination", "primary", "nominee"},
    "control": {"control", "majority"},
    "price_level": {"above", "below", "close", "reach", "hit", "exceed"},
    "ballot": {"ballot"},
    "pardon": {"pardon", "pardons", "commute", "commuted"},
    "testify": {"testify", "testimony", "testimonies"},
    "contempt": {"contempt"},
    "confirm": {"confirm", "confirmed", "confirmation"},
    "visit": {"visit", "visits", "visited", "travel"},
    "acquire": {"buy", "buys", "bought", "purchase", "acquire", "acquired", "acquisition"},
    "leave": {"leave", "leaves", "resign", "resigned", "departure", "quit"},
    "lawsuit": {"sue", "sues", "sued", "lawsuit", "lawsuits", "litigation"},
}

_GENERATIONAL_SUFFIXES = {"jr", "junior", "sr", "senior", "ii", "iii", "iv"}

# Antonym pairs: if one title has word A and the other has word B, they're inverse
_ANTONYM_PAIRS = [
    ({"uphold", "upheld", "upholds"}, {"strike", "struck", "overturn", "overturned"}),
    ({"pass", "passes", "passed", "approve"}, {"block", "blocked", "reject", "rejected", "fail"}),
    ({"ban", "bans", "banned"}, {"allow", "allowed", "legalize", "legalized"}),
    ({"win", "wins"}, {"lose", "loses", "lost"}),
    ({"rise", "rises", "increase"}, {"fall", "falls", "decline", "decrease"}),
]

# Country/region keywords for scope mismatch detection
_COUNTRY_MARKERS = {
    "us": {"u.s.", "us ", "united states", "american"},
    "japan": {"japan", "japanese"},
    "peru": {"peru", "peruvian"},
    "brazil": {"brazil", "brazilian"},
    "hungary": {"hungary", "hungarian"},
    "korea": {"korea", "korean"},
    "uk": {"uk ", "u.k.", "united kingdom", "british", "britain"},
    "germany": {"germany", "german"},
    "france": {"france", "french"},
    "australia": {"australia", "australian"},
    "canada": {"canada", "canadian"},
    "mexico": {"mexico", "mexican"},
    "india": {"india", "indian"},
    "china": {"china", "chinese"},
    "turkey": {"turkey", "turkish"},
    "israel": {"israel", "israeli"},
    "bolivia": {"bolivia", "bolivian"},
    "moldova": {"moldova", "moldovan"},
    "tanzania": {"tanzania", "tanzanian"},
    "argentina": {"argentina", "argentine", "argentinian"},
    "colombia": {"colombia", "colombian"},
    "chile": {"chile", "chilean"},
    "ecuador": {"ecuador", "ecuadorian"},
    "venezuela": {"venezuela", "venezuelan"},
    "philippines": {"philippines", "philippine", "filipino"},
    "indonesia": {"indonesia", "indonesian"},
    "thailand": {"thailand", "thai"},
    "vietnam": {"vietnam", "vietnamese"},
    "nigeria": {"nigeria", "nigerian"},
    "south_africa": {"south africa", "south african"},
    "kenya": {"kenya", "kenyan"},
    "egypt": {"egypt", "egyptian"},
    "iraq": {"iraq", "iraqi"},
    "iran": {"iran", "iranian"},
    "ukraine": {"ukraine", "ukrainian"},
    "poland": {"poland", "polish"},
    "italy": {"italy", "italian"},
    "spain": {"spain", "spanish"},
    "netherlands": {"netherlands", "dutch"},
    "sweden": {"sweden", "swedish"},
    "norway": {"norway", "norwegian"},
    "denmark": {"denmark", "danish"},
    "finland": {"finland", "finnish"},
    "greece": {"greece", "greek"},
    "portugal": {"portugal", "portuguese"},
    "new_zealand": {"new zealand"},
    "singapore": {"singapore"},
    "taiwan": {"taiwan", "taiwanese"},
}


# --- Number normalization ---
_NUMBER_SUFFIXES = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "t": 1_000_000_000_000,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
}


def _normalize_numbers(text: str) -> str:
    """Normalize all number formats to plain integers.

    Handles: $100k, $100,000, 100K, 100 thousand, 100000, etc.
    """
    # Remove dollar signs and commas in numbers
    text = re.sub(r"\$\s*", "", text)
    text = re.sub(r"(\d),(\d)", r"\1\2", text)

    # Handle suffix notation: 100k, 1.5m, etc.
    def expand_suffix(m):
        num_str = m.group(1)
        suffix = m.group(2).lower()
        multiplier = _NUMBER_SUFFIXES.get(suffix, 1)
        try:
            value = float(num_str) * multiplier
            return str(int(value))
        except (ValueError, OverflowError):
            return m.group(0)

    text = re.sub(
        r"(\d+(?:\.\d+)?)\s*(k|m|b|t|thousand|million|billion|trillion)\b",
        expand_suffix,
        text,
        flags=re.IGNORECASE,
    )

    return text


def _normalize_entities(text: str) -> str:
    """Replace known aliases with canonical entity names."""
    # Sort by length (longest first) to avoid partial replacements
    sorted_aliases = sorted(_ALIAS_TO_CANONICAL.keys(), key=len, reverse=True)
    for alias in sorted_aliases:
        # Use word boundary matching to avoid partial replacements
        pattern = r"\b" + re.escape(alias) + r"\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            canonical = _ALIAS_TO_CANONICAL[alias]
            text = re.sub(pattern, canonical, text, flags=re.IGNORECASE)
    return text


def _extract_entities(text: str) -> set[str]:
    """Extract key entities from normalized text.

    Returns canonical entity names found in the text.
    De-duplicates hierarchical entities (e.g., removes "trump" when "trump jr" is present).
    """
    entities = set()
    text_lower = text.lower()
    for canonical, aliases in ENTITY_ALIASES.items():
        for alias in aliases:
            if alias in text_lower:
                entities.add(canonical)
                break
    # De-duplicate: if "trump jr" is present, remove "trump" (more specific wins)
    if "trump jr" in entities:
        entities.discard("trump")
    return entities


def _extract_numbers(text: str) -> set[int]:
    """Extract all significant numbers from text."""
    numbers = set()
    for match in re.finditer(r"\b(\d{2,})\b", text):
        try:
            numbers.add(int(match.group(1)))
        except ValueError:
            pass
    return numbers


def _get_market_types(text: str) -> set[str]:
    """Identify market type(s) from title keywords.

    Returns set of market type labels (e.g., {"election"}, {"endorsement"}).
    """
    words = set(text.lower().split())
    types = set()
    for mtype, keywords in MARKET_TYPE_KEYWORDS.items():
        if words & keywords:
            types.add(mtype)
    return types


def _is_bucket_market(title: str) -> bool:
    """Check if a market is about a specific numeric bucket or range.

    Detects:
    - PredictIt contracts like "How many seats? - 48"
    - Kalshi margin-of-victory ranges like "between 6% and 9%"
    - "above X", "exactly X", "between X and Y" patterns
    """
    if " - " in title:
        suffix = title.split(" - ", 1)[1].strip()
        if re.match(r'^\d+[+\-]?$', suffix):
            return True
    if re.search(r'\bhow many\b', title, re.IGNORECASE):
        return True
    # Margin-of-victory or numeric range markets
    if re.search(r'\bbetween \d+%?\s+and\s+\d+%?\b', title, re.IGNORECASE):
        return True
    if re.search(r'\bmargin of victory\b', title, re.IGNORECASE):
        return True
    # "exactly N seats/people" patterns
    if re.search(r'\bexactly \d+\b', title, re.IGNORECASE):
        return True
    return False


def _has_inverse_polarity(title_a: str, title_b: str) -> bool:
    """Check if two titles ask the same question with opposite polarity.

    E.g., "Will Supreme Court uphold ban?" vs "Will SCOTUS strike down ban?"
    These look similar but are actually inverse questions.
    """
    words_a = set(title_a.lower().split())
    words_b = set(title_b.lower().split())

    for positive_set, negative_set in _ANTONYM_PAIRS:
        a_has_pos = bool(words_a & positive_set)
        a_has_neg = bool(words_a & negative_set)
        b_has_pos = bool(words_b & positive_set)
        b_has_neg = bool(words_b & negative_set)

        # One title has the positive, the other has the negative
        if (a_has_pos and b_has_neg and not a_has_neg) or (a_has_neg and b_has_pos and not b_has_neg):
            return True

    return False


def _has_scope_mismatch(title_a: str, title_b: str) -> bool:
    """Check if titles refer to different geographic scopes or multi-vs-single.

    Catches:
    - "Win in GA, MI, NC, AND ME" (4-state sweep) vs single state
    - Japan House vs US House (different countries)
    """
    a_lower = title_a.lower()
    b_lower = title_b.lower()

    # Multi-condition sweep: if one title has "and" joining multiple states/conditions
    # and the other doesn't, they're different scope
    a_has_and = bool(re.search(r'\band\b', a_lower))
    b_has_and = bool(re.search(r'\band\b', b_lower))

    # Count comma-separated items (indicates multi-target)
    a_commas = a_lower.count(",")
    b_commas = b_lower.count(",")

    # One is a multi-target sweep (3+ commas) and the other isn't
    if (a_commas >= 2 and a_has_and and b_commas == 0) or \
       (b_commas >= 2 and b_has_and and a_commas == 0):
        return True

    # "Any X" scope vs specific instance
    # "Will any independent win..." vs "Which party will win in Alaska?"
    a_has_any = bool(re.search(r'\bany\b', a_lower))
    b_has_any = bool(re.search(r'\bany\b', b_lower))
    if a_has_any != b_has_any:
        # Check if the "any" title is broader than the other
        # by looking for specific state/district names
        any_title = a_lower if a_has_any else b_lower
        specific_title = b_lower if a_has_any else a_lower
        # If the specific title mentions a state that the "any" title doesn't
        for _country, markers in _COUNTRY_MARKERS.items():
            for marker in markers:
                if marker in specific_title and marker not in any_title:
                    return True

    # Country mismatch: detect if titles reference different countries
    countries_a = set()
    countries_b = set()
    for country, markers in _COUNTRY_MARKERS.items():
        for marker in markers:
            if marker in a_lower:
                countries_a.add(country)
            if marker in b_lower:
                countries_b.add(country)

    # If both explicitly mention countries and they don't overlap
    if countries_a and countries_b and not (countries_a & countries_b):
        return True

    return False


def _is_vote_threshold_vs_outcome(title_a: str, title_b: str) -> bool:
    """Check if one title is about a vote threshold and the other about an outcome.

    E.g., "Will X get over 5% of the vote?" vs "Will X be the nominee?"
    These are fundamentally different questions about the same person.
    """
    a_lower = title_a.lower()
    b_lower = title_b.lower()

    # Detect vote threshold patterns
    threshold_pattern = r'(get |receive )?(over|above|under|below|more than|less than|at least)\s+\d+%'
    a_is_threshold = bool(re.search(threshold_pattern, a_lower))
    b_is_threshold = bool(re.search(threshold_pattern, b_lower))

    if a_is_threshold != b_is_threshold:
        # One is a vote threshold, the other isn't - different questions
        return True

    return False


def _is_count_vs_specific(title_a: str, title_b: str) -> bool:
    """Check if one title is about a count/range and the other is about a specific instance.

    E.g., "Will Trump pardon between 3 and 9 people?" vs "Will Trump pardon Elon Musk?"
    """
    a_lower = title_a.lower()
    b_lower = title_b.lower()

    # Detect count/range patterns
    count_pattern = r'\b(between \d+ and \d+|above \d+|exactly \d+|at least \d+|more than \d+|fewer than \d+)\b'
    a_has_count = bool(re.search(count_pattern, a_lower))
    b_has_count = bool(re.search(count_pattern, b_lower))

    if a_has_count != b_has_count:
        # One is a count/range, check if the other is about a specific person/thing
        non_count = b_lower if a_has_count else a_lower
        non_count_entities = _extract_entities(non_count)
        specific_people = non_count_entities & PERSON_ENTITIES
        if specific_people:
            return True

    return False


def _has_generational_difference(title_a: str, title_b: str) -> bool:
    """Check if titles refer to different generations of the same family.

    Returns True if one title has a generational suffix (Jr, Sr, III, etc.)
    and the other doesn't.
    """
    a_lower = title_a.lower()
    b_lower = title_b.lower()
    a_has_gen = any(
        re.search(r'\b' + re.escape(s) + r'\.?\b', a_lower)
        for s in _GENERATIONAL_SUFFIXES
    )
    b_has_gen = any(
        re.search(r'\b' + re.escape(s) + r'\.?\b', b_lower)
        for s in _GENERATIONAL_SUFFIXES
    )
    return a_has_gen != b_has_gen


def _subjects_compatible(title_a: str, platform_a: Optional[str], title_b: str, platform_b: Optional[str]) -> bool:
    """Check if two matched titles refer to the same subject/candidate.

    PredictIt contracts have format: "Who will win X? - Candidate Name"
    If one title specifies a candidate and the other specifies a DIFFERENT one, reject.

    Handles:
    - Person vs party name mismatches (Tim Walz vs "Democratic")
    - First-name-only overlaps (Mark Cuban vs Mark Kelly)
    - Generational suffixes (Trump vs Trump Jr)
    - Bucket vs binary markets ("48 seats" vs "control Senate")

    Returns:
        True if subjects are compatible (same person/team, or can't determine).
    """
    # Extract PredictIt contract-specific suffix (after " - ")
    subject_a = None
    subject_b = None

    if " - " in title_a:
        subject_a = title_a.split(" - ", 1)[1].strip().lower()
    if " - " in title_b:
        subject_b = title_b.split(" - ", 1)[1].strip().lower()

    # If neither has a subject suffix, can't validate
    if subject_a is None and subject_b is None:
        return True

    # Reject bucket-vs-binary mismatches (e.g., "48 seats" vs "control Senate")
    if _is_bucket_market(title_a) != _is_bucket_market(title_b):
        return False

    # If both have subjects, check if they match (via canonical entities)
    if subject_a is not None and subject_b is not None:
        entities_a = _extract_entities(subject_a)
        entities_b = _extract_entities(subject_b)
        if entities_a and entities_b:
            return bool(entities_a & entities_b)
        # Fall back to word overlap - require >50% for multi-word subjects
        words_a = {w for w in subject_a.split() if len(w) > 2}
        words_b = {w for w in subject_b.split() if len(w) > 2}
        words_a -= {"the", "will", "win", "and", "for", "who"}
        words_b -= {"the", "will", "win", "and", "for", "who"}
        if words_a and words_b:
            overlap = len(words_a & words_b)
            max_len = max(len(words_a), len(words_b))
            return overlap / max_len > 0.5
        return True

    other_title = title_b.lower() if subject_a else title_a.lower()
    subject = subject_a or subject_b

    # Check via entity matching
    subject_entities = _extract_entities(subject)
    other_entities = _extract_entities(other_title)

    # Reject person-vs-party mismatches:
    # If subject is a party name ("Democratic", "Republican") and the other
    # title is about a specific person, they're different markets
    subject_party = subject_entities & PARTY_ENTITIES
    other_people = other_entities & PERSON_ENTITIES
    if subject_party and other_people:
        other_party = other_entities & PARTY_ENTITIES
        if not (subject_party & other_party):
            return False

    # Reverse check: subject is a person, other is about a party
    subject_people = subject_entities & PERSON_ENTITIES
    other_party = other_entities & PARTY_ENTITIES
    if subject_people and other_party and not other_people:
        return False

    if subject_entities and other_entities:
        return bool(subject_entities & other_entities)

    # Fall back to word-boundary matching (not substring matching)
    subject_words = {w for w in subject.split() if len(w) > 2}
    subject_words -= {"the", "will", "win", "and", "for", "who"}

    if not subject_words:
        return True

    other_words = set(re.findall(r'\b\w+\b', other_title))
    matches = len(subject_words & other_words)
    # Require >50% of subject words to appear in the other title
    return matches / len(subject_words) > 0.5


def _dates_compatible(date_a: Optional[datetime], date_b: Optional[datetime], max_days_diff: int = 30) -> bool:
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
    """Match markets across platforms using fuzzy string matching
    enhanced with entity extraction and semantic normalization."""

    # Common words to normalize/remove for better matching
    STOP_WORDS = {
        "will", "the", "a", "an", "be", "to", "in", "on", "at", "by",
        "for", "of", "or", "and", "is", "are", "was", "were", "been",
        "being", "have", "has", "had", "do", "does", "did", "shall",
        "should", "would", "could", "may", "might", "must", "can",
        "this", "that", "what", "which", "who", "whom", "how", "when",
        "where", "there", "here", "than", "then", "its", "it",
    }

    # Platform-specific patterns to normalize
    PLATFORM_PATTERNS = {
        "kalshi": [
            (r"\[.*?\]", ""),           # Remove bracketed text like [Bitcoin]
            (r"KX\w+-\w+", ""),         # Remove Kalshi tickers
            (r"KXBTC-\w+", "bitcoin"),  # Normalize BTC ticker
        ],
        "polymarket": [
            (r"\?$", ""),               # Remove trailing question marks
        ],
        "predictit": [
            (r" - .*$", ""),            # Remove contract-specific suffix
        ],
        "draftkings": [
            (r"\(.*?\)", ""),           # Remove parenthetical notes
        ],
        "fanduel": [
            (r"\(.*?\)", ""),           # Remove parenthetical notes
        ],
    }

    def __init__(
        self,
        min_confidence: float = 0.65,
        llm_validator: Optional[LLMMatchValidator] = None,
    ):
        """Initialize matcher.

        Args:
            min_confidence: Minimum similarity score (0-1) to consider a match.
            llm_validator: Optional LLM validator for filtering false positives.
        """
        self.min_confidence = min_confidence
        self.llm_validator = llm_validator
        self.logger = logger.bind(component="MarketMatcher")

    def normalize_title(self, title: str, platform: Optional[str] = None) -> str:
        """Normalize market title for comparison.

        Applies platform-specific cleanup, number normalization,
        entity canonicalization, and general text cleanup.

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

        # Normalize numbers before entity matching ($100k -> 100000)
        normalized = _normalize_numbers(normalized)

        # Normalize entities (BTC -> bitcoin, POTUS -> president, etc.)
        normalized = _normalize_entities(normalized)

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

        Uses a multi-signal approach:
        1. Fuzzy string ratios (token set, partial, token sort)
        2. Keyword overlap
        3. Entity overlap bonus (names, assets, etc.)
        4. Number overlap bonus (price levels, thresholds)

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

        # --- Early rejection checks ---

        # Subject compatibility (Mark Cuban vs Mark Kelly, person vs party, etc.)
        if not _subjects_compatible(title_a, platform_a, title_b, platform_b):
            return 0.0

        # Generational suffix mismatch (Trump vs Trump Jr)
        if _has_generational_difference(title_a, title_b):
            return 0.0

        # Bucket vs binary mismatch ("48 seats" vs "control Senate")
        if _is_bucket_market(title_a) != _is_bucket_market(title_b):
            return 0.0

        # Market type mismatch ("win primary" vs "endorse", "on ballot" vs "win")
        types_a = _get_market_types(norm_a)
        types_b = _get_market_types(norm_b)
        if types_a and types_b and not (types_a & types_b):
            return 0.0

        # Ballot eligibility vs election outcome are different even if both say "election"
        a_has_ballot = "ballot" in types_a
        b_has_ballot = "ballot" in types_b
        if a_has_ballot != b_has_ballot:
            return 0.0

        # Inverse polarity ("uphold ban" vs "strike down ban")
        if _has_inverse_polarity(title_a, title_b):
            return 0.0

        # Scope mismatch (Japan House vs US House, 4-state sweep vs single state)
        if _has_scope_mismatch(title_a, title_b):
            return 0.0

        # Count/range vs specific instance ("pardon 3-9 people" vs "pardon Elon Musk")
        if _is_count_vs_specific(title_a, title_b):
            return 0.0

        # Vote threshold vs outcome ("get over 5% of vote" vs "be the nominee")
        if _is_vote_threshold_vs_outcome(title_a, title_b):
            return 0.0

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

        # Base similarity from fuzzy matching
        base_similarity = (
            0.30 * token_set_ratio
            + 0.20 * partial_ratio
            + 0.20 * token_sort_ratio
            + 0.15 * keyword_overlap
        )

        # --- Entity overlap bonus ---
        entities_a = _extract_entities(norm_a)
        entities_b = _extract_entities(norm_b)

        entity_bonus = 0.0
        if entities_a and entities_b:
            entity_overlap = len(entities_a & entities_b) / max(len(entities_a), len(entities_b))
            entity_bonus = 0.10 * entity_overlap

            # Strong penalty if key entities DON'T overlap
            people_a = entities_a & PERSON_ENTITIES
            people_b = entities_b & PERSON_ENTITIES
            if people_a and people_b and not (people_a & people_b):
                return 0.0  # Different people = definitely different markets

            # Person-vs-party mismatch penalty
            party_a = entities_a & PARTY_ENTITIES
            party_b = entities_b & PARTY_ENTITIES
            if (people_a and party_b and not people_b) or (people_b and party_a and not people_a):
                return 0.0

        # --- Number overlap bonus ---
        numbers_a = _extract_numbers(norm_a)
        numbers_b = _extract_numbers(norm_b)

        number_bonus = 0.0
        if numbers_a and numbers_b:
            common_numbers = numbers_a & numbers_b
            if common_numbers:
                number_bonus = 0.05
            else:
                big_a = {n for n in numbers_a if n >= 1000}
                big_b = {n for n in numbers_b if n >= 1000}
                if big_a and big_b and not (big_a & big_b):
                    return max(0.0, base_similarity - 0.15)

        similarity = min(1.0, base_similarity + entity_bonus + number_bonus)

        return similarity

    def find_matches(
        self,
        markets_a: list[MarketData],
        markets_b: list[MarketData],
        min_confidence: Optional[float] = None,
        max_date_diff_days: int = 30,
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

        # Pre-compute normalized titles and entities for markets_b
        b_cache = []
        for market_b in markets_b:
            norm_title = self.normalize_title(market_b.title, market_b.platform)
            entities = _extract_entities(norm_title)
            keywords = self.extract_keywords(norm_title)
            b_cache.append((norm_title, entities, keywords))

        for market_a in markets_a:
            norm_a = self.normalize_title(market_a.title, market_a.platform)
            entities_a = _extract_entities(norm_a)
            keywords_a = self.extract_keywords(norm_a)

            best_match = None
            best_score = 0
            best_idx = -1

            for idx, market_b in enumerate(markets_b):
                if idx in used_b_indices:
                    continue

                # Quick pre-filter: skip if no entity or keyword overlap
                _, entities_b, keywords_b = b_cache[idx]
                if entities_a and entities_b and not (entities_a & entities_b):
                    # No entity overlap - very unlikely to be same market
                    # Unless neither has strong entities
                    if len(entities_a) >= 2 or len(entities_b) >= 2:
                        continue

                if keywords_a and keywords_b:
                    overlap_ratio = len(keywords_a & keywords_b) / max(len(keywords_a), len(keywords_b))
                    if overlap_ratio < 0.1:
                        # Less than 10% keyword overlap - skip expensive fuzzy matching
                        continue

                # Skip if resolution dates are too far apart
                if not _dates_compatible(market_a.end_date, market_b.end_date, max_date_diff_days):
                    continue

                # Skip if titles refer to different candidates/subjects
                if not _subjects_compatible(
                    market_a.title, market_a.platform,
                    market_b.title, market_b.platform,
                ):
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

        self.logger.info(
            "Matching complete",
            markets_a_count=len(markets_a),
            markets_b_count=len(markets_b),
            matches_found=len(matches),
        )

        return matches

    async def find_matches_validated(
        self,
        markets_a: list[MarketData],
        markets_b: list[MarketData],
        min_confidence: Optional[float] = None,
        max_date_diff_days: int = 30,
    ) -> list[tuple[MarketData, MarketData, float]]:
        """Find matching markets with optional LLM validation.

        Runs sync fuzzy matching first, then filters candidates through
        the LLM validator if one is configured.

        Args:
            markets_a: First list of markets.
            markets_b: Second list of markets.
            min_confidence: Override minimum confidence threshold.
            max_date_diff_days: Maximum days difference in resolution dates.

        Returns:
            List of (market_a, market_b, confidence) tuples.
        """
        # Step 1: fuzzy matching (fast pre-filter)
        candidates = self.find_matches(
            markets_a, markets_b, min_confidence, max_date_diff_days
        )

        if not self.llm_validator or not candidates:
            return candidates

        # Step 2: LLM validation (precision filter)
        validated = []
        for market_a, market_b, confidence in candidates:
            is_same = await self.llm_validator.validate_match(
                title_a=market_a.title,
                platform_a=market_a.platform,
                title_b=market_b.title,
                platform_b=market_b.platform,
            )
            if is_same:
                validated.append((market_a, market_b, confidence))
            else:
                self.logger.info(
                    "LLM rejected match",
                    title_a=market_a.title[:60],
                    title_b=market_b.title[:60],
                    confidence=f"{confidence:.2%}",
                )

        self.logger.info(
            "LLM validation complete",
            candidates=len(candidates),
            validated=len(validated),
            rejected=len(candidates) - len(validated),
        )

        return validated

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
