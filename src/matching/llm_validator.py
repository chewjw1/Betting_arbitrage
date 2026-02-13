"""LLM-based validation for cross-platform market matching.

Uses GPT-4o-mini or Claude Haiku to validate whether two prediction market titles
are asking about the same real-world event, filtering out false
positives from fuzzy matching.

Validation results are cached both in-memory and in the database
to avoid re-validating the same pairs across scanner restarts.
"""

import hashlib
from typing import Optional, Union

import structlog

logger = structlog.get_logger()


class LLMMatchValidator:
    """Validate fuzzy match candidates using an LLM.

    Uses a two-tier caching strategy:
    1. In-memory cache for fast lookups within a session
    2. Database cache for persistence across restarts

    Supports both Anthropic (Claude) and OpenAI (GPT) as providers.
    """

    PROMPT_TEMPLATE = (
        "Are these two prediction market questions about the SAME specific "
        "real-world event/outcome?\n\n"
        'Market A ({platform_a}): "{title_a}"\n'
        'Market B ({platform_b}): "{title_b}"\n\n'
        "Answer SAME or DIFFERENT (one word only).\n\n"
        "DIFFERENT if: different people, countries, time periods, positions "
        "(president vs VP), actions (win vs announce/visit/buy), scope "
        "(one state vs four), inverse polarity (uphold vs strike down), "
        "or fundamentally different questions."
    )

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        provider: str = "openai",
    ):
        """Initialize validator.

        Args:
            api_key: API key for the LLM provider.
            model: Model to use for validation (gpt-4o-mini is cheaper).
            provider: 'openai' for GPT or 'anthropic' for Claude.
        """
        self.model = model
        self.provider = provider
        self._memory_cache: dict[str, bool] = {}
        self.logger = logger.bind(component="LLMMatchValidator")

        self._client: Optional[Union["AsyncAnthropic", "AsyncOpenAI"]] = None

        if provider == "anthropic":
            try:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic(api_key=api_key)
                self.logger.info("Initialized Anthropic client", model=model)
            except ImportError:
                self.logger.warning("anthropic package not installed, trying OpenAI fallback")
                provider = "openai"

        if provider == "openai":
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(api_key=api_key)
                self.provider = "openai"
                self.logger.info("Initialized OpenAI client", model=model)
            except ImportError:
                self.logger.warning("openai package not installed, LLM validation disabled")
                self._client = None

    @staticmethod
    def _cache_key(title_a: str, title_b: str) -> str:
        """Generate a deterministic cache key for a title pair."""
        pair = tuple(sorted([title_a.strip().lower(), title_b.strip().lower()]))
        return hashlib.sha256(f"{pair[0]}||{pair[1]}".encode()).hexdigest()

    async def _check_db_cache(self, cache_key: str) -> Optional[bool]:
        """Check if we have a cached result in the database.

        Args:
            cache_key: SHA256 hash of the title pair.

        Returns:
            Cached is_same_event value, or None if not cached.
        """
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
        """Store validation result in the database.

        Args:
            cache_key: SHA256 hash of the title pair.
            title_a: First market title.
            platform_a: First market platform.
            title_b: Second market title.
            platform_b: Second market platform.
            is_same: Whether LLM says they're the same event.
            llm_response: Raw LLM response ("SAME" or "DIFFERENT").
        """
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
            # Don't fail if cache write fails - the validation still works
            self.logger.warning("DB cache store failed", error=str(e))

    async def _call_anthropic(self, prompt: str) -> str:
        """Call Anthropic Claude API."""
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip().upper()

    async def _call_openai(self, prompt: str) -> str:
        """Call OpenAI API."""
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=10,
        )
        return response.choices[0].message.content.strip().upper()

    async def validate_match(
        self,
        title_a: str,
        platform_a: str,
        title_b: str,
        platform_b: str,
    ) -> bool:
        """Validate whether two markets are about the same event.

        Uses a two-tier cache:
        1. Check in-memory cache (fast)
        2. Check database cache (persistent)
        3. If not cached, call LLM and store result in both caches

        Args:
            title_a: First market title.
            platform_a: First market platform.
            title_b: Second market title.
            platform_b: Second market platform.

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

        # Tier 3: Call LLM
        prompt = self.PROMPT_TEMPLATE.format(
            platform_a=platform_a,
            title_a=title_a,
            platform_b=platform_b,
            title_b=title_b,
        )

        try:
            if self.provider == "anthropic":
                answer = await self._call_anthropic(prompt)
            else:
                answer = await self._call_openai(prompt)

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
                provider=self.provider,
                title_a=title_a[:60],
                title_b=title_b[:60],
                result=answer,
                is_same=result,
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
        """Get cache statistics.

        Returns:
            Dict with memory cache size and provider info.
        """
        return {
            "memory_cache_size": len(self._memory_cache),
            "provider": self.provider,
            "model": self.model,
        }
