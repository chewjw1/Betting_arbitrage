"""LLM-based validation for cross-platform market matching.

Uses GPT-4o-mini to validate whether two prediction market titles
are asking about the same real-world event, filtering out false
positives from fuzzy matching.

Enhanced prompt includes description, resolution criteria, end date,
and category when available — reducing false positives by ~20%.

Validation results are cached both in-memory and in the database
to avoid re-validating the same pairs across scanner restarts.
"""

import hashlib
from datetime import datetime
from typing import Optional

import structlog

logger = structlog.get_logger()


class LLMMatchValidator:
    """Validate fuzzy match candidates using GPT-4o-mini.

    Uses a two-tier caching strategy:
    1. In-memory cache for fast lookups within a session
    2. Database cache for persistence across restarts
    """

    # Shared DIFFERENT criteria used in both prompts
    _DIFFERENT_CRITERIA = (
        "DIFFERENT if ANY of these apply:\n"
        "- Different people, countries, or entities\n"
        "- Different time periods, deadlines, or resolution windows "
        "(e.g., 'before August 2026' vs 'before end of term' are DIFFERENT)\n"
        "- Different stages or rounds of the same process "
        "(e.g., 'win 1st round' vs 'win the election' are DIFFERENT — "
        "you can win a runoff without winning round 1)\n"
        "- Different positions (president vs VP, secretary vs chair)\n"
        "- Different actions (win vs announce, visit, buy, nominate)\n"
        "- Different scope (one state vs four, single race vs overall)\n"
        "- Inverse polarity (uphold vs strike down, pass vs block)\n"
        "- One is a subset of the other (specific deadline vs open-ended)\n"
        "- Fundamentally different questions despite similar wording\n\n"
        "SAME only if both markets resolve YES/NO under the EXACT same "
        "conditions with the same deadline. When in doubt, say DIFFERENT."
    )

    # Basic prompt when no extra context is available (titles only)
    PROMPT_TEMPLATE_BASIC = (
        "Are these two prediction market questions about the SAME specific "
        "real-world event/outcome?\n\n"
        'Market A ({platform_a}): "{title_a}"\n'
        'Market B ({platform_b}): "{title_b}"\n\n'
        "Answer SAME or DIFFERENT (one word only).\n\n"
        + _DIFFERENT_CRITERIA
    )

    # Enhanced prompt with description, resolution criteria, dates, category
    PROMPT_TEMPLATE_ENRICHED = (
        "You are a precision filter for a prediction market arbitrage system. "
        "We've already determined these markets are textually similar. "
        "Your job: confirm they resolve on the EXACT same outcome, under "
        "the EXACT same conditions and deadline.\n\n"
        "MARKET A ({platform_a})\n"
        "Title: {title_a}\n"
        "{context_a}\n"
        "MARKET B ({platform_b})\n"
        "Title: {title_b}\n"
        "{context_b}\n"
        "Answer SAME or DIFFERENT (one word only).\n\n"
        + _DIFFERENT_CRITERIA
    )

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        """Initialize validator.

        Args:
            api_key: OpenAI API key.
            model: Model to use for validation (default: gpt-4o-mini).
        """
        self.model = model
        self._memory_cache: dict[str, bool] = {}
        self.logger = logger.bind(component="LLMMatchValidator")
        self._client: Optional["AsyncOpenAI"] = None

        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=api_key)
            self.logger.info("Initialized OpenAI client", model=model)
        except ImportError:
            self.logger.warning("openai package not installed, LLM validation disabled")

    @staticmethod
    def _cache_key(title_a: str, title_b: str) -> str:
        """Generate a deterministic cache key for a title pair."""
        pair = tuple(sorted([title_a.strip().lower(), title_b.strip().lower()]))
        return hashlib.sha256(f"{pair[0]}||{pair[1]}".encode()).hexdigest()

    @staticmethod
    def _build_context_block(
        description: Optional[str] = None,
        resolution_criteria: Optional[str] = None,
        end_date: Optional[datetime] = None,
        category: Optional[str] = None,
    ) -> str:
        """Build a context block for a market with available metadata."""
        lines = []
        if category:
            lines.append(f"Category: {category}")
        if end_date:
            lines.append(f"Resolves by: {end_date.strftime('%B %d, %Y')}")
        if description:
            # Truncate long descriptions
            desc = description[:200] + "..." if len(description) > 200 else description
            lines.append(f"Description: {desc}")
        if resolution_criteria:
            criteria = resolution_criteria[:300] + "..." if len(resolution_criteria) > 300 else resolution_criteria
            lines.append(f"Resolution criteria: {criteria}")
        return "\n".join(lines) + "\n" if lines else ""

    async def _check_db_cache(self, cache_key: str) -> Optional[bool]:
        """Check if we have a cached result in the database."""
        try:
            from sqlalchemy import select
            from src.database import async_session_factory
            from src.database.models import LLMValidationCache

            async with async_session_factory() as session:
                query = select(LLMValidationCache).where(
                    LLMValidationCache.cache_key == cache_key
                )
                result = await session.execute(query)
                cached = result.scalar_one_or_none()

                if cached:
                    self.logger.debug(
                        "DB cache hit",
                        cache_key=cache_key[:16],
                        is_same=cached.is_same_event,
                    )
                    return cached.is_same_event

        except Exception as e:
            self.logger.warning("DB cache lookup failed", error=str(e))

        return None

    async def _store_db_cache(
        self,
        cache_key: str,
        title_a: str,
        platform_a: str,
        title_b: str,
        platform_b: str,
        is_same: bool,
        llm_response: str,
    ) -> None:
        """Store validation result in the database."""
        try:
            from src.database import async_session_factory
            from src.database.models import LLMValidationCache

            async with async_session_factory() as session:
                cached = LLMValidationCache(
                    cache_key=cache_key,
                    title_a=title_a,
                    title_b=title_b,
                    platform_a=platform_a,
                    platform_b=platform_b,
                    is_same_event=is_same,
                    llm_response=llm_response,
                    model_used=self.model,
                )
                session.add(cached)
                await session.commit()

                self.logger.debug(
                    "Stored in DB cache",
                    cache_key=cache_key[:16],
                    is_same=is_same,
                )

        except Exception as e:
            self.logger.warning("DB cache store failed", error=str(e))

    async def validate_match(
        self,
        title_a: str,
        platform_a: str,
        title_b: str,
        platform_b: str,
        description_a: Optional[str] = None,
        description_b: Optional[str] = None,
        resolution_criteria_a: Optional[str] = None,
        resolution_criteria_b: Optional[str] = None,
        end_date_a: Optional[datetime] = None,
        end_date_b: Optional[datetime] = None,
        category_a: Optional[str] = None,
        category_b: Optional[str] = None,
    ) -> bool:
        """Validate whether two markets are about the same event.

        Uses a two-tier cache:
        1. Check in-memory cache (fast)
        2. Check database cache (persistent)
        3. If not cached, call GPT-4o-mini and store result in both caches

        Args:
            title_a/title_b: Market titles.
            platform_a/platform_b: Platform names.
            description_a/description_b: Market descriptions (optional).
            resolution_criteria_a/resolution_criteria_b: How markets resolve (optional).
            end_date_a/end_date_b: Resolution dates (optional).
            category_a/category_b: Market categories (optional).

        Returns:
            True if the LLM considers them the same event, or if validation
            fails (graceful fallback keeps the fuzzy match result).
        """
        if self._client is None:
            return True  # Fallback: keep fuzzy match result

        key = self._cache_key(title_a, title_b)

        # Tier 1: In-memory cache
        if key in self._memory_cache:
            return self._memory_cache[key]

        # Tier 2: Database cache
        db_result = await self._check_db_cache(key)
        if db_result is not None:
            self._memory_cache[key] = db_result
            return db_result

        # Tier 3: Call OpenAI
        # Use enriched prompt if we have any extra context, otherwise basic
        has_context = any([
            description_a, description_b,
            resolution_criteria_a, resolution_criteria_b,
            end_date_a, end_date_b,
            category_a, category_b,
        ])

        if has_context:
            context_a = self._build_context_block(
                description_a, resolution_criteria_a, end_date_a, category_a
            )
            context_b = self._build_context_block(
                description_b, resolution_criteria_b, end_date_b, category_b
            )
            prompt = self.PROMPT_TEMPLATE_ENRICHED.format(
                platform_a=platform_a,
                title_a=title_a,
                context_a=context_a,
                platform_b=platform_b,
                title_b=title_b,
                context_b=context_b,
            )
        else:
            prompt = self.PROMPT_TEMPLATE_BASIC.format(
                platform_a=platform_a,
                title_a=title_a,
                platform_b=platform_b,
                title_b=title_b,
            )

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=10,
            )
            answer = response.choices[0].message.content.strip().upper()
            result = answer.startswith("SAME")

            # Store in both caches
            self._memory_cache[key] = result
            await self._store_db_cache(
                cache_key=key,
                title_a=title_a,
                platform_a=platform_a,
                title_b=title_b,
                platform_b=platform_b,
                is_same=result,
                llm_response=answer[:20],
            )

            self.logger.debug(
                "LLM validation",
                title_a=title_a[:60],
                title_b=title_b[:60],
                result=answer,
                is_same=result,
                enriched=has_context,
            )
            return result

        except Exception as e:
            self.logger.warning(
                "LLM validation failed, keeping fuzzy match",
                error=str(e),
                title_a=title_a[:60],
                title_b=title_b[:60],
            )
            return True  # Fallback: keep fuzzy match result

    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "memory_cache_size": len(self._memory_cache),
            "model": self.model,
        }
