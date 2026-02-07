"""LLM-based validation for cross-platform market matching.

Uses GPT-4o-mini to validate whether two prediction market titles
are asking about the same real-world event, filtering out false
positives from fuzzy matching.
"""

import hashlib
from typing import Optional

import structlog

logger = structlog.get_logger()


class LLMMatchValidator:
    """Validate fuzzy match candidates using an LLM."""

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

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        """Initialize validator.

        Args:
            api_key: OpenAI API key.
            model: Model to use for validation.
        """
        self.model = model
        self._cache: dict[str, bool] = {}
        self.logger = logger.bind(component="LLMMatchValidator")

        try:
            from openai import AsyncOpenAI
            self._client: Optional[AsyncOpenAI] = AsyncOpenAI(api_key=api_key)
        except ImportError:
            self.logger.warning("openai package not installed, LLM validation disabled")
            self._client = None

    @staticmethod
    def _cache_key(title_a: str, title_b: str) -> str:
        """Generate a deterministic cache key for a title pair."""
        pair = tuple(sorted([title_a.strip().lower(), title_b.strip().lower()]))
        return hashlib.sha256(f"{pair[0]}||{pair[1]}".encode()).hexdigest()

    async def validate_match(
        self,
        title_a: str,
        platform_a: str,
        title_b: str,
        platform_b: str,
    ) -> bool:
        """Validate whether two markets are about the same event.

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
        if key in self._cache:
            return self._cache[key]

        prompt = self.PROMPT_TEMPLATE.format(
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

            self._cache[key] = result
            self.logger.debug(
                "LLM validation",
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
