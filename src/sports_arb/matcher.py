"""Match markets across Kalshi and Polymarket."""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import structlog
import httpx

logger = structlog.get_logger()


@dataclass
class MatchedMarket:
    """A market matched across platforms."""
    event_key: str
    title: str
    category: str
    resolution_date: Optional[datetime] = None
    kalshi_market_id: Optional[str] = None
    kalshi_title: Optional[str] = None
    polymarket_token_id: Optional[str] = None
    polymarket_title: Optional[str] = None
    match_confidence: float = 0.0


class MarketMatcher:
    """Find matching markets across Kalshi and Polymarket."""

    def __init__(self):
        self._client = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def fetch_kalshi_markets(
        self,
        categories: Optional[list[str]] = None,
        limit: int = 200,
    ) -> list[dict]:
        """Fetch active markets from Kalshi."""
        markets = []
        cursor = None

        while len(markets) < limit:
            params = {
                "limit": min(100, limit - len(markets)),
                "status": "open",
            }
            if cursor:
                params["cursor"] = cursor

            resp = await self._client.get(
                "https://api.elections.kalshi.com/trade-api/v2/markets",
                params=params,
            )

            if resp.status_code != 200:
                logger.error("kalshi_fetch_error", status=resp.status_code)
                break

            data = resp.json()
            batch = data.get("markets", [])
            markets.extend(batch)

            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        if categories:
            markets = [
                m for m in markets
                if m.get("category", "").lower() in [c.lower() for c in categories]
            ]

        logger.info("kalshi_markets_fetched", count=len(markets))
        return markets

    async def fetch_polymarket_markets(
        self,
        categories: Optional[list[str]] = None,
        limit: int = 200,
    ) -> list[dict]:
        """Fetch active markets from Polymarket."""
        markets = []
        offset = 0

        while len(markets) < limit:
            params = {
                "limit": min(100, limit - len(markets)),
                "offset": offset,
                "active": "true",
                "closed": "false",
            }

            resp = await self._client.get(
                "https://gamma-api.polymarket.com/markets",
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            if resp.status_code != 200:
                logger.error("polymarket_fetch_error", status=resp.status_code)
                break

            batch = resp.json()
            if not batch:
                break

            markets.extend(batch)
            offset += len(batch)

        logger.info("polymarket_markets_fetched", count=len(markets))
        return markets

    def normalize_title(self, title: str) -> str:
        """Normalize a market title for comparison."""
        title = title.lower()
        title = re.sub(r'[^\w\s]', ' ', title)
        title = re.sub(r'\s+', ' ', title)

        stopwords = {
            'will', 'the', 'be', 'a', 'an', 'in', 'on', 'at', 'to',
            'of', 'for', 'by', 'or', 'and', 'is', 'it', 'this', 'that',
        }
        words = [w for w in title.split() if w not in stopwords]

        return ' '.join(words)

    def extract_key_terms(self, title: str) -> set[str]:
        """Extract key identifying terms from a title."""
        normalized = self.normalize_title(title)
        terms = set(normalized.split())

        date_pattern = r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d+|\d{4}\b'
        dates = re.findall(date_pattern, title.lower())
        terms.update(dates)

        number_pattern = r'\$?[\d,]+\.?\d*[km]?\b'
        numbers = re.findall(number_pattern, title.lower())
        terms.update(numbers)

        return terms

    def calculate_similarity(self, title_a: str, title_b: str) -> float:
        """Calculate similarity score between two titles."""
        terms_a = self.extract_key_terms(title_a)
        terms_b = self.extract_key_terms(title_b)

        if not terms_a or not terms_b:
            return 0.0

        intersection = terms_a & terms_b
        union = terms_a | terms_b

        jaccard = len(intersection) / len(union) if union else 0.0

        norm_a = self.normalize_title(title_a)
        norm_b = self.normalize_title(title_b)

        words_a = set(norm_a.split())
        words_b = set(norm_b.split())
        overlap = len(words_a & words_b) / max(len(words_a), len(words_b))

        return (jaccard * 0.6) + (overlap * 0.4)

    async def find_matches(
        self,
        min_confidence: float = 0.5,
        categories: Optional[list[str]] = None,
    ) -> list[MatchedMarket]:
        """Find matching markets across platforms."""
        kalshi_markets, poly_markets = await asyncio.gather(
            self.fetch_kalshi_markets(categories),
            self.fetch_polymarket_markets(categories),
        )

        matches = []
        used_poly = set()

        for k in kalshi_markets:
            k_title = k.get("title", "")
            k_ticker = k.get("ticker", "")
            best_match = None
            best_score = 0.0

            for i, p in enumerate(poly_markets):
                if i in used_poly:
                    continue

                p_title = p.get("question", "") or p.get("title", "")
                score = self.calculate_similarity(k_title, p_title)

                if score > best_score and score >= min_confidence:
                    best_score = score
                    best_match = (i, p)

            if best_match:
                idx, p = best_match
                used_poly.add(idx)

                tokens = p.get("tokens", [])
                yes_token = next(
                    (t for t in tokens if t.get("outcome", "").lower() == "yes"),
                    tokens[0] if tokens else None,
                )

                match = MatchedMarket(
                    event_key=f"{k_ticker}-{p.get('condition_id', '')[:8]}",
                    title=k_title,
                    category=k.get("category", ""),
                    kalshi_market_id=k_ticker,
                    kalshi_title=k_title,
                    polymarket_token_id=yes_token.get("token_id") if yes_token else None,
                    polymarket_title=p.get("question", ""),
                    match_confidence=best_score,
                )
                matches.append(match)

        matches.sort(key=lambda m: m.match_confidence, reverse=True)
        logger.info("markets_matched", count=len(matches))

        return matches


async def main():
    """Test market matching."""
    async with MarketMatcher() as matcher:
        matches = await matcher.find_matches(min_confidence=0.4)

        print(f"\nFound {len(matches)} matched markets:\n")
        for m in matches[:20]:
            print(f"[{m.match_confidence:.2f}] {m.title[:60]}")
            print(f"  Kalshi: {m.kalshi_market_id}")
            print(f"  Poly:   {m.polymarket_token_id[:32] if m.polymarket_token_id else 'N/A'}...")
            print()


if __name__ == "__main__":
    asyncio.run(main())
